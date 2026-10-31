"""Offscreen tests for the native Qt terminal view."""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QInputMethodEvent, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication

import theme
from terminal_view import TerminalWidget

_APP = QApplication.instance() or QApplication([])
_TMP = tempfile.TemporaryDirectory(prefix="usbbridge-termview-")
os.environ["USBBRIDGE_HOME"] = _TMP.name


def make_widget():
    widget = TerminalWidget()
    widget.resize(900, 400)
    widget.show()
    widget.begin_session("test")
    widget.set_attached(True)
    _APP.processEvents()
    return widget


def feed(widget, data: bytes):
    widget.feed(data)
    while widget._pending:
        widget._drain_pending()
    _APP.processEvents()


def drain(widget):
    while widget._pending:
        widget._drain_pending()
    _APP.processEvents()


def key(widget, code, mods=Qt.KeyboardModifier.NoModifier, text=""):
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, code, mods, text))
    _APP.processEvents()


def mouse(widget, typ, x, y, button=Qt.MouseButton.LeftButton,
          buttons=None, mods=Qt.KeyboardModifier.NoModifier):
    if buttons is None:
        buttons = button
    pos = QPointF(x, y)
    event = QMouseEvent(typ, pos, pos, pos, button, buttons, mods)
    if typ == QEvent.Type.MouseButtonPress:
        widget.mousePressEvent(event)
    elif typ == QEvent.Type.MouseMove:
        widget.mouseMoveEvent(event)
    else:
        widget.mouseReleaseEvent(event)
    _APP.processEvents()


def test_alt_screen():
    w = make_widget()
    feed(w, b"NORMAL\r\n")
    feed(w, b"\x1b[?1049hALT-SCREEN\x1b[?1049l")
    assert w._total_lines() == 30
    text = "".join(r.text for r in w._lines(0, 1)[0])
    assert text.startswith("NORMAL")
    feed(w, b"\x1b[?1049h")
    assert w._total_lines() == 30
    assert "".join(r.text for r in w._lines(0, 1)[0]).startswith("")
    feed(w, b"\x1b[?1049l")
    w.close()


def test_colours_render():
    w = make_widget()
    feed(w, b"\x1b[31mRED\x1b[0m")
    runs = w._lines(0, 1)[0]
    red = [r for r in runs if r.text.startswith("RED")][0]
    fg, _bg = w._style_colors(red.style)
    assert fg.name().lower() == theme.terminal_color("red").lower()
    img = w.grab().toImage()
    assert not img.isNull()
    w.close()


def test_selection_and_clipboard():
    w = make_widget()
    feed(w, b"hello world\r\nsecond")
    w._sel_anchor = (0, 0)
    w._sel_cursor = (1, 5)
    assert w._selection_text() == "hello world\nsecond"
    w.copy()
    assert QApplication.clipboard().text() == "hello world\nsecond"
    w.close()


def test_ctrl_c_copy_vs_interrupt():
    w = make_widget()
    feed(w, b"hello")
    out = []
    w.bytes_out.connect(out.append)
    w._sel_anchor = w._sel_cursor = None
    key(w, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert out == [b"\x03"]
    w._sel_anchor = (0, 0)
    w._sel_cursor = (0, 4)
    out.clear()
    key(w, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert out == []
    assert QApplication.clipboard().text() == "hello"
    w.close()


def test_bracketed_paste():
    w = make_widget()
    feed(w, b"\x1b[?2004h")
    QApplication.clipboard().setText("a\nb")
    out = []
    w.bytes_out.connect(out.append)
    w._paste_clipboard()
    assert out == [b"\x1b[200~a\nb\x1b[201~"]
    w.close()


def test_key_modes_after_queued_output():
    w = make_widget()
    out = []
    w.bytes_out.connect(out.append)
    w.feed(b"\x1b[?1h")           # queued, not drained by the timer
    key(w, Qt.Key.Key_Up)
    assert out == [b"\x1bOA"]
    feed(w, b"\x1b[?1l")
    key(w, Qt.Key.Key_Up)
    assert out[-1] == b"\x1b[A"
    w.close()


def test_mouse_reporting_sgr():
    w = make_widget()
    w.resize(900, 600)          # tall enough to show all 30 PTY rows
    _APP.processEvents()
    feed(w, b"\x1b[?1000h\x1b[?1006h")
    out = []
    w.bytes_out.connect(out.append)
    x = w._cell_w * 2 + 1
    y = w._cell_h * 1 + 1
    row = w.verticalScrollBar().value() + 1
    mouse(w, QEvent.Type.MouseButtonPress, x, y)
    assert out[-1] == f"\x1b[<0;3;{row + 1}M".encode()
    mouse(w, QEvent.Type.MouseButtonRelease, x, y)
    assert out[-1] == f"\x1b[<0;3;{row + 1}m".encode()
    w.close()


def test_scroll_follow():
    w = make_widget()
    feed(w, b"line\r\n" * 100)
    vbar = w.verticalScrollBar()
    assert w._follow
    assert vbar.value() == vbar.maximum() > 0
    w._scroll_lines(-10)
    assert not w._follow
    top = vbar.value()
    feed(w, b"more\r\n")
    assert vbar.value() == top
    w._scroll_lines(10000)
    assert w._follow
    assert vbar.value() == vbar.maximum()
    w.close()


def test_ime_commit():
    w = make_widget()
    out = []
    w.bytes_out.connect(out.append)
    event = QInputMethodEvent()
    event.setCommitString("中文")
    w.inputMethodEvent(event)
    assert out == ["中文".encode("utf-8")]
    rect = w.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
    assert isinstance(rect, QRect)
    w.close()


def test_scrollback_spill_and_slice():
    w = make_widget()
    data = b"".join(b"line %d\r\n" % i for i in range(2500))
    feed(w, data)
    assert len(w._store) > 0
    assert w._total_lines() >= 2500
    first = "".join(r.text for r in w._lines(0, 1)[0]).rstrip()
    assert first == "line 0"
    mid = "".join(r.text for r in w._lines(600, 601)[0]).rstrip()
    assert mid == "line 600"
    w._scroll_lines(-100000)
    assert not w._follow
    assert w.verticalScrollBar().value() == 0
    w._scroll_lines(100000)
    assert w._follow
    w.close()


def test_context_menu_actions():
    import terminal_view
    w = make_widget()
    feed(w, b"hello")
    w._sel_anchor = (0, 0)
    w._sel_cursor = (0, 2)
    original = terminal_view.QMenu.exec
    terminal_view.QMenu.exec = lambda self, *a, **k: None
    try:
        w._context_menu(QPoint(0, 0))
    finally:
        terminal_view.QMenu.exec = original
    w.close()


def test_zoom_and_theme():
    w = make_widget()
    size = w.font_size()
    w.zoom(1)
    assert w.font_size() == size + 1
    w.reset_zoom()
    assert w.font_size() == 11
    for name in ("dark", "light"):
        theme.set_terminal_theme(name)
        w.apply_theme()
        feed(w, b"\x1b[32mGREEN\x1b[0m")
    w.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("terminal view tests passed")
