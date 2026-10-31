"""Headless tests for the Qt key -> VT byte encoder."""
from PyQt6.QtCore import Qt

from terminal_keys import KeyEncoder, modifier_parameter


class FakeScreen:
    def __init__(self, modes=()):
        self.mode = set(modes)


M = Qt.KeyboardModifier


def enc(key, mods=M.NoModifier, text="", modes=()):
    return KeyEncoder(FakeScreen(modes)).encode(key, mods, text)


def test_modifier_parameter():
    assert modifier_parameter(M.NoModifier) == 1
    assert modifier_parameter(M.ShiftModifier) == 2
    assert modifier_parameter(M.AltModifier) == 3
    assert modifier_parameter(M.ControlModifier) == 5
    assert modifier_parameter(M.ControlModifier | M.ShiftModifier) == 6


def test_cursor_keys():
    assert enc(Qt.Key.Key_Up) == b"\x1b[A"
    assert enc(Qt.Key.Key_Down) == b"\x1b[B"
    assert enc(Qt.Key.Key_Right) == b"\x1b[C"
    assert enc(Qt.Key.Key_Left) == b"\x1b[D"
    assert enc(Qt.Key.Key_Up, modes=(1 << 5,)) == b"\x1bOA"
    assert enc(Qt.Key.Key_Down, modes=(1 << 5,)) == b"\x1bOB"
    assert enc(Qt.Key.Key_Up, M.ControlModifier) == b"\x1b[1;5A"
    assert enc(Qt.Key.Key_Up, M.ShiftModifier) == b"\x1b[1;2A"
    assert enc(Qt.Key.Key_Up, M.AltModifier) == b"\x1b[1;3A"


def test_editing_keys():
    assert enc(Qt.Key.Key_Home) == b"\x1b[H"
    assert enc(Qt.Key.Key_End) == b"\x1b[F"
    assert enc(Qt.Key.Key_Home, M.ControlModifier) == b"\x1b[1;5H"
    assert enc(Qt.Key.Key_Insert) == b"\x1b[2~"
    assert enc(Qt.Key.Key_Delete) == b"\x1b[3~"
    assert enc(Qt.Key.Key_PageUp) == b"\x1b[5~"
    assert enc(Qt.Key.Key_PageDown) == b"\x1b[6~"
    assert enc(Qt.Key.Key_Delete, M.ControlModifier) == b"\x1b[3;5~"


def test_function_keys():
    assert enc(Qt.Key.Key_F1) == b"\x1bOP"
    assert enc(Qt.Key.Key_F4) == b"\x1bOS"
    assert enc(Qt.Key.Key_F5) == b"\x1b[15~"
    assert enc(Qt.Key.Key_F12) == b"\x1b[24~"
    assert enc(Qt.Key.Key_F1, M.ControlModifier) == b"\x1b[1;5P"
    assert enc(Qt.Key.Key_F5, M.ControlModifier) == b"\x1b[15;5~"


def test_control_and_alt():
    assert enc(Qt.Key.Key_C, M.ControlModifier) == b"\x03"
    assert enc(Qt.Key.Key_D, M.ControlModifier) == b"\x04"
    assert enc(Qt.Key.Key_Space, M.ControlModifier) == b"\x00"
    assert enc(Qt.Key.Key_BracketLeft, M.ControlModifier) == b"\x1b"
    assert enc(Qt.Key.Key_Backslash, M.ControlModifier) == b"\x1c"
    assert enc(Qt.Key.Key_X, M.AltModifier, "x") == b"\x1bx"
    assert enc(Qt.Key.Key_C, M.ControlModifier | M.AltModifier) == b"\x1b\x03"
    assert enc(Qt.Key.Key_X, M.AltModifier, "x") == b"\x1bx"


def test_text_keys():
    assert enc(Qt.Key.Key_Tab, M.NoModifier, "\t") == b"\t"
    assert enc(Qt.Key.Key_Tab, M.ShiftModifier, "\t") == b"\x1b[Z"
    assert enc(Qt.Key.Key_Backspace) == b"\x7f"
    assert enc(Qt.Key.Key_Backspace, M.ControlModifier) == b"\x08"
    assert enc(Qt.Key.Key_Return) == b"\r"
    assert enc(Qt.Key.Key_Enter) == b"\r"
    assert enc(Qt.Key.Key_A, M.NoModifier, "a") == b"a"
    assert enc(Qt.Key.Key_A, M.ShiftModifier, "A") == b"A"
    assert "中".encode("utf-8") == enc(Qt.Key.Key_unknown, M.NoModifier, "中")


def test_altgr_is_text_not_alt():
    # Windows reports AltGr as Ctrl+Alt; the group-switch flag must win.
    mods = M.GroupSwitchModifier | M.ControlModifier | M.AltModifier
    assert enc(Qt.Key.Key_At, mods, "@") == b"@"
    assert enc(Qt.Key.Key_E, mods, "€") == "€".encode("utf-8")


def test_unknown_returns_none():
    assert enc(Qt.Key.Key_unknown) is None
    assert enc(Qt.Key.Key_Shift) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("terminal key tests passed")
