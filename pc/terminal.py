"""Interactive VT100 terminal widget backed by pyte (120x30 to match phone PTY)."""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont, QKeyEvent, QTextCursor, QColor, QBrush
from PyQt5.QtWidgets import QPlainTextEdit

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
        lines = []
        for y in range(self._screen.lines):
            row = self._screen.buffer[y]
            lines.append("".join(row[x].data for x in range(self._screen.columns)))
        text = "\n".join(lines)
        self.setPlainText(text)
        # Place cursor
        cur = self.textCursor()
        block = min(self._screen.cursor.y, self.blockCount() - 1)
        cur.movePosition(QTextCursor.Start)
        for _ in range(block):
            cur.movePosition(QTextCursor.NextBlock)
        cur.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor,
                         min(self._screen.cursor.x, COLS - 1))
        self.setTextCursor(cur)

    def keyPressEvent(self, event: QKeyEvent):
        if not self._attached:
            return super().keyPressEvent(event)
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
