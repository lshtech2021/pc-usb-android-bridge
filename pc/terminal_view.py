"""Native Qt terminal view for the remote SSH PTY.

The pyte model lives in :mod:`terminal_model`; this module paints only the
visible character cells, owns the scrollback window, and translates Qt input
into VT bytes. It intentionally does not use QPlainTextEdit/QTextDocument: a
terminal is a fixed cell grid, not a text document.
"""
from __future__ import annotations

from wcwidth import wcwidth

from PyQt6.QtCore import Qt, QEvent, QRect, QRectF, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetricsF,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication, QFrame, QMenu

import pyte

import theme
from scrollback import ScrollbackStore, session_path
from terminal_keys import KeyEncoder
from terminal_model import (
    DualScreen,
    HOT_HISTORY,
    Style,
    normalize_color,
    private_mode,
    row_runs,
)

COLS, ROWS = 120, 30
MIN_FONT_SIZE, MAX_FONT_SIZE = 7, 26
SCROLL_LINES = 3
FEED_CHUNK = 8 * 1024
FEED_INTERVAL_MS = 16
# The previous terminal sent clipboard text verbatim. Some remote shells that
# advertise ?2004 still mishandle the bracketed wrapper, so keep raw paste as
# the default; flip this to True to use bracketed paste for multi-line text.
USE_BRACKETED_PASTE = False

_BASE_TO_BRIGHT = {
    "black": "brightblack",
    "red": "brightred",
    "green": "brightgreen",
    "yellow": "brightyellow",
    "blue": "brightblue",
    "magenta": "brightmagenta",
    "cyan": "brightcyan",
    "white": "brightwhite",
}


