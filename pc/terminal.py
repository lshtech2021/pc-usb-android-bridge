"""Interactive VT100 terminal widget backed by pyte (120x30 to match phone PTY).

Copy/paste: Ctrl+Shift+C / Ctrl+Shift+V (or context menu). Ctrl+C with a selection
copies; otherwise sends interrupt (0x03) to the remote.
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont, QKeyEvent, QTextCursor, QKeySequence
from PyQt5.QtWidgets import QApplication, QPlainTextEdit, QAction, QMenu

import pyte


COLS, ROWS = 120, 30


class TerminalWidget(QPlainTextEdit):
    """Renders remote output via pyte and emits raw key bytes for REMOTE_DATA."""
    bytes_out = pyqtSignal(object)   # bytes

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        font = QFont("Consolas", 11)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setStyleSheet("QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; }")
        self._screen = pyte.Screen(COLS, ROWS)
        self._stream = pyte.ByteStream(self._screen)
        self._attached = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def clear_screen(self):
        self._screen.reset()
        self._render()

    def feed(self, data: bytes):
        if not data:
            return
        self._stream.feed(data)
        self._render()

    def set_attached(self, attached: bool):
        self._attached = attached
        if attached:
            self.setFocus()

    def _render(self):
        # Preserve selection across refresh when possible
        had_sel = self.textCursor().hasSelection()
        sel_start = self.textCursor().selectionStart() if had_sel else 0
        sel_end = self.textCursor().selectionEnd() if had_sel else 0

        lines = []
        for y in range(self._screen.lines):
            row = self._screen.buffer[y]
            lines.append("".join(row[x].data for x in range(self._screen.columns)))
        text = "\n".join(lines)
        self.setPlainText(text)

        if had_sel and sel_start < sel_end:
            cur = self.textCursor()
            cur.setPosition(min(sel_start, len(text)))
            cur.setPosition(min(sel_end, len(text)), QTextCursor.KeepAnchor)
            self.setTextCursor(cur)
        else:
            cur = self.textCursor()
            block = min(self._screen.cursor.y, self.blockCount() - 1)
            cur.movePosition(QTextCursor.Start)
            for _ in range(block):
                cur.movePosition(QTextCursor.NextBlock)
            cur.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor,
                             min(self._screen.cursor.x, COLS - 1))
            self.setTextCursor(cur)

    def _context_menu(self, pos):
        menu = QMenu(self)
        act_copy = QAction("Copy", self)
        act_copy.setEnabled(self.textCursor().hasSelection())
        act_copy.triggered.connect(self.copy)
        act_paste = QAction("Paste", self)
        act_paste.setEnabled(self._attached and bool(QApplication.clipboard().text()))
        act_paste.triggered.connect(self._paste_clipboard)
        act_select = QAction("Select all", self)
        act_select.triggered.connect(self.selectAll)
        menu.addAction(act_copy)
        menu.addAction(act_paste)
        menu.addSeparator()
        menu.addAction(act_select)
        menu.exec_(self.mapToGlobal(pos))

    def _paste_clipboard(self):
        if not self._attached:
            return
        text = QApplication.clipboard().text()
        if text:
            # Normalize Windows newlines for remote PTY
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            self.bytes_out.emit(text.encode("utf-8", "ignore"))

    def keyPressEvent(self, event: QKeyEvent):
        if not self._attached:
            return super().keyPressEvent(event)

        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)

        # Ctrl+Shift+C / Ctrl+Insert → copy
        if (ctrl and shift and key == Qt.Key_C) or (ctrl and key == Qt.Key_Insert):
            if self.textCursor().hasSelection():
                self.copy()
            event.accept()
            return

        # Ctrl+Shift+V / Shift+Insert → paste to remote
        if (ctrl and shift and key == Qt.Key_V) or (shift and key == Qt.Key_Insert):
            self._paste_clipboard()
            event.accept()
            return

        # Ctrl+C: copy if selection, else remote interrupt
        if ctrl and key == Qt.Key_C and not shift:
            if self.textCursor().hasSelection():
                self.copy()
                event.accept()
                return
            self.bytes_out.emit(b"\x03")
            event.accept()
            return

        # Ctrl+V without shift: paste (also common on Windows)
        if ctrl and key == Qt.Key_V and not shift:
            self._paste_clipboard()
            event.accept()
            return

        # Let standard copy shortcut work when not attached-handling
        if event.matches(QKeySequence.Copy):
            self.copy()
            event.accept()
            return
        if event.matches(QKeySequence.Paste):
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
        key = event.key()
        mods = event.modifiers()
        text = event.text()

        if mods & Qt.ControlModifier:
            # Already handled C/V above
            if Qt.Key_A <= key <= Qt.Key_Z:
                return bytes([key - Qt.Key_A + 1])
            if key == Qt.Key_Space:
                return b"\x00"
            return None

        mapping = {
            Qt.Key_Return: b"\r",
            Qt.Key_Enter: b"\r",
            Qt.Key_Backspace: b"\x7f",
            Qt.Key_Tab: b"\t",
            Qt.Key_Escape: b"\x1b",
            Qt.Key_Up: b"\x1b[A",
            Qt.Key_Down: b"\x1b[B",
            Qt.Key_Right: b"\x1b[C",
            Qt.Key_Left: b"\x1b[D",
            Qt.Key_Home: b"\x1b[H",
            Qt.Key_End: b"\x1b[F",
            Qt.Key_Delete: b"\x1b[3~",
            Qt.Key_PageUp: b"\x1b[5~",
            Qt.Key_PageDown: b"\x1b[6~",
        }
        if key in mapping:
            return mapping[key]
        if text:
            return text.encode("utf-8", "ignore")
        return None
