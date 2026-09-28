"""QSettings-backed persistence for window geometry and preferences."""
from PyQt5.QtCore import QSettings

ORG = "USBBridge"
APP = "PCClient"

KEY_GEOMETRY = "window/geometry"
KEY_TAB = "window/tab"
KEY_DEVICE = "connection/device"
KEY_ADB_PATH = "connection/adb_path"
KEY_PALETTE = "appearance/palette"
KEY_FONT_SIZE = "terminal/font_size"
KEY_TERMINAL_THEME = "terminal/theme"
KEY_PROXY_ENABLED = "proxy/enabled"
KEY_PROXY_PORT = "proxy/port"


def store() -> QSettings:
    return QSettings(ORG, APP)


def get(key: str, default=None):
    return store().value(key, default)


def put(key: str, value):
    store().setValue(key, value)


def sync():
    store().sync()
