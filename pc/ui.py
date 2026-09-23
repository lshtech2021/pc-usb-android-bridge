"""PyQt5 UI: three tabs (messages / files / remote terminal attach-by-ID).

Run: py ui.py
"""
import sys

from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QComboBox, QPushButton, QLabel, QTabWidget, QPlainTextEdit,
    QLineEdit, QProgressBar, QFileDialog, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView)

from adb_manager import Adb
from client import PhoneClient
from terminal import TerminalWidget

PC_PORT, PHONE_PORT = 12580, 9999


class MainWindow(QMainWindow):
    sig_text = pyqtSignal(str)
    sig_status = pyqtSignal(str)
    sig_hello = pyqtSignal(object)
    sig_fprog = pyqtSignal(object, str, int, int, str)
    sig_fdone = pyqtSignal(object, str, bool, str, str)
    sig_rout = pyqtSignal(object, str, object)
    sig_rclose = pyqtSignal(object, int, str)
    sig_rerr = pyqtSignal(object, str, object)
    sig_clist = pyqtSignal(object)
    sig_attach = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("USB Bridge Client")
        self.resize(900, 640)
        self.adb, self.client, self.ch = Adb(), PhoneClient(), None
        self._conn_items = []   # list of connection dicts from phone
        self._rows = {}
        c = self.client
        c.on_text = self.sig_text.emit
        c.on_status = self.sig_status.emit
        c.on_hello_ack = self.sig_hello.emit
        c.on_file_progress = lambda *a: self.sig_fprog.emit(*a)
        c.on_file_done = lambda *a: self.sig_fdone.emit(*a)
        c.on_remote_output = lambda *a: self.sig_rout.emit(*a)
        c.on_remote_close = lambda *a: self.sig_rclose.emit(*a)
        c.on_remote_error = lambda *a: self.sig_rerr.emit(*a)
        c.on_conn_list = self.sig_clist.emit
        c.on_attach_ack = lambda h, p: self.sig_attach.emit(h, p)
        for s, slot in ((self.sig_text, self._on_text), (self.sig_status, self._on_status),
                        (self.sig_hello, self._hello_ack),
                        (self.sig_fprog, self._on_fprog), (self.sig_fdone, self._on_fdone),
                        (self.sig_rout, self._on_rout), (self.sig_rclose, self._on_rclose),
                        (self.sig_rerr, self._on_rerr),
                        (self.sig_clist, self._on_clist), (self.sig_attach, self._on_attach)):
            s.connect(slot)
        self._build_ui()

    def _build_ui(self):
        top = QHBoxLayout()
        self.cmb = QComboBox()
        self.ed_token = QLineEdit()
        self.ed_token.setPlaceholderText("Phone token")
        self.ed_token.setEchoMode(QLineEdit.Password)
        self.ed_token.setFixedWidth(130)
        self.ed_pc_name = QLineEdit()
        self.ed_pc_name.setPlaceholderText("PC display name")
        self.ed_pc_name.setText(self.client.identity.pc_name)
        self.ed_pc_name.setFixedWidth(140)
        b1 = QPushButton("Refresh Devices"); b1.clicked.connect(self.refresh)
        b2 = QPushButton("Connect"); b2.clicked.connect(self.connect_phone)
        b3 = QPushButton("Disconnect"); b3.clicked.connect(self.disconnect_phone)
        self.lbl = QLabel("Not connected")
        top.addWidget(QLabel("Device:")); top.addWidget(self.cmb, 1)
        top.addWidget(QLabel("Token:")); top.addWidget(self.ed_token)
        top.addWidget(QLabel("PC name:")); top.addWidget(self.ed_pc_name)
        top.addWidget(b1); top.addWidget(b2); top.addWidget(b3); top.addWidget(self.lbl)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._msg_tab(), "Messages")
        self.tabs.addTab(self._file_tab(), "Files")
        self.tabs.addTab(self._term_tab(), "Remote Terminal")
        root = QWidget(); lay = QVBoxLayout(root)
        lay.addLayout(top)
        lay.addWidget(QLabel(
            f"PC fingerprint: {self.client.identity.short_id}… "
            "(private key never shown; approve this PC on the phone when prompted)"))
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self.refresh()

    # ---- Tab1 Messages ----
    def _msg_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        self.msg_view = QPlainTextEdit()
        self.msg_view.setReadOnly(True)
        self.msg_view.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        h = QHBoxLayout()
        self.msg_input = QLineEdit(); self.msg_input.returnPressed.connect(self.send_text)
        b = QPushButton("Send"); b.clicked.connect(self.send_text)
        b_copy = QPushButton("Copy all")
        b_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.msg_view.toPlainText()))
        h.addWidget(self.msg_input, 1); h.addWidget(b); h.addWidget(b_copy)
        v.addWidget(QLabel("Messages (select text to copy, or Copy all)"))
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
        low = s.lower()
        if ("[connection closed]" in low or "bad_token" in low
                or "pc_rejected" in low or "pc_already_connected" in low
                or "pc_key_changed" in low):
            self.lbl.setText("Not connected")
            self._reset_attach()

    def _hello_ack(self, info):
        serial = self.cmb.currentData() or "?"
        self.lbl.setText("Connected " + str(serial) + " (encrypted)")
        self.msg_view.appendPlainText(
            f"[Phone connected: {info.get('device', '?')}] link sealed")
        self.client.list_connections()

    # ---- Tab2 Files ----
    def _file_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        b = QPushButton("Choose a file to send to phone…"); b.clicked.connect(self.pick_send)
        self.ftab = QTableWidget(0, 4)
        self.ftab.setHorizontalHeaderLabels(["File", "Direction", "Progress", "Status"])
        self.ftab.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ftab.verticalHeader().setVisible(False)
        v.addWidget(b); v.addWidget(self.ftab, 1)
        v.addWidget(QLabel("Files sent from the phone are saved in ./downloads/"))
        return w

    def pick_send(self):
        if not (self.client.tp and self.client.tp.alive):
            return QMessageBox.warning(self, "Notice", "Please connect to the phone first")
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

    # ---- Tab3 Remote terminal (attach by phone connection ID) ----
    def _term_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.cmb_conn = QComboBox()
        self.cmb_conn.setMinimumWidth(280)
        br = QPushButton("Refresh list"); br.clicked.connect(self.refresh_connections)
        ba = QPushButton("Attach"); ba.clicked.connect(self.attach_conn)
        bd = QPushButton("Detach"); bd.clicked.connect(self.detach_conn)
        self.lbl_term = QLabel("Not attached — start a connection on the phone, then Attach")
        h.addWidget(QLabel("Connection:")); h.addWidget(self.cmb_conn, 1)
        h.addWidget(br); h.addWidget(ba); h.addWidget(bd)
        v.addLayout(h)
        v.addWidget(self.lbl_term)
        self.term = TerminalWidget()
        self.term.bytes_out.connect(self._term_bytes)
        v.addWidget(self.term, 1)
        v.addWidget(QLabel(
            "Interactive terminal (pyte). Copy: Ctrl+Shift+C or selection+Ctrl+C; "
            "Paste: Ctrl+Shift+V / Ctrl+V. Right-click for menu."))
        return w

    def refresh_connections(self):
        if not (self.client.tp and self.client.tp.alive):
            return QMessageBox.warning(self, "Notice", "Please connect to the phone first")
        self.client.list_connections()

    def _on_clist(self, connections):
        self._conn_items = list(connections or [])
        cur = self.cmb_conn.currentData()
        self.cmb_conn.clear()
        for c in self._conn_items:
            cid = c.get("id", "?")
            state = c.get("state", "?")
            label = f"{cid}  {c.get('name', '')}  {c.get('user', '')}@{c.get('host', '')}  [{state}]"
            self.cmb_conn.addItem(label, cid)
        if cur:
            i = self.cmb_conn.findData(cur)
            if i >= 0:
                self.cmb_conn.setCurrentIndex(i)

    def attach_conn(self):
        if not (self.client.tp and self.client.tp.alive):
            return QMessageBox.warning(self, "Notice", "Please connect to the phone first")
        cid = self.cmb_conn.currentData()
        if not cid:
            return QMessageBox.warning(self, "Notice", "No connection selected")
        if self.ch is not None:
            self.detach_conn()
        self.term.clear_screen()
        self.lbl_term.setText(f"Attaching to {cid}…")
        self.ch = self.client.attach(cid)

    def detach_conn(self):
        if self.ch is not None:
            self.client.detach(self.ch)
        self._reset_attach()

    def _reset_attach(self):
        self.ch = None
        self.term.set_attached(False)
        self.lbl_term.setText("Not attached — start a connection on the phone, then Attach")

    def _on_attach(self, h, backlog):
        if not h.get("ok"):
            self.lbl_term.setText(f"Attach failed")
            self.ch = None
            return
        cid = h.get("connection_id", "?")
        self.ch = h.get("channel", self.ch)
        self.lbl_term.setText(f"Attached to {cid} (channel {self.ch})")
        self.term.set_attached(True)
        if backlog:
            self.term.feed(bytes(backlog))

    def _term_bytes(self, data):
        if self.ch is not None and data:
            self.client.remote_input(self.ch, bytes(data))

    def _on_rout(self, ch, stream, data):
        if ch != self.ch:
            return
        self.term.feed(bytes(data))

    def _on_rclose(self, ch, code, reason):
        if ch == self.ch:
            self.lbl_term.setText(f"Session closed code={code} {reason}")
            self.term.set_attached(False)
            self.ch = None

    def _on_rerr(self, ch, code, h):
        msg = h.get("message", "")
        self.lbl_term.setText(f"Error {code}: {msg}")
        if ch == self.ch or code in ("CONN_NOT_RUNNING", "CONN_BUSY", "CONN_NOT_FOUND"):
            self.term.set_attached(False)
            if ch == self.ch:
                self.ch = None
        self.msg_view.appendPlainText(f"[Remote error] {code} {msg}")

    # ---- USB connection management ----
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
            return QMessageBox.warning(
                self, "Notice",
                "Please refresh and select a device first (USB debugging must be enabled and authorized)")
        try:
            name = self.ed_pc_name.text().strip()
            if name:
                self.client.identity.set_name(name)
            self.adb.forward(serial, PC_PORT, PHONE_PORT)
            self.lbl.setText("Connecting… approve on phone if prompted")
            self.client.connect("127.0.0.1", PC_PORT, self.ed_token.text())
        except Exception as e:
            self.lbl.setText("Not connected")
            QMessageBox.critical(self, "Connection failed", str(e))

    def disconnect_phone(self):
        if self.ch is not None:
            try:
                self.client.detach(self.ch)
            except Exception:
                pass
        self._reset_attach()
        self.client.close()
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
