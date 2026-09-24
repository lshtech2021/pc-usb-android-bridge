"""Composite widgets shared by the main window.

Kept out of ui.py so the window layout reads as layout, not as widget plumbing.
"""
import os
import subprocess
import sys
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QHeaderView, QLabel,
                             QScrollArea, QTableWidget, QVBoxLayout, QWidget)

from theme import BUBBLE_MAX_FRACTION, SPACE_M, SPACE_S, SPACE_XS


class MessageLog(QScrollArea):
    """Chat-style transcript: one bubble per message, empty state when there is none."""

    EMPTY_TEXT = "No messages yet — connect a phone, then type below."

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("messageLog")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._body = QWidget()
        self._body.setObjectName("messageBody")
        self._lay = QVBoxLayout(self._body)
        self._lay.setContentsMargins(SPACE_M, SPACE_M, SPACE_M, SPACE_M)
        self._lay.setSpacing(SPACE_S)

        self._empty = QLabel(self.EMPTY_TEXT)
        self._empty.setProperty("role", "emptyState")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._lay.addWidget(self._empty)
        self._lay.addStretch(1)
        self.setWidget(self._body)

        self._items = []        # (text, from_me, at) for the transcript
        self._bodies = []       # QLabels whose max width tracks the viewport

    # ---- Content ----
    def add_message(self, text: str, from_me: bool, at: float | None = None):
        at = time.time() if at is None else at
        self._empty.setVisible(False)

        bubble = QFrame()
        bubble.setObjectName("bubbleMine" if from_me else "bubbleTheirs")
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(SPACE_M, SPACE_S, SPACE_M, SPACE_XS)
        bv.setSpacing(2)

        body = QLabel(text)
        body.setObjectName("bubbleText")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setMaximumWidth(self._max_bubble_width())
        self._bodies.append(body)

        stamp = QLabel(time.strftime("%H:%M", time.localtime(at)))
        stamp.setObjectName("bubbleTime")
        stamp.setAlignment(Qt.AlignRight)

        bv.addWidget(body)
        bv.addWidget(stamp)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        if from_me:                     # own messages sit on the right
            h.addStretch(1)
            h.addWidget(bubble)
        else:
            h.addWidget(bubble)
            h.addStretch(1)

        stick = self._at_bottom()
        self._lay.insertWidget(self._lay.count() - 1, row)
        self._items.append((text, from_me, at))
        if stick:
            self._scroll_to_end()

    def add_notice(self, text: str):
        """System line (connect/disconnect/error): centred and muted, not a chat bubble."""
        self._empty.setVisible(False)
        label = QLabel(text)
        label.setProperty("role", "emptyState")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        stick = self._at_bottom()
        self._lay.insertWidget(self._lay.count() - 1, label)
        if stick:
            self._scroll_to_end()

    def transcript(self) -> str:
        """Plain-text rendering of the whole log, for Copy all."""
        return "\n".join(
            f"[{'PC' if from_me else 'Phone'}] {time.strftime('%H:%M', time.localtime(at))}  {text}"
            for text, from_me, at in self._items)

    def clear(self):
        for i in range(self._lay.count() - 2, 0, -1):
            item = self._lay.itemAt(i)
            w = item.widget() if item else None
            if w is not None and w is not self._empty:
                w.setParent(None)
        self._items.clear()
        self._bodies.clear()
        self._empty.setVisible(True)

    # ---- Layout helpers ----
    def _max_bubble_width(self) -> int:
        return max(220, int(self.viewport().width() * BUBBLE_MAX_FRACTION))

    def _at_bottom(self) -> bool:
        sb = self.verticalScrollBar()
        return sb.value() >= sb.maximum() - 8

    def _scroll_to_end(self):
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self._max_bubble_width()
        for body in self._bodies:
            body.setMaximumWidth(width)


class FileDropTable(QTableWidget):
    """Transfer table that also accepts files dragged onto it."""

    files_dropped = pyqtSignal(list)

    COLUMNS = ["File", "Direction", "Progress", "Status", "Action"]
    EMPTY_TEXT = ("Drag files here to send to the phone"
                  "<br><span style='color:#9096A1'>or use Add files…</span>")

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLUMNS), parent)
        self.setObjectName("fileTable")
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setShowGrid(False)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setHorizontalHeaderLabels(self.COLUMNS)

        head = self.horizontalHeader()
        head.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # Only the name column absorbs slack; two stretched columns would split it
        # evenly and leave the progress/status cells stranded mid-table.
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.Fixed)
        head.setSectionResizeMode(3, QHeaderView.Interactive)
        head.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.setColumnWidth(2, 130)
        self.setColumnWidth(3, 300)

        self._empty = QLabel(self.EMPTY_TEXT, self.viewport())
        self._empty.setObjectName("fileEmpty")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.refresh_empty_state()

    # ---- Empty state ----
    def refresh_empty_state(self):
        self._empty.setVisible(self.rowCount() == 0)
        self._place_empty()

    def _place_empty(self):
        self._empty.setGeometry(0, 0, self.viewport().width(), self.viewport().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_empty()

    # ---- Drag and drop ----
    def _acceptable(self, event) -> bool:
        return event.mimeData().hasUrls()

    def dragEnterEvent(self, event):
        if self._acceptable(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._acceptable(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            local = url.toLocalFile()
            if os.path.isdir(local):
                for root, _dirs, files in os.walk(local):
                    paths.extend(os.path.join(root, f) for f in files)
            elif os.path.isfile(local):
                paths.append(local)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()


def open_in_file_manager(path: str):
    """Show path in the OS file manager, selecting it when it is a file."""
    path = os.path.abspath(path)
    is_file = os.path.isfile(path)
    try:
        if sys.platform.startswith("win"):
            if is_file:
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)          # noqa: S606 - opening a folder is the intent
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if is_file else ["open", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if is_file else path])
    except Exception:
        pass
