"""PyQt5 UI: three tabs corresponding to the three features (messages / files / remote terminal).

Run: py ui.py
"""
import sys

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QComboBox, QPushButton, QLabel, QTabWidget, QPlainTextEdit,
    QLineEdit, QProgressBar, QFileDialog, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox)

from adb_manager import Adb
from client import PhoneClient

PC_PORT, PHONE_PORT = 12580, 9999


class MainWindow(QMainWindow):
    sig_text = pyqtSignal(str)
    sig_status = pyqtSignal(str)
    sig_fprog = pyqtSignal(object, str, int, int, str)
    sig_fdone = pyqtSignal(object, str, bool, str, str)
    sig_rout = pyqtSignal(object, str, object)
    sig_rclose = pyqtSignal(object, int, str)
    sig_rerr = pyqtSignal(object, str, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("USB Bridge Client")
        self.resize(820, 600)
        self.adb, self.client, self.ch = Adb(), PhoneClient(), None
        self._pending = None          # Last remote-open parameters, used to resend after fingerprint confirmation
        self._rows = {}               # fid -> table row index
        c = self.client
        c.on_text = self.sig_text.emit
        c.on_status = self.sig_status.emit
        c.on_hello_ack = lambda i: self.sig_status.emit(f"[Phone connected: {i.get('device','?')}]")
        c.on_file_progress = lambda *a: self.sig_fprog.emit(*a)
        c.on_file_done = lambda *a: self.sig_fdone.emit(*a)
        c.on_remote_output = lambda *a: self.sig_rout.emit(*a)
        c.on_remote_close = lambda *a: self.sig_rclose.emit(*a)
        c.on_remote_error = lambda *a: self.sig_rerr.emit(*a)
        for s, slot in ((self.sig_text, self._on_text), (self.sig_status, self._on_status),
                        (self.sig_fprog, self._on_fprog), (self.sig_fdone, self._on_fdone),
                        (self.sig_rout, self._on_rout), (self.sig_rclose, self._on_rclose),
                        (self.sig_rerr, self._on_rerr)):
            s.connect(slot)
        self._build_ui()

    def _build_ui(self):
        top = QHBoxLayout()
        self.cmb = QComboBox()
        self.ed_token = QLineEdit()
        self.ed_token.setPlaceholderText("Phone token")
        self.ed_token.setFixedWidth(130)
        b1 = QPushButton("Refresh Devices"); b1.clicked.connect(self.refresh)
        b2 = QPushButton("Connect"); b2.clicked.connect(self.connect_phone)
        b3 = QPushButton("Disconnect"); b3.clicked.connect(self.disconnect_phone)
        self.lbl = QLabel("Not connected")
        top.addWidget(QLabel("Device:")); top.addWidget(self.cmb, 1)
        top.addWidget(QLabel("Token:")); top.addWidget(self.ed_token)
        top.addWidget(b1); top.addWidget(b2); top.addWidget(b3); top.addWidget(self.lbl)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._msg_tab(), "Messages")
        self.tabs.addTab(self._file_tab(), "Files")
        self.tabs.addTab(self._term_tab(), "Remote Terminal")
        root = QWidget(); lay = QVBoxLayout(root)
        lay.addLayout(top); lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self.refresh()

    # ---- Tab1 Messages ----
    def _msg_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        self.msg_view = QPlainTextEdit(); self.msg_view.setReadOnly(True)
        h = QHBoxLayout()
        self.msg_input = QLineEdit(); self.msg_input.returnPressed.connect(self.send_text)
        b = QPushButton("Send"); b.clicked.connect(self.send_text)
        h.addWidget(self.msg_input, 1); h.addWidget(b)
        v.addWidget(self.msg_view, 1); v.addLayout(h)
        return w

    def send_text(self):
        t = self.msg_input.text().strip()
        if t and self.client.tp and self.client.tp.alive:
            self.client.send_text(t)
            self.msg_view.appendPlainText(f"[PC] {t}")
            self.msg_input.clear()

    def _on_text(self, t):
        self.msg_view.appendPlainText(f"[Phone] {t}")
        self.tabs.setCurrentIndex(0)

    def _on_status(self, s):
        self.msg_view.appendPlainText(s)

    # ---- Tab2 Files (one row per fid, supports concurrent transfers) ----
    def _file_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        b = QPushButton("Choose a file to send to phone..."); b.clicked.connect(self.pick_send)
        self.ftab = QTableWidget(0, 4)
        self.ftab.setHorizontalHeaderLabels(["File", "Direction", "Progress", "Status"])
        self.ftab.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ftab.verticalHeader().setVisible(False)
        v.addWidget(b); v.addWidget(self.ftab, 1)
        v.addWidget(QLabel("Files sent from the phone are saved in ./downloads/"))
        return w

    def pick_send(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose File")
        if path:
            self.client.send_file(path)

    def _row_for(self, fid, name, direction):
        if fid in self._rows:
            return self._rows[fid]
        r = self.ftab.rowCount(); self.ftab.insertRow(r)
        self.ftab.setItem(r, 0, QTableWidgetItem(name))
        self.ftab.setItem(r, 1, QTableWidgetItem("Send -> Phone" if direction == "up" else "Phone -> PC"))
        bar = QProgressBar(); bar.setValue(0)
        self.ftab.setCellWidget(r, 2, bar)
        self.ftab.setItem(r, 3, QTableWidgetItem("Transferring"))
        self._rows[fid] = r
        return r

    def _on_fprog(self, fid, name, sent, total, d):
        r = self._row_for(fid, name, d)
        bar = self.ftab.cellWidget(r, 2)
        bar and bar.setValue(int(sent * 100 / max(total, 1)))
        self.ftab.scrollToItem(self.ftab.item(r, 0))

    def _on_fdone(self, fid, name, ok, d, path):
        r = self._row_for(fid, name, d)
        bar = self.ftab.cellWidget(r, 2)
        bar and bar.setValue(100 if ok else bar.value())
        tip = ("Done" if ok else "Failed") + (f" -> {path}" if path else "")
        self.ftab.setItem(r, 3, QTableWidgetItem(("✓ " if ok else "✗ ") + tip))

    # ---- Tab3 Remote terminal (phone as SSH proxy) ----
    def _term_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.ed_host = QLineEdit(); self.ed_host.setPlaceholderText("Remote server IP")
        self.ed_port = QLineEdit("22"); self.ed_port.setFixedWidth(50)
        self.ed_user = QLineEdit(); self.ed_user.setPlaceholderText("Username")
        self.ed_pass = QLineEdit(); self.ed_pass.setPlaceholderText("Password")
        self.ed_pass.setEchoMode(QLineEdit.Password)
        for x in (self.ed_host, self.ed_port, self.ed_user, self.ed_pass):
            h.addWidget(x)
        v.addLayout(h)

        h2 = QHBoxLayout()
        self.chk_key = QCheckBox("Use private key")
        self.ed_key = QLineEdit(); self.ed_key.setPlaceholderText("Private key file (OpenSSH/PKCS#8 PEM)")
        bk = QPushButton("Choose..."); bk.clicked.connect(self.pick_key)
        self.ed_phrase = QLineEdit(); self.ed_phrase.setPlaceholderText("Key passphrase (optional)")
        self.ed_phrase.setEchoMode(QLineEdit.Password); self.ed_phrase.setFixedWidth(130)
        for x in (self.chk_key, self.ed_key, bk, self.ed_phrase):
            h2.addWidget(x)
        h2.setStretch(1, 1)
        v.addLayout(h2)

        h3 = QHBoxLayout()
        b1 = QPushButton("SSH via phone (shell)")
        b1.clicked.connect(lambda: self.open_remote("ssh"))
        self.ed_cmd = QLineEdit(); self.ed_cmd.setPlaceholderText("One-shot command, e.g. uname -a")
        b2 = QPushButton("Run exec once"); b2.clicked.connect(lambda: self.open_remote("exec"))
        b3 = QPushButton("Disconnect"); b3.clicked.connect(self.close_channel)
        for x in (b1, self.ed_cmd, b2, b3):
            h3.addWidget(x)
        h3.setStretch(1, 1)
        v.addLayout(h3)

        self.term = QPlainTextEdit(); self.term.setReadOnly(True)
        h4 = QHBoxLayout()
        self.term_input = QLineEdit(); self.term_input.returnPressed.connect(self.term_send)
        h4.addWidget(self.term_input, 1)
        v.addWidget(self.term, 1); v.addLayout(h4)
        return w

    def pick_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose Private Key")
        if path:
            self.ed_key.setText(path); self.chk_key.setChecked(True)

    def _auth_params(self):
        if self.chk_key.isChecked():
            path = self.ed_key.text().strip()
            if not path:
                raise ValueError("Please select a private key file")
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return {"private_key": f.read(), "passphrase": self.ed_phrase.text()}
        return {"password": self.ed_pass.text()}

    def open_remote(self, kind, trust_fingerprint=None):
        if not (self.client.tp and self.client.tp.alive):
            return QMessageBox.warning(self, "Notice", "Please connect to the phone first")
        try:
            base = dict(host=self.ed_host.text().strip(),
                        port=int(self.ed_port.text() or 22),
                        user=self.ed_user.text().strip(),
                        **self._auth_params())
        except Exception as e:
            return QMessageBox.warning(self, "Invalid parameters", str(e))
        if kind == "exec":
            base["command"] = self.ed_cmd.text().strip()
            if not base["command"]:
                return QMessageBox.warning(self, "Invalid parameters", "Please enter the command to run")
        if trust_fingerprint:
            base["trust_fingerprint"] = trust_fingerprint
        self._pending = (kind, base)
        self.ch = self.client.remote_open(kind, **base)
        self.term.appendPlainText(
            f"** Connecting to {base['user']}@{base['host']}:{base['port']} via phone ({kind}) ... **")

    def close_channel(self):
        if self.ch:
            self.client.remote_close(self.ch)
            self.ch = None

    def term_send(self):
        line = self.term_input.text(); self.term_input.clear()
        if self.ch:
            self.client.remote_input(self.ch, (line + "\n").encode())  # Line mode, suitable for running commands

    def _on_rout(self, ch, stream, data):
        self.term.insertPlainText(bytes(data).decode("utf-8", "ignore"))
        self.term.moveCursor(QTextCursor.End)

    def _on_rclose(self, ch, code, reason):
        self.term.appendPlainText(f"\n** Channel closed code={code} {reason} **")
        if ch == self.ch:
            self.ch = None

    def _on_rerr(self, ch, code, h):
        host, fp = h.get("host", ""), h.get("fingerprint", "")
        if code == "UNKNOWN_HOST":                     # First connection: fingerprint is confirmed by the user
            ans = QMessageBox.question(
                self, "First connection to this host",
                f"The fingerprint of target host {host} is not in the phone's known-hosts store:\n\n{fp}\n\n"
                "Verify it with the server administrator before trusting. Trust and continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans == QMessageBox.Yes and self._pending:
                kind, base = self._pending
                base.pop("trust_fingerprint", None)
                self._pending = (kind, base)
                self.ch = self.client.remote_open(kind, trust_fingerprint=fp, **base)
                self.term.appendPlainText(f"** Trusted the fingerprint for {host}, reconnecting ... **")
            else:
                self.term.appendPlainText(f"** Cancelled connection to {host} **")
        elif code == "HOST_KEY_CHANGED":
            QMessageBox.critical(self, "Security Warning",
                f"The fingerprint of target host {host} does not match the known record!\n\nCurrent fingerprint: {fp}\n\n"
                "A man-in-the-middle attack is possible, so the connection was refused. Clear this host's known-host record on the phone and confirm again.")
            self.term.appendPlainText(f"** Connection refused: host key for {host} has changed **")
        else:
            self.term.appendPlainText(f"\n** Remote error {code}: {h.get('message','')} **")

    # ---- Connection management ----
    def refresh(self):
        self.cmb.clear()
        try:
            for d in self.adb.devices():
                if d["state"] == "device":
                    self.cmb.addItem(d["desc"], d["serial"])
        except Exception as e:
            self._on_status(f"[adb unavailable] {e}")

    def connect_phone(self):
        serial = self.cmb.currentData()
        if not serial:
            return QMessageBox.warning(self, "Notice", "Please refresh and select a device first (USB debugging must be enabled and authorized)")
        try:
            self.adb.forward(serial, PC_PORT, PHONE_PORT)
            self.client.connect("127.0.0.1", PC_PORT, self.ed_token.text().strip())
            self.lbl.setText("Connected " + serial)
        except Exception as e:
            QMessageBox.critical(self, "Connection failed", str(e))

    def disconnect_phone(self):
        self.client.close()
        self.ch = None
        self.lbl.setText("Not connected")
        serial = self.cmb.currentData()
        if serial:
            try:
                self.adb.remove_forward(serial, PC_PORT)
            except Exception:
                pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow(); w.show(); sys.exit(app.exec_())
