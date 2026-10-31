# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the USB Bridge PC client.

Build from this directory:

    pip install -r requirements.txt pyinstaller
    pyinstaller usb_bridge.spec          # -> dist/USBBridge/  (ship the whole folder)

The app has no data files to bundle: the stylesheet, palette and icons are all
generated in code, so only the Python modules need collecting.

Not bundled (kept as runtime dependencies):
  * `adb` from Android platform-tools; the client shells out to it and expects it
    on PATH or configured via Adb(path=...).
  * The PC identity, read/written at %USERPROFILE%\\.usbbridge\\identity.json.
"""

NAME = "USBBridge"

# Qt ships far more bindings than this GUI uses; dropping the heavy unused ones
# shrinks the bundle without touching anything the client imports (ui.py pulls in
# QtCore / QtGui / QtWidgets only).
excludes = [
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuickWidgets",
    "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
    "PyQt6.Qt3DCore", "PyQt6.Qt3DRender", "PyQt6.Qt3DAnimation", "PyQt6.Qt3DExtras",
    "PyQt6.QtBluetooth", "PyQt6.QtNfc", "PyQt6.QtPositioning", "PyQt6.QtLocation",
    "PyQt6.QtSensors", "PyQt6.QtSerialPort", "PyQt6.QtWebSockets", "PyQt6.QtWebChannel",
    "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtHelp",
    "tkinter", "matplotlib", "numpy", "scipy", "pandas", "PIL",
]

a = Analysis(
    ["ui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                      # GUI app: never open a console window
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=NAME,
)
