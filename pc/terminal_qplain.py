"""Interactive VT100 terminal widget backed by pyte (120x30 to match phone PTY).

Copy/paste: Ctrl+Shift+C / Ctrl+Shift+V (or context menu). Ctrl+C with a selection
copies; otherwise sends interrupt (0x03) to the remote. Ctrl+wheel / Ctrl+plus /
Ctrl+minus zoom the font. Mouse wheel / scrollbar / Shift+PageUp|PageDown scroll
through local scrollback history.

All scrollback is kept in a per-session file. Only a fixed memory window is held
in RAM / rendered; scrolling to the edge pages older or newer lines from the file.
"""
from PyQt6.QtCore import Qt, QEvent, pyqtSignal
from PyQt6.QtGui import (QAction, QColor, QFont, QKeyEvent, QKeySequence,
                         QPalette, QTextCursor)
from PyQt6.QtWidgets import QApplication, QPlainTextEdit, QMenu

import pyte

import theme
from scrollback import ScrollbackCache, session_path
from terminal_keys import KeyEncoder
from terminal_model import DualScreen


COLS, ROWS = 120, 30
# Max history lines kept in RAM / shown at once (live PTY rows are extra).
# Everything older lives only in the session file (unlimited).
SCROLLBACK_MEMORY = 10000
# Lines to page in from the session file when the user hits the scroll edge.
SCROLLBACK_PAGE = 500
MIN_FONT_SIZE, MAX_FONT_SIZE = 7, 26


