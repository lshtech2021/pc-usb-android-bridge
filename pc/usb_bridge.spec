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
    "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngine",
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets",
    "PyQt5.Qt3DCore", "PyQt5.Qt3DRender", "PyQt5.Qt3DAnimation", "PyQt5.Qt3DExtras",
    "PyQt5.QtBluetooth", "PyQt5.QtNfc", "PyQt5.QtPositioning", "PyQt5.QtLocation",
    "PyQt5.QtSensors", "PyQt5.QtSerialPort", "PyQt5.QtWebSockets", "PyQt5.QtWebChannel",
    "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtDesigner", "PyQt5.QtHelp",
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