class TerminalWidget(QAbstractScrollArea):
    """Character-cell terminal widget backed by pyte."""

    bytes_out = pyqtSignal(object)   # bytes to the remote PTY
    zoom_changed = pyqtSignal(int)   # font point size
    escape_out = pyqtSignal()        # Escape while detached (full screen)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._attached = False
        self._font_size = 11
        self._session_id = "default"

        self._store = ScrollbackStore(session_path(self._session_id), COLS)
        self._store.reset(COLS)
        self._screen = DualScreen(COLS, ROWS, history=HOT_HISTORY,
                                  on_evict=self._on_history_evict)
        self._stream = pyte.ByteStream(self._screen)
        self._encoder = KeyEncoder(self._screen)

        self._pending = bytearray()
        self._evicted = []
        self._feed_timer = QTimer(self)
        self._feed_timer.setInterval(FEED_INTERVAL_MS)
        self._feed_timer.timeout.connect(self._drain_pending)

        self._sel_anchor = None       # (absolute line, column)
        self._sel_cursor = None
        self._selecting = False
        self._preedit = ""
        self._follow = True
        self._normal_follow = True
        self._normal_scroll = 0
        self._syncing = False
        self._visible_rows = 1
        self._color_cache = {}
        self._font_variants = {}

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.viewport().setMouseTracking(True)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().installEventFilter(self)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

        # Keep copy/paste working even if a future event path consumes the key
        # before keyPressEvent. WidgetWithChildren keeps these scoped to the
        # terminal, so the message/file inputs keep their normal clipboard.
        for sequence, slot in (
            ("Ctrl+Shift+C", self.copy),
            ("Ctrl+Insert", self.copy),
            ("Ctrl+Shift+V", self._paste_clipboard),
            ("Shift+Insert", self._paste_clipboard),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self, activated=slot)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.horizontalScrollBar().valueChanged.connect(
            lambda _v: self.viewport().update())

        self._apply_font()
        self.apply_theme()

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------
    def apply_theme(self):
        self._color_cache.clear()
        self.viewport().update()

    def _apply_font(self):
        font = QFont("Consolas", self._font_size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        try:
            font.setFamilies(
                ["Consolas", "Cascadia Mono", "Microsoft YaHei Mono", "monospace"])
        except Exception:
            pass
        self._font = font
        self.setFont(font)
        metrics = QFontMetricsF(font)
        self._cell_w = max(1.0, metrics.horizontalAdvance("M"))
        self._cell_h = max(1.0, metrics.height())
        self._font_variants = {}
        for bold in (False, True):
            for italic in (False, True):
                for underline in (False, True):
                    for strike in (False, True):
                        variant = QFont(font)
                        variant.setBold(bold)
                        variant.setItalic(italic)
                        variant.setUnderline(underline)
                        variant.setStrikeOut(strike)
                        self._font_variants[
                            (bold, italic, underline, strike)] = variant
        self._sync_scrollbar()
        self.viewport().update()
        self.zoom_changed.emit(self._font_size)

    def font_size(self) -> int:
        return self._font_size

    def zoom(self, delta: int):
        target = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, self._font_size + delta))
        if target != self._font_size:
            self._font_size = target
            self._apply_font()

    def reset_zoom(self):
        if self._font_size != 11:
            self._font_size = 11
            self._apply_font()

    def _color(self, value: str, default_key: str) -> QColor:
        key = (value, default_key)
        cached = self._color_cache.get(key)
        if cached is not None:
            return cached
        name = normalize_color(value)
        if name == "default":
            name = theme.terminal_color(default_key)
        if name.startswith("#"):
            color = QColor(name)
        else:
            try:
                color = QColor(theme.terminal_color(name))
            except KeyError:
                color = QColor(theme.terminal_color(default_key))
        self._color_cache[key] = color
        return color

    def _style_colors(self, style):
        fg = normalize_color(style.fg)
        bg = normalize_color(style.bg)
        if style.bold and fg in _BASE_TO_BRIGHT:
            fg = _BASE_TO_BRIGHT[fg]
        if style.reverse:
            if fg == "default" and bg == "default":
                return self._color("default", "bg"), self._color("default", "fg")
            fg, bg = bg, fg
        return self._color(fg, "fg"), self._color(bg, "bg")

    def _font_for(self, style) -> QFont:
        key = (bool(style.bold), bool(style.italic),
               bool(style.underline), bool(style.strike))
        return self._font_variants[key]

    # ------------------------------------------------------------------
    # Public API (mirrors the old QPlainTextEdit widget)
    # ------------------------------------------------------------------
    def begin_session(self, session_id: str = "default"):
        self._session_id = session_id or "default"
        self._store = ScrollbackStore(session_path(self._session_id), COLS)
        self._store.reset(COLS)
        self._screen.reset()
        self._pending.clear()
        self._evicted.clear()
        self._feed_timer.stop()
        self._sel_anchor = self._sel_cursor = None
        self._selecting = False
        self._preedit = ""
        self._follow = True
        self._normal_follow = True
        self._normal_scroll = 0
        self._sync_scrollbar()
        self.viewport().update()

    def clear_screen(self):
        self.begin_session(self._session_id)

    def feed(self, data: bytes):
        if not data:
            return
        self._pending.extend(data)
        if not self._feed_timer.isActive():
            self._feed_timer.start()

    def set_attached(self, attached: bool):
        self._attached = attached
        if attached:
            self.setFocus()
        self.viewport().update()

    def session_file(self) -> str:
        return str(self._store.path)

    # ------------------------------------------------------------------
    # Model / history
    # ------------------------------------------------------------------
    def _total_lines(self) -> int:
        if self._screen.alt_active:
            return self._screen.lines
        return (len(self._store) + len(self._screen.history.top)
                + self._screen.lines)

    def _history_base(self) -> int:
        return len(self._store) + len(self._screen.history.top)

    def _lines(self, start: int, end: int):
        """Return styled runs for absolute lines [start, end)."""
        if end <= start:
            return []
        if self._screen.alt_active:
            a = max(0, start)
            b = min(self._screen.lines, end)
            return [row_runs(self._screen.buffer[y], COLS) for y in range(a, b)]

        file_n = len(self._store)
        hot = list(self._screen.history.top)
        hot_n = len(hot)
        base = file_n + hot_n
        out = []
        if start < file_n:
            out.extend(self._store.slice(start, min(end, file_n)))
        if end > file_n:
            hs = max(0, start - file_n)
            he = min(hot_n, end - file_n)
            out.extend(row_runs(hot[i], COLS) for i in range(hs, he))
        if end > base:
            ss = max(0, start - base)
            se = min(self._screen.lines, end - base)
            out.extend(row_runs(self._screen.buffer[y], COLS)
                       for y in range(ss, se))
        return out

    def _line_runs(self, line: int):
        if line < 0 or line >= self._total_lines():
            return []
        lines = self._lines(line, line + 1)
        return lines[0] if lines else []

    def _on_history_evict(self, row):
        """Called by DualScreen when a line leaves the hot history window."""
        self._evicted.append(row_runs(row, COLS))

    def _flush_evicted(self):
        if self._evicted:
            self._store.append_many(self._evicted)
            self._evicted.clear()

    def _drain_pending(self):
        if not self._pending:
            self._feed_timer.stop()
            return
        chunk = bytes(self._pending[:FEED_CHUNK])
        del self._pending[:FEED_CHUNK]
        was_alt = self._screen.alt_active
        if not was_alt:
            self._normal_follow = self._follow
            self._normal_scroll = self.verticalScrollBar().value()
        self._stream.feed(chunk)
        now_alt = self._screen.alt_active
        self._flush_evicted()
        self._sync_scrollbar()
        if was_alt and not now_alt:
            self._follow = self._normal_follow
            self.verticalScrollBar().setValue(self._normal_scroll)
            self._sync_scrollbar()
        self.viewport().update()
        if not self._pending:
            self._feed_timer.stop()

    def _flush_pending(self):
        """Apply queued remote output before interpreting an input event.

        The remote may enable DECCKM, bracketed paste or mouse reporting in the
        same burst as the user's next key/click; interpreting the event against
        stale modes produces the wrong bytes.
        """
        if self._pending:
            self._drain_pending()

    # ------------------------------------------------------------------
    # Scrollbar / viewport
    # ------------------------------------------------------------------
    def _sync_scrollbar(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            viewport = self.viewport()
            visible = max(1, int(viewport.height() // self._cell_h))
            total = self._total_lines()
            vmax = max(0, total - visible)
            vbar = self.verticalScrollBar()
            vbar.setRange(0, vmax)
            vbar.setPageStep(visible)
            vbar.setSingleStep(1)
            if self._follow:
                vbar.setValue(vmax)
            else:
                vbar.setValue(min(vbar.value(), vmax))

            hmax = max(0, int(COLS * self._cell_w) - viewport.width())
            hbar = self.horizontalScrollBar()
            hbar.setRange(0, hmax)
            hbar.setPageStep(max(1, viewport.width()))
            hbar.setSingleStep(max(1, int(self._cell_w)))
            self._visible_rows = visible
        finally:
            self._syncing = False

    def _on_scroll(self, value: int):
        if self._syncing:
            return
        self._follow = value >= self.verticalScrollBar().maximum()
        self.viewport().update()

    def scrollContentsBy(self, _dx: int, _dy: int):
        # We repaint the visible slice; the default scroll-area blit is wrong
        # for a virtual viewport.
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_scrollbar()

    def sizeHint(self) -> QSize:
        # Keep the layout minimum below a 1366x768 laptop screen; the fixed
        # 120-column grid then uses the horizontal scrollbar instead of forcing
        # an oversized window.
        return QSize(int(self._cell_w * 60), int(self._cell_h * 20))

    def _scroll_lines(self, delta: int):
        vbar = self.verticalScrollBar()
        vbar.setValue(max(0, min(vbar.maximum(), vbar.value() + delta)))
        self._follow = vbar.value() >= vbar.maximum()
        self.viewport().update()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self.viewport())
        bg = QColor(theme.terminal_color("bg"))
        painter.fillRect(self.viewport().rect(), bg)

        start = self.verticalScrollBar().value()
        visible = self._visible_rows
        lines = self._lines(start, start + visible)
        hscroll = self.horizontalScrollBar().value()
        cw, ch = self._cell_w, self._cell_h
        sel = self._selection_range()
        selection_color = QColor(theme.terminal_color("selection"))
        selection_color.setAlpha(160)

        for row, runs in enumerate(lines):
            line_no = start + row
            y = row * ch
            x = 0.0
            for run in runs:
                _fg, run_bg = self._style_colors(run.style)
                if run_bg != bg:
                    painter.fillRect(
                        QRectF(x - hscroll, y, run.cells * cw, ch), run_bg)
                x += run.cells * cw

            if sel and sel[0][0] <= line_no <= sel[1][0]:
                a, b = sel
                c0 = a[1] if line_no == a[0] else 0
                c1 = b[1] + 1 if line_no == b[0] else COLS
                painter.fillRect(
                    QRectF(c0 * cw - hscroll, y, (c1 - c0) * cw, ch),
                    selection_color)

            x = 0.0
            for run in runs:
                if run.text and run.text.strip():
                    fg, _bg = self._style_colors(run.style)
                    painter.setPen(fg)
                    painter.setFont(self._font_for(run.style))
                    painter.drawText(
                        QRectF(x - hscroll, y, run.cells * cw, ch),
                        int(Qt.AlignmentFlag.AlignLeft
                            | Qt.AlignmentFlag.AlignVCenter),
                        run.text)
                x += run.cells * cw

        self._paint_cursor(painter, start, visible, hscroll, cw, ch)
        self._paint_preedit(painter, start, hscroll, cw, ch)
        painter.end()

    def _paint_cursor(self, painter, start, visible, hscroll, cw, ch):
        line = self._cursor_abs_line()
        if line is None or not (start <= line < start + visible):
            return
        cursor = self._screen.cursor
        x = cursor.x * cw - hscroll
        y = (line - start) * ch
        color = QColor(theme.terminal_color("cursor"))
        ch_data = self._screen.buffer[cursor.y][cursor.x]
        wide = bool(ch_data.data) and wcwidth(ch_data.data) == 2
        width = cw * (2 if wide else 1)
        if self.hasFocus():
            painter.fillRect(QRectF(x, y, width, ch), color)
            # Repaint the character under the block in the terminal background.
            if ch_data.data:
                painter.setPen(QColor(theme.terminal_color("bg")))
                painter.setFont(self._font_for(_style_from_char(ch_data)))
                painter.drawText(
                    QRectF(x, y, width, ch),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    ch_data.data)
        else:
            painter.setPen(color)
            painter.drawRect(QRectF(x + 0.5, y + 0.5, width - 1, ch - 1))

    def _paint_preedit(self, painter, start, hscroll, cw, ch):
        if not self._preedit:
            return
        line = self._cursor_abs_line()
        if line is None or not (start <= line < start + self._visible_rows):
            return
        cursor = self._screen.cursor
        x = cursor.x * cw - hscroll
        y = (line - start) * ch
        font = QFont(self._font)
        font.setUnderline(True)
        painter.setFont(font)
        painter.setPen(QColor(theme.terminal_color("fg")))
        cells = sum(2 if wcwidth(ch) == 2 else 1 for ch in self._preedit)
        painter.drawText(
            QRectF(x, y, cw * max(1, cells), ch),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._preedit)

    # ------------------------------------------------------------------
    # Selection / clipboard
    # ------------------------------------------------------------------
    def _selection_range(self):
        if self._sel_anchor is None or self._sel_cursor is None:
            return None
        a, b = self._sel_anchor, self._sel_cursor
        if a > b:
            a, b = b, a
        return a, b

    def _has_selection(self) -> bool:
        sel = self._selection_range()
        return bool(sel and sel[0] != sel[1])

    def _cell_at(self, pos):
        start = self.verticalScrollBar().value()
        col = int((pos.x() + self.horizontalScrollBar().value()) // self._cell_w)
        row = int(pos.y() // self._cell_h)
        total = self._total_lines()
        line = max(0, min(start + row, total - 1)) if total else 0
        col = max(0, min(col, COLS - 1))
        return (line, col)

    def _run_columns(self, runs) -> list[str]:
        cols: list[str] = []
        for run in runs:
            consumed = 0
            for char in run.text:
                width = 2 if wcwidth(char) == 2 else 1
                cols.append(char)
                consumed += 1
                if width == 2:
                    cols.append("")
                    consumed += 1
            if consumed < run.cells:
                cols.extend([""] * (run.cells - consumed))
        return cols[:COLS]

    def _selection_text(self) -> str:
        sel = self._selection_range()
        if not sel:
            return ""
        a, b = sel
        out = []
        for line in range(a[0], b[0] + 1):
            cols = self._run_columns(self._line_runs(line))
            start = a[1] if line == a[0] else 0
            end = b[1] + 1 if line == b[0] else COLS
            start = max(0, min(start, COLS))
            end = max(start, min(end, COLS))
            out.append("".join(cols[start:end]).rstrip())
        return "\n".join(out)

    def copy(self):
        text = self._selection_text()
        if text:
            QApplication.clipboard().setText(text)

    def selectAll(self):
        total = self._total_lines()
        if total:
            self._sel_anchor = (0, 0)
            self._sel_cursor = (total - 1, COLS - 1)
            self.viewport().update()

    def _paste_clipboard(self):
        if not self._attached:
            return
        self._flush_pending()
        text = QApplication.clipboard().text()
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if USE_BRACKETED_PASTE and private_mode(self._screen, 2004):
            text = "\x1b[200~" + text + "\x1b[201~"
        self._send_text(text)

    def _send_text(self, text: str):
        if text:
            self.bytes_out.emit(text.encode("utf-8", "ignore"))

    # ------------------------------------------------------------------
    # Keyboard / IME
    # ------------------------------------------------------------------
    def event(self, ev):
        # QWidget consumes Tab/Backtab for focus traversal before
        # keyPressEvent() runs; while attached they belong to the remote.
        if (self._attached and ev.type() == QEvent.Type.KeyPress
                and ev.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)):
            self.keyPressEvent(ev)
            return True
        return super().event(ev)

    def keyPressEvent(self, event: QKeyEvent):
        self._flush_pending()
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

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
            if key == Qt.Key.Key_Escape:
                self.escape_out.emit()
                event.accept()
                return
            super().keyPressEvent(event)
            return

        if (ctrl and shift and key == Qt.Key.Key_C) or (ctrl and key == Qt.Key.Key_Insert):
            self.copy()
            event.accept()
            return
        if (ctrl and shift and key == Qt.Key.Key_V) or (shift and key == Qt.Key.Key_Insert):
            self._paste_clipboard()
            event.accept()
            return
        if ctrl and key == Qt.Key.Key_C and not shift:
            if self._has_selection():
                self.copy()
            else:
                self._send_text("\x03")
            event.accept()
            return
        if ctrl and key == Qt.Key.Key_V and not shift:
            self._paste_clipboard()
            event.accept()
            return

        if shift and key == Qt.Key.Key_PageUp:
            self._scroll_lines(-self._visible_rows)
            event.accept()
            return
        if shift and key == Qt.Key.Key_PageDown:
            self._scroll_lines(self._visible_rows)
            event.accept()
            return

        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self._paste_clipboard()
            event.accept()
            return

        data = self._encoder.encode(key, mods, event.text())
        if data is not None:
            self.bytes_out.emit(data)
            event.accept()
            return
        super().keyPressEvent(event)

    def inputMethodEvent(self, event: QInputMethodEvent):
        self._preedit = event.preeditString()
        commit = event.commitString()
        if commit:
            self._send_text(commit)
        self.viewport().update()
        event.accept()

    def inputMethodQuery(self, query):
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            return self._cursor_rect()
        if query == Qt.InputMethodQuery.ImEnabled:
            return True
        if query == Qt.InputMethodQuery.ImHints:
            return (Qt.InputMethodHint.ImhNoPredictiveText
                    | Qt.InputMethodHint.ImhNoAutoUppercase).value
        return super().inputMethodQuery(query)

    def _cursor_abs_line(self):
        if self._screen.cursor.hidden:
            return None
        if self._screen.alt_active:
            return self._screen.cursor.y
        return self._history_base() + self._screen.cursor.y

    def _cursor_rect(self) -> QRect:
        line = self._cursor_abs_line()
        start = self.verticalScrollBar().value()
        if line is None or not (start <= line < start + self._visible_rows):
            return QRect(-1000, -1000, 0, 0)
        cursor = self._screen.cursor
        x = int(cursor.x * self._cell_w - self.horizontalScrollBar().value())
        y = int((line - start) * self._cell_h)
        ch_data = self._screen.buffer[cursor.y][cursor.x]
        wide = bool(ch_data.data) and wcwidth(ch_data.data) == 2
        return QRect(x, y, int(self._cell_w) * (2 if wide else 1),
                     int(self._cell_h))

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if private_mode(self._screen, 1004):
            self._send_text("\x1b[I")
        self.viewport().update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if private_mode(self._screen, 1004):
            self._send_text("\x1b[O")
        self.viewport().update()

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------
    def _mouse_reporting(self) -> bool:
        return any(private_mode(self._screen, n) for n in (1000, 1002, 1003))

    def _screen_row(self, line: int) -> int:
        if self._screen.alt_active:
            return max(0, min(line, ROWS - 1))
        row = line - self._history_base()
        return max(0, min(row, ROWS - 1))

    def _send_mouse(self, kind: str, pos, button: int, mods):
        sgr = private_mode(self._screen, 1006)
        row = self._screen_row(self._cell_at(pos)[0])
        col = self._cell_at(pos)[1]
        code = {0: 0, 1: 1, 2: 2}.get(button, button)
        if kind == "wheel_up":
            code = 64
        elif kind == "wheel_down":
            code = 65
        elif kind == "motion":
            code = 32 + code
        if mods & Qt.KeyboardModifier.ShiftModifier:
            code += 4
        if mods & Qt.KeyboardModifier.AltModifier:
            code += 8
        if mods & Qt.KeyboardModifier.ControlModifier:
            code += 16
        if sgr:
            final = b"m" if kind == "release" else b"M"
            seq = b"\x1b[<" + str(code).encode() + b";" + \
                str(col + 1).encode() + b";" + str(row + 1).encode() + final
        else:
            if kind == "release":
                code = 3
            x = min(222, col)
            y = min(222, row)
            seq = b"\x1b[M" + bytes([32 + code, 33 + x, 33 + y])
        self.bytes_out.emit(seq)

    def _button_number(self, button) -> int:
        if button == Qt.MouseButton.LeftButton:
            return 0
        if button == Qt.MouseButton.MiddleButton:
            return 1
        if button == Qt.MouseButton.RightButton:
            return 2
        return 0

    def mousePressEvent(self, event):
        self._flush_pending()
        self.setFocus()
        pos = event.position().toPoint()
        if self._mouse_reporting() and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._send_mouse("press", pos, self._button_number(event.button()),
                             event.modifiers())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            cell = self._cell_at(pos)
            self._sel_anchor = cell
            self._sel_cursor = cell
            self._selecting = True
            self.viewport().update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._selecting and (event.buttons() & Qt.MouseButton.LeftButton):
            self._sel_cursor = self._cell_at(pos)
            self.viewport().update()
            event.accept()
            return
        if self._mouse_reporting():
            held = bool(event.buttons() & Qt.MouseButton.LeftButton)
            if (held and private_mode(self._screen, 1002)) or \
                    private_mode(self._screen, 1003):
                button = self._button_number(event.button()) if held else 3
                self._send_mouse("motion", pos, button, event.modifiers())
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint()
        if self._selecting:
            self._sel_cursor = self._cell_at(pos)
            self._selecting = False
            self.viewport().update()
            event.accept()
            return
        if self._mouse_reporting() and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._send_mouse("release", pos, self._button_number(event.button()),
                             event.modifiers())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        cell = self._cell_at(event.position().toPoint())
        cols = self._run_columns(self._line_runs(cell[0]))
        if not cols:
            event.accept()
            return
        start = min(cell[1], len(cols) - 1)
        end = start
        while start > 0 and cols[start - 1].strip():
            start -= 1
        while end + 1 < len(cols) and cols[end + 1].strip():
            end += 1
        self._sel_anchor = (cell[0], start)
        self._sel_cursor = (cell[0], end)
        self.viewport().update()
        event.accept()

    def wheelEvent(self, event):
        self._flush_pending()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        if self._mouse_reporting() and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            pos = event.position().toPoint()
            kind = "wheel_up" if event.angleDelta().y() > 0 else "wheel_down"
            self._send_mouse(kind, pos, 0, event.modifiers())
            event.accept()
            return
        self._scroll_lines(-SCROLL_LINES if event.angleDelta().y() > 0
                           else SCROLL_LINES)
        event.accept()

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._context_menu(event.pos())
            return True
        return super().eventFilter(obj, event)

    def contextMenuEvent(self, event):
        pos = self.viewport().mapFrom(self, event.pos())
        self._context_menu(pos)
        event.accept()

    def _context_menu(self, pos):
        if self._mouse_reporting() and not (
                QApplication.keyboardModifiers()
                & Qt.KeyboardModifier.ShiftModifier):
            # The app owns the mouse; the press/release were already sent by
            # mousePressEvent/mouseReleaseEvent. Shift+right-click opens the
            # local menu.
            return
        menu = QMenu(self)
        act_copy = QAction("Copy\tCtrl+Shift+C", self)
        act_copy.setEnabled(self._has_selection())
        act_copy.triggered.connect(self.copy)
        act_paste = QAction("Paste\tCtrl+Shift+V", self)
        act_paste.setEnabled(
            self._attached and bool(QApplication.clipboard().text()))
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
        menu.exec(self.viewport().mapToGlobal(pos))


def _style_from_char(ch):
    return Style(
        fg=ch.fg,
        bg=ch.bg,
        bold=bool(ch.bold),
        italic=bool(ch.italics),
        underline=bool(ch.underscore),
        strike=bool(ch.strikethrough),
        reverse=bool(ch.reverse),
        blink=bool(ch.blink),
    )
