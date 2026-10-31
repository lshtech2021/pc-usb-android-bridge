"""Translate Qt key events into VT/xterm bytes for the remote PTY.

The encoder understands normal and application cursor modes (DECCKM), the
common xterm modifier encoding, Ctrl/Alt combinations, F1-F12, and AltGr. It
does not handle widget shortcuts such as Ctrl+Shift+C/V; the widget checks
those before calling here.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt

from terminal_model import private_mode

# xterm modifier parameter: 1 + Shift(1) + Alt(2) + Ctrl(4)
_SHIFT = Qt.KeyboardModifier.ShiftModifier
_CTRL = Qt.KeyboardModifier.ControlModifier
_ALT = Qt.KeyboardModifier.AltModifier
_ALTGR = Qt.KeyboardModifier.GroupSwitchModifier

_SPECIAL_KEYS = {
    Qt.Key.Key_Shift,
    Qt.Key.Key_Control,
    Qt.Key.Key_Alt,
    Qt.Key.Key_AltGr,
    Qt.Key.Key_Meta,
    Qt.Key.Key_CapsLock,
    Qt.Key.Key_NumLock,
    Qt.Key.Key_ScrollLock,
}

_CURSOR = {
    Qt.Key.Key_Up: ("A", "A"),
    Qt.Key.Key_Down: ("B", "B"),
    Qt.Key.Key_Right: ("C", "C"),
    Qt.Key.Key_Left: ("D", "D"),
}

_FUNCTION = {
    Qt.Key.Key_F1: ("P", None, "P"),
    Qt.Key.Key_F2: ("Q", None, "Q"),
    Qt.Key.Key_F3: ("R", None, "R"),
    Qt.Key.Key_F4: ("S", None, "S"),
    Qt.Key.Key_F5: ("15~", 15, None),
    Qt.Key.Key_F6: ("17~", 17, None),
    Qt.Key.Key_F7: ("18~", 18, None),
    Qt.Key.Key_F8: ("19~", 19, None),
    Qt.Key.Key_F9: ("20~", 20, None),
    Qt.Key.Key_F10: ("21~", 21, None),
    Qt.Key.Key_F11: ("23~", 23, None),
    Qt.Key.Key_F12: ("24~", 24, None),
}

_TILDE = {
    Qt.Key.Key_Insert: 2,
    Qt.Key.Key_Delete: 3,
    Qt.Key.Key_PageUp: 5,
    Qt.Key.Key_PageDown: 6,
}


def modifier_parameter(modifiers: Qt.KeyboardModifier) -> int:
    value = 1
    if modifiers & _SHIFT:
        value += 1
    if modifiers & _ALT:
        value += 2
    if modifiers & _CTRL:
        value += 4
    return value


def _csi(code: str, param: int) -> bytes:
    if param > 1:
        return f"\x1b[{code};{param}".encode("ascii")
    return f"\x1b[{code}".encode("ascii")


class KeyEncoder:
    """Encode one key press for the currently active pyte screen."""

    def __init__(self, screen):
        self.screen = screen

    def encode(self, key: int, modifiers: Qt.KeyboardModifier, text: str = "") -> bytes | None:
        if key in _SPECIAL_KEYS:
            return None

        altgr = bool(modifiers & _ALTGR)
        ctrl = bool(modifiers & _CTRL) and not altgr
        alt = bool(modifiers & _ALT) and not altgr
        shift = bool(modifiers & _SHIFT)
        param = modifier_parameter(modifiers)

        # AltGr produces a text character (e.g. @, €, ł); never ESC-prefix it.
        if altgr:
            return text.encode("utf-8") if text else None

        # Ctrl+letter and the standard control punctuation.
        if ctrl:
            control = self._control_key(key, text)
            if control is not None:
                return (b"\x1b" if alt else b"") + control

        # Cursor keys: SS3 in application mode, CSI otherwise. Modifiers use
        # the CSI 1;<m> form even in application mode (xterm behaviour).
        if key in _CURSOR:
            normal, application = _CURSOR[key]
            if param > 1:
                return f"\x1b[1;{param}{normal}".encode("ascii")
            if private_mode(self.screen, 1):
                return f"\x1bO{application}".encode("ascii")
            return f"\x1b[{normal}".encode("ascii")

        if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            letter = "H" if key == Qt.Key.Key_Home else "F"
            if param > 1:
                return f"\x1b[1;{param}{letter}".encode("ascii")
            return f"\x1b[{letter}".encode("ascii")

        if key in _TILDE:
            number = _TILDE[key]
            if param > 1:
                return f"\x1b[{number};{param}~".encode("ascii")
            return f"\x1b[{number}~".encode("ascii")

        if key in _FUNCTION:
            normal, tilde_code, ss3 = _FUNCTION[key]
            if tilde_code is not None:
                if param > 1:
                    return f"\x1b[{tilde_code};{param}~".encode("ascii")
                return f"\x1b[{normal}".encode("ascii")
            if param > 1:
                return f"\x1b[1;{param}{normal}".encode("ascii")
            return f"\x1bO{normal}".encode("ascii")

        if key == Qt.Key.Key_Tab:
            seq = b"\x1b[Z" if shift else b"\t"
            return (b"\x1b" if alt else b"") + seq
        if key == Qt.Key.Key_Backtab:
            return b"\x1b[Z"
        if key == Qt.Key.Key_Backspace:
            seq = b"\x08" if ctrl else b"\x7f"
            return (b"\x1b" if alt else b"") + seq
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return (b"\x1b" if alt else b"") + b"\r"
        if key == Qt.Key.Key_Escape:
            return b"\x1b\x1b" if alt else b"\x1b"

        if text:
            if ctrl:
                return None
            if alt:
                return b"\x1b" + text.encode("utf-8")
            return text.encode("utf-8")

        # Fall back to a printable ASCII mapping for synthetic events that
        # omit text (tests and some input methods).
        if Qt.Key.Key_A.value <= key <= Qt.Key.Key_Z.value:
            ch = chr(key)
            return ch.lower() if not shift else ch
        if Qt.Key.Key_0.value <= key <= Qt.Key.Key_9.value:
            return chr(key).encode("ascii")
        return None

    @staticmethod
    def _control_key(key: int, text: str) -> bytes | None:
        if key in (Qt.Key.Key_Space, Qt.Key.Key_At):
            return b"\x00"
        if Qt.Key.Key_A.value <= key <= Qt.Key.Key_Z.value:
            return bytes([key - Qt.Key.Key_A.value + 1])
        special = {
            Qt.Key.Key_BracketLeft: b"\x1b",
            Qt.Key.Key_Backslash: b"\x1c",
            Qt.Key.Key_BracketRight: b"\x1d",
            Qt.Key.Key_AsciiCircum: b"\x1e",
            Qt.Key.Key_Underscore: b"\x1f",
            Qt.Key.Key_Minus: b"\x1f",
            Qt.Key.Key_Slash: b"\x1f",
        }
        if key in special:
            return special[key]
        # Ctrl+digit has no portable meaning; let the caller fall through.
        return None
