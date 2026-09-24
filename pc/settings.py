"""QSettings-backed persistence for window geometry and preferences."""
from PyQt5.QtCore import QSettings

ORG = "USBBridge"
APP = "PCClient"

KEY_GEOMETRY = "window/geometry"
KEY_TAB = "window/tab"
KEY_DEVICE = "connection/device"
KEY_PALETTE = "appearance/palette"
KEY_FONT_SIZE = "terminal/font_size"


def store() -> QSettings:
    return QSettings(ORG, APP)


def get(key: str, default=None):
    return store().value(key, default)


def put(key: str, value):
    store().setValue(key, value)


def sync():
    store().sync()