class TerminalWidget(QPlainTextEdit):
    """Renders remote output via pyte and emits raw key bytes for REMOTE_DATA."""
    bytes_out = pyqtSignal(object)   # bytes
    zoom_changed = pyqtSignal(int)   # font point size
    escape_out = pyqtSignal()        # Escape while detached (used to leave full screen)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Set before any setter that can deliver an event: event() reads it, and
        # setFont() below fires a FontChange the moment the widget is constructed.
        self._attached = False
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._font_size = 11
        self._apply_font()
        # Hot window only; lines that fall out are spilled to the session file.
        self._screen = DualScreen(COLS, ROWS, history=SCROLLBACK_MEMORY)
        self._stream = pyte.ByteStream(self._screen)
        self._encoder = KeyEncoder(self._screen)
        self._session_id = "default"
        self._cache = ScrollbackCache(session_path(self._session_id))
        # Absolute index into (file + hot) of the first displayed history line.
        self._view_start = 0
        self._following = True
        self._paging_depth = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.apply_theme()

    # ---- Appearance ----
    def apply_theme(self):
        """Render in the selected terminal colour scheme (independent of the app).

        The visible area is the scroll area's *viewport*, which does not pick up
        a QPlainTextEdit background from the style sheet, so the palette is set
        on both and the viewport is told to fill itself.
        """
        bg = theme.terminal_color("bg")
        fg = theme.terminal_color("fg")
        self.setStyleSheet(
            "QPlainTextEdit { background-color: %s; color: %s; border: 1px solid %s; }"
            % (bg, fg, theme.color("border")))
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(bg))
        palette.setColor(QPalette.ColorRole.Text, QColor(fg))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.terminal_color("selection")))
        palette.setColor(QPalette.ColorRole.HighlightedText,
                         QColor(theme.terminal_color("selection_fg")))
        self.setPalette(palette)
        self.viewport().setPalette(palette)
        self.viewport().setAutoFillBackground(True)

    def _apply_font(self):
        font = QFont("Consolas", self._font_size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.zoom_changed.emit(self._font_size)

    def font_size(self) -> int:
        return self._font_size

    def zoom(self, delta: int):
        target = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, self._font_size + delta))
        if target != self._font_size:
            self._font_size = target
            self._apply_font()

    def reset_zoom(self):
        self._font_size = 11
        self._apply_font()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)
        self._maybe_page()

    def begin_session(self, session_id: str = "default"):
        """Start a fresh scrollback session (new cache file + cleared screen)."""
        self._session_id = session_id or "default"
        self._cache = ScrollbackCache(session_path(self._session_id))
        self._cache.reset()
        self._view_start = 0
        self._following = True
        self._screen.reset()
        self._render()

    def clear_screen(self):
        self.begin_session(self._session_id)

    def feed(self, data: bytes):
        if not data:
            return
        alt_before = self._screen.alt_active
        prev = [] if alt_before else list(self._screen.history.top)
        self._stream.feed(data)
        if not alt_before and not self._screen.alt_active:
            self._spill_history(prev)
        self._render()

    def set_attached(self, attached: bool):
        self._attached = attached
        if attached:
            self.setFocus()

    def session_file(self) -> str:
        return str(self._cache.path)

    @staticmethod
    def _row_text(row, columns: int) -> str:
        return "".join(row[x].data for x in range(columns))

    def _spill_history(self, prev_top):
        """Append lines dropped from pyte's hot deque to the session file."""
        if not prev_top or self._screen.alt_active:
            return
        cur_ids = {id(r) for r in self._screen.history.top}
        spilled = []
        for row in prev_top:
            if id(row) in cur_ids:
                break
            spilled.append(self._row_text(row, self._screen.columns))
        if spilled:
            self._cache.extend(spilled)

    def _scroll_len(self) -> int:
        if self._screen.alt_active:
            return self._screen.lines
        return len(self._cache) + len(self._screen.history.top)

    def _hot_texts(self) -> list[str]:
        cols = self._screen.columns
        return [self._row_text(row, cols) for row in self._screen.history.top]

    def _slice_scrollback(self, start: int, end: int) -> list[str]:
        """History lines [start:end] across session file + hot pyte buffer."""
        if self._screen.alt_active:
            return []
        file_n = len(self._cache)
        hot = self._hot_texts()
        total = file_n + len(hot)
        end = max(0, min(end, total))
        start = max(0, min(start, end))
        lines: list[str] = []
        if start < file_n:
            lines.extend(self._cache.slice(start, min(end, file_n)))
        if end > file_n:
            hs = max(0, start - file_n)
            he = end - file_n
            lines.extend(hot[hs:he])
        return lines

    def _on_scroll(self, _value: int):
        self._maybe_page()

    def _maybe_page(self):
        if self._paging_depth or self._screen.alt_active:
            return
        sb = self.verticalScrollBar()
        if sb.value() <= 8:
            self._page_older()
        elif sb.value() >= sb.maximum() - 4:
            self._page_newer_or_follow()

    def _page_older(self):
        if self._view_start <= 0:
            return
        page = min(SCROLLBACK_PAGE, self._view_start)
        self._paging_depth += 1
        try:
            self._following = False
            sb = self.verticalScrollBar()
            old_max = sb.maximum()
            old_val = sb.value()
            self._view_start -= page
            self._render(preserve_scroll=False)
            new_max = sb.maximum()
            sb.setValue(old_val + (new_max - old_max))
        finally:
            self._paging_depth = max(0, self._paging_depth - 1)

    def _page_newer_or_follow(self):
        total = self._scroll_len()
        end = self._view_start + SCROLLBACK_MEMORY
        if end >= total:
            if not self._following:
                self._following = True
                self._render()
            return
        page = min(SCROLLBACK_PAGE, total - end)
        self._paging_depth += 1
        try:
            sb = self.verticalScrollBar()
            old_val = sb.value()
            self._view_start += page
            if self._view_start + SCROLLBACK_MEMORY >= total:
                self._following = True
            self._render(preserve_scroll=False)
            # Stay near the bottom edge so further scrolling can page again.
            sb.setValue(min(old_val, sb.maximum()))
        finally:
            self._paging_depth = max(0, self._paging_depth - 1)

    def _render(self, preserve_scroll: bool = True):
        # Ignore scrollbar signals from setPlainText / setValue so live output
        # cannot accidentally page out of follow mode.
        self._paging_depth += 1
        try:
            self._render_body(preserve_scroll)
        finally:
            self._paging_depth = max(0, self._paging_depth - 1)

    def _render_body(self, preserve_scroll: bool = True):
        sb = self.verticalScrollBar()
        old_scroll = sb.value()

        total = self._scroll_len()
        alt = self._screen.alt_active
        if alt:
            self._following = True
            self._view_start = 0
            start, end = 0, total
            lines = []
            show_screen = True
        else:
            if self._following:
                self._view_start = max(0, total - SCROLLBACK_MEMORY)

            start = self._view_start
            end = min(total, start + SCROLLBACK_MEMORY)
            lines = self._slice_scrollback(start, end)
            # Live PTY rows only when this window includes the newest history.
            show_screen = self._following or end >= total
        if show_screen:
            cols = self._screen.columns
            for y in range(self._screen.lines):
                lines.append(self._row_text(self._screen.buffer[y], cols))

        had_sel = self.textCursor().hasSelection()
        sel_start = self.textCursor().selectionStart() if had_sel else 0
        sel_end = self.textCursor().selectionEnd() if had_sel else 0

        text = "\n".join(lines)
        self.setPlainText(text)

        if had_sel and sel_start < sel_end:
            cur = self.textCursor()
            cur.setPosition(min(sel_start, len(text)))
            cur.setPosition(min(sel_end, len(text)), QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cur)
        elif self._following:
            hist_shown = 0 if alt else end - start
            cur = self.textCursor()
            block = min(hist_shown + self._screen.cursor.y, self.blockCount() - 1)
            cur.movePosition(QTextCursor.MoveOperation.Start)
            for _ in range(block):
                cur.movePosition(QTextCursor.MoveOperation.NextBlock)
            cur.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor,
                             min(self._screen.cursor.x, COLS - 1))
            self.setTextCursor(cur)

        if self._following:
            sb.setValue(sb.maximum())
        elif preserve_scroll:
            sb.setValue(min(old_scroll, sb.maximum()))

    def _context_menu(self, pos):
        # A tab in the action text is rendered as the shortcut column by QMenu.
        menu = QMenu(self)
        act_copy = QAction("Copy\tCtrl+Shift+C", self)
        act_copy.setEnabled(self.textCursor().hasSelection())
        act_copy.triggered.connect(self.copy)
        act_paste = QAction("Paste\tCtrl+Shift+V", self)
        act_paste.setEnabled(self._attached and bool(QApplication.clipboard().text()))
        act_paste.triggered.connect(self._paste_clipboard)
        act_select = QAction("Select all", self)
        act_select.triggered.connect(self.selectAll)
        menu.addAction(act_copy)
        menu.addAction(act_paste)
        menu.addSeparator()
        menu.addAction(act_select)
        menu.addSeparator()
        act_bigger = QAction("Bigger text\tCtrl++", self)
        act_bigger.setEnabled(self._font_size < MAX_FONT_SIZE)
        act_bigger.triggered.connect(lambda: self.zoom(1))
        act_smaller = QAction("Smaller text\tCtrl+-", self)
        act_smaller.setEnabled(self._font_size > MIN_FONT_SIZE)
        act_smaller.triggered.connect(lambda: self.zoom(-1))
        act_reset = QAction("Reset text size\tCtrl+0", self)
        act_reset.triggered.connect(self.reset_zoom)
        menu.addAction(act_bigger)
        menu.addAction(act_smaller)
        menu.addAction(act_reset)
        menu.exec(self.mapToGlobal(pos))

    def _paste_clipboard(self):
        if not self._attached:
            return
        text = QApplication.clipboard().text()
        if text:
            # Normalize Windows newlines for remote PTY
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            self.bytes_out.emit(text.encode("utf-8", "ignore"))

    def event(self, ev):
        # QWidget::event() consumes Tab/Backtab for focus traversal *before*
        # keyPressEvent() runs, so the terminal never sees them. While attached
        # we want Tab to reach the remote shell (completion); intercept here.
        if (self._attached and ev.type() == QEvent.Type.KeyPress
                and ev.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)):
            self.keyPressEvent(ev)
            return True
        return super().event(ev)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        # Zoom works whether or not a session is attached.
        if ctrl and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(1)
            event.accept()
            return
        if ctrl and key == Qt.Key.Key_Minus:
            self.zoom(-1)
            event.accept()
            return
        if ctrl and key == Qt.Key.Key_0:
            self.reset_zoom()
            event.accept()
            return

        if not self._attached:
            # Escape is only free when nothing is attached; otherwise it is a
            # real key the remote expects.
            if key == Qt.Key.Key_Escape:
                self.escape_out.emit()
                event.accept()
                return
            return super().keyPressEvent(event)

        # Ctrl+Shift+C / Ctrl+Insert → copy
        if (ctrl and shift and key == Qt.Key.Key_C) or (ctrl and key == Qt.Key.Key_Insert):
            if self.textCursor().hasSelection():
                self.copy()
            event.accept()
            return

        # Ctrl+Shift+V / Shift+Insert → paste to remote
        if (ctrl and shift and key == Qt.Key.Key_V) or (shift and key == Qt.Key.Key_Insert):
            self._paste_clipboard()
            event.accept()
            return

        # Ctrl+C: copy if selection, else remote interrupt
        if ctrl and key == Qt.Key.Key_C and not shift:
            if self.textCursor().hasSelection():
                self.copy()
                event.accept()
                return
            self.bytes_out.emit(b"\x03")
            event.accept()
            return

        # Ctrl+V without shift: paste (also common on Windows)
        if ctrl and key == Qt.Key.Key_V and not shift:
            self._paste_clipboard()
            event.accept()
            return

        # Shift+PageUp/PageDown scroll local scrollback; plain Page* go remote.
        if shift and key == Qt.Key.Key_PageUp:
            sb = self.verticalScrollBar()
            sb.setValue(sb.value() - sb.pageStep())
            self._maybe_page()
            event.accept()
            return
        if shift and key == Qt.Key.Key_PageDown:
            sb = self.verticalScrollBar()
            sb.setValue(sb.value() + sb.pageStep())
            self._maybe_page()
            event.accept()
            return

        # Let standard copy shortcut work when not attached-handling
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self._paste_clipboard()
            event.accept()
            return

        data = self._map_key(event)
        if data is not None:
            self.bytes_out.emit(data)
            event.accept()
            return
        super().keyPressEvent(event)

    def _map_key(self, event: QKeyEvent):
        return self._encoder.encode(event.key(), event.modifiers(), event.text())
