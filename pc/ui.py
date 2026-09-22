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
        self.setWindowTitle("USB Bridge 客户端")
        self.resize(820, 600)
        self.adb, self.client, self.ch = Adb(), PhoneClient(), None
        self._pending = None          # Last remote-open parameters, used to resend after fingerprint confirmation
        self._rows = {}               # fid -> table row index
        c = self.client
        c.on_text = self.sig_text.emit
        c.on_status = self.sig_status.emit
        c.on_hello_ack = lambda i: self.sig_status.emit(f"[已连接手机: {i.get('device','?')}]")
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
        self.ed_token.setPlaceholderText("手机端 token")
        self.ed_token.setFixedWidth(130)
        b1 = QPushButton("刷新设备"); b1.clicked.connect(self.refresh)
        b2 = QPushButton("连接"); b2.clicked.connect(self.connect_phone)
        b3 = QPushButton("断开"); b3.clicked.connect(self.disconnect_phone)
        self.lbl = QLabel("未连接")
        top.addWidget(QLabel("设备:")); top.addWidget(self.cmb, 1)
        top.addWidget(QLabel("Token:")); top.addWidget(self.ed_token)
        top.addWidget(b1); top.addWidget(b2); top.addWidget(b3); top.addWidget(self.lbl)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._msg_tab(), "消息")
        self.tabs.addTab(self._file_tab(), "文件")
        self.tabs.addTab(self._term_tab(), "远程终端")
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
        b = QPushButton("发送"); b.clicked.connect(self.send_text)
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
        self.msg_view.appendPlainText(f"[手机] {t}")
        self.tabs.setCurrentIndex(0)

    def _on_status(self, s):
        self.msg_view.appendPlainText(s)

    # ---- Tab2 Files (one row per fid, supports concurrent transfers) ----
    def _file_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        b = QPushButton("选择文件发送到手机…"); b.clicked.connect(self.pick_send)
        self.ftab = QTableWidget(0, 4)
        self.ftab.setHorizontalHeaderLabels(["文件", "方向", "进度", "状态"])
        self.ftab.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ftab.verticalHeader().setVisible(False)
        v.addWidget(b); v.addWidget(self.ftab, 1)
        v.addWidget(QLabel("手机发来的文件保存在 ./downloads/"))
        return w

    def pick_send(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件")
        if path:
            self.client.send_file(path)

    def _row_for(self, fid, name, direction):
        if fid in self._rows:
            return self._rows[fid]
        r = self.ftab.rowCount(); self.ftab.insertRow(r)
        self.ftab.setItem(r, 0, QTableWidgetItem(name))
        self.ftab.setItem(r, 1, QTableWidgetItem("发送 → 手机" if direction == "up" else "手机 → PC"))
        bar = QProgressBar(); bar.setValue(0)
        self.ftab.setCellWidget(r, 2, bar)
        self.ftab.setItem(r, 3, QTableWidgetItem("传输中"))
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
        tip = ("完成" if ok else "失败") + (f" → {path}" if path else "")
        self.ftab.setItem(r, 3, QTableWidgetItem(("✓ " if ok else "✗ ") + tip))

    # ---- Tab3 Remote terminal (phone as SSH proxy) ----
    def _term_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.ed_host = QLineEdit(); self.ed_host.setPlaceholderText("远程服务器 IP")
        self.ed_port = QLineEdit("22"); self.ed_port.setFixedWidth(50)
        self.ed_user = QLineEdit(); self.ed_user.setPlaceholderText("用户名")
        self.ed_pass = QLineEdit(); self.ed_pass.setPlaceholderText("密码")
        self.ed_pass.setEchoMode(QLineEdit.Password)
        for x in (self.ed_host, self.ed_port, self.ed_user, self.ed_pass):
            h.addWidget(x)
        v.addLayout(h)

        h2 = QHBoxLayout()
        self.chk_key = QCheckBox("使用私钥")
        self.ed_key = QLineEdit(); self.ed_key.setPlaceholderText("私钥文件（OpenSSH/PKCS#8 PEM）")
        bk = QPushButton("选择…"); bk.clicked.connect(self.pick_key)
        self.ed_phrase = QLineEdit(); self.ed_phrase.setPlaceholderText("私钥口令(可选)")
        self.ed_phrase.setEchoMode(QLineEdit.Password); self.ed_phrase.setFixedWidth(130)
        for x in (self.chk_key, self.ed_key, bk, self.ed_phrase):
            h2.addWidget(x)
        h2.setStretch(1, 1)
        v.addLayout(h2)

        h3 = QHBoxLayout()
        b1 = QPushButton("经手机 SSH 连接(shell)")
        b1.clicked.connect(lambda: self.open_remote("ssh"))
        self.ed_cmd = QLineEdit(); self.ed_cmd.setPlaceholderText("一次性命令，如: uname -a")
        b2 = QPushButton("exec 执行一次"); b2.clicked.connect(lambda: self.open_remote("exec"))
        b3 = QPushButton("断开"); b3.clicked.connect(self.close_channel)
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
        path, _ = QFileDialog.getOpenFileName(self, "选择私钥")
        if path:
            self.ed_key.setText(path); self.chk_key.setChecked(True)

    def _auth_params(self):
        if self.chk_key.isChecked():
            path = self.ed_key.text().strip()
            if not path:
                raise ValueError("请选择私钥文件")
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return {"private_key": f.read(), "passphrase": self.ed_phrase.text()}
        return {"password": self.ed_pass.text()}

    def open_remote(self, kind, trust_fingerprint=None):
        if not (self.client.tp and self.client.tp.alive):
            return QMessageBox.warning(self, "提示", "请先连接手机")
        try:
            base = dict(host=self.ed_host.text().strip(),
                        port=int(self.ed_port.text() or 22),
                        user=self.ed_user.text().strip(),
                        **self._auth_params())
        except Exception as e:
            return QMessageBox.warning(self, "参数错误", str(e))
        if kind == "exec":
            base["command"] = self.ed_cmd.text().strip()
            if not base["command"]:
                return QMessageBox.warning(self, "参数错误", "请填写要执行的命令")
        if trust_fingerprint:
            base["trust_fingerprint"] = trust_fingerprint
        self._pending = (kind, base)
        self.ch = self.client.remote_open(kind, **base)
        self.term.appendPlainText(
            f"** 正在通过手机连接 {base['user']}@{base['host']}:{base['port']} ({kind}) … **")

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
        self.term.appendPlainText(f"\n** 通道关闭 code={code} {reason} **")
        if ch == self.ch:
            self.ch = None

    def _on_rerr(self, ch, code, h):
        host, fp = h.get("host", ""), h.get("fingerprint", "")
        if code == "UNKNOWN_HOST":                     # First connection: fingerprint is confirmed by the user
            ans = QMessageBox.question(
                self, "首次连接该主机",
                f"目标主机 {host} 的指纹不在手机已知主机库中：\n\n{fp}\n\n"
                "请与服务器管理员核对后再信任。是否信任并继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans == QMessageBox.Yes and self._pending:
                kind, base = self._pending
                base.pop("trust_fingerprint", None)
                self._pending = (kind, base)
                self.ch = self.client.remote_open(kind, trust_fingerprint=fp, **base)
                self.term.appendPlainText(f"** 已信任 {host} 的指纹，重新连接 … **")
            else:
                self.term.appendPlainText(f"** 已取消连接 {host} **")
        elif code == "HOST_KEY_CHANGED":
            QMessageBox.critical(self, "安全告警",
                f"目标主机 {host} 的指纹与已知记录不一致！\n\n当前指纹：{fp}\n\n"
                "可能存在中间人攻击，已拒绝连接。请在手机上清除该主机的已知主机记录后再确认。")
            self.term.appendPlainText(f"** 拒绝连接：{host} 主机密钥已变更 **")
        else:
            self.term.appendPlainText(f"\n** 远程错误 {code}: {h.get('message','')} **")

    # ---- Connection management ----
    def refresh(self):
        self.cmb.clear()
        try:
            for d in self.adb.devices():
                if d["state"] == "device":
                    self.cmb.addItem(d["desc"], d["serial"])
        except Exception as e:
            self._on_status(f"[adb 不可用] {e}")

    def connect_phone(self):
        serial = self.cmb.currentData()
        if not serial:
            return QMessageBox.warning(self, "提示", "请先刷新并选择设备（需开启USB调试并授权）")
        try:
            self.adb.forward(serial, PC_PORT, PHONE_PORT)
            self.client.connect("127.0.0.1", PC_PORT, self.ed_token.text().strip())
            self.lbl.setText("已连接 " + serial)
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))

    def disconnect_phone(self):
        self.client.close()
        self.ch = None
        self.lbl.setText("未连接")
        serial = self.cmb.currentData()
        if serial:
            try:
                self.adb.remove_forward(serial, PC_PORT)
            except Exception:
                pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow(); w.show(); sys.exit(app.exec_())
