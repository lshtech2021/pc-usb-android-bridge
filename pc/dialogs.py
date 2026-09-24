"""Dialogs that hold configuration the main window does not need on screen.

The connection form lives here rather than in the window so the terminal gets
the vertical space instead.
"""
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QComboBox, QDialog, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout)

import theme


class ConnectionDialog(QDialog):
    """Device, token, PC name and fingerprint, plus connect/disconnect.

    This is a view: the window owns the client and reacts to the signals.
    """

    connect_requested = pyqtSignal()
    disconnect_requested = pyqtSignal()
    refresh_requested = pyqtSignal()
    device_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection")
        self.setModal(False)
        self.setMinimumWidth(460)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACE_L, theme.SPACE_L,
                                 theme.SPACE_L, theme.SPACE_L)
        outer.setSpacing(theme.SPACE_M)

        form = QGridLayout()
        form.setHorizontalSpacing(theme.SPACE_S)
        form.setVerticalSpacing(theme.SPACE_S)
        form.setColumnStretch(1, 1)

        self.cmb_device = QComboBox()
        self.cmb_device.setToolTip("USB device reported by adb")
        self.cmb_device.currentIndexChanged.connect(
            lambda _=0: self.device_changed.emit())
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setToolTip("Re-scan adb for connected devices (F5)")
        self.btn_refresh.clicked.connect(self.refresh_requested)
        device_row = QHBoxLayout()
        device_row.setSpacing(theme.SPACE_S)
        device_row.addWidget(self.cmb_device, 1)
        device_row.addWidget(self.btn_refresh)

        self.ed_token = QLineEdit()
        self.ed_token.setPlaceholderText("8 hex digits shown on the phone")
        self.ed_token.setEchoMode(QLineEdit.Password)
        self.btn_show = QPushButton("Show")
        self.btn_show.setCheckable(True)
        self.btn_show.setFixedWidth(58)
        self.btn_show.setToolTip("Reveal the token")
        self.btn_show.toggled.connect(self._toggle_token)
        token_row = QHBoxLayout()
        token_row.setSpacing(theme.SPACE_S)
        token_row.addWidget(self.ed_token, 1)
        token_row.addWidget(self.btn_show)

        self.ed_pc_name = QLineEdit()
        self.ed_pc_name.setPlaceholderText("PC display name")
        self.ed_pc_name.setToolTip("Name shown in the phone's Approve PC dialog")

        self.lbl_fp = QLabel()
        self.lbl_fp.setProperty("role", "mono")
        self.btn_copy_fp = QPushButton("Copy")
        self.btn_copy_fp.setProperty("variant", "subtle")
        self.btn_copy_fp.setToolTip("Copy the full PC fingerprint")
        self.btn_copy_fp.clicked.connect(self._copy_fingerprint)
        fp_row = QHBoxLayout()
        fp_row.setSpacing(theme.SPACE_S)
        fp_row.addWidget(self.lbl_fp)
        fp_row.addWidget(self.btn_copy_fp)
        fp_row.addStretch(1)

        self.lbl_fp_note = theme.caption(
            "Private key is never shown. The phone asks you to approve this PC "
            "the first time it connects.")
        self.lbl_fp_note.setWordWrap(True)

        form.addWidget(theme.field_label("Device"), 0, 0)
        form.addLayout(device_row, 0, 1)
        form.addWidget(theme.field_label("Token"), 1, 0)
        form.addLayout(token_row, 1, 1)
        form.addWidget(theme.field_label("PC name"), 2, 0)
        form.addWidget(self.ed_pc_name, 2, 1)
        form.addWidget(theme.field_label("Fingerprint"), 3, 0)
        form.addLayout(fp_row, 3, 1)
        form.addWidget(self.lbl_fp_note, 4, 1)
        outer.addLayout(form)

        self.pill = theme.connection_pill()
        outer.addWidget(self.pill, 0, Qt.AlignLeft)

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.SPACE_S)
        buttons.addStretch(1)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setProperty("variant", "primary")
        self.btn_connect.setToolTip("Forward the USB port and handshake with the phone")
        self.btn_connect.clicked.connect(self.connect_requested)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self.disconnect_requested)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        buttons.addWidget(self.btn_connect)
        buttons.addWidget(self.btn_disconnect)
        buttons.addWidget(self.btn_close)
        outer.addLayout(buttons)

    # ---- State pushed by the window ----
    def set_devices(self, devices):
        """devices: list of (label, serial). Keeps the current selection."""
        current = self.selected_serial()
        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()
        for label, serial in devices:
            self.cmb_device.addItem(label, serial)
        self.cmb_device.blockSignals(False)
        if current:
            index = self.cmb_device.findData(current)
            if index >= 0:
                self.cmb_device.setCurrentIndex(index)

    def select_serial(self, serial):
        if not serial:
            return
        index = self.cmb_device.findData(serial)
        if index >= 0:
            self.cmb_device.setCurrentIndex(index)

    def selected_serial(self):
        return self.cmb_device.currentData()

    def device_label(self):
        return self.cmb_device.currentText()

    def token(self) -> str:
        return self.ed_token.text().strip()

    def pc_name(self) -> str:
        return self.ed_pc_name.text().strip()

    def set_pc_name(self, name: str):
        self.ed_pc_name.setText(name)

    def set_identity(self, short_id: str, full_id: str):
        self.lbl_fp.setText(f"{short_id}…")
        self.lbl_fp.setToolTip(full_id)

    def set_state(self, state: str, detail: str = ""):
        self.pill.set_state(state, detail)
        self.btn_connect.setEnabled(state in ("disconnected", "error"))
        self.btn_disconnect.setEnabled(state in ("connecting", "connected"))

    def apply_theme(self):
        self.pill.apply()

    # ---- Local behaviour ----
    def _toggle_token(self, shown: bool):
        self.ed_token.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        self.btn_show.setText("Hide" if shown else "Show")

    def _copy_fingerprint(self):
        QApplication.clipboard().setText(self.lbl_fp.toolTip())
        self.btn_copy_fp.setText("Copied")
        QTimer.singleShot(1200, lambda: self.btn_copy_fp.setText("Copy"))

    def present(self):
        self.show()
        self.raise_()
        self.activateWindow()
