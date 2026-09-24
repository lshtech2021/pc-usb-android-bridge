"""PyQt5 UI: three tabs (messages / files / remote terminal attach-by-ID).

Run: py ui.py
"""
import sys

from PyQt5.QtCore import QTimer, pyqtSignal, Qt
from PyQt5.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap
from PyQt5.QtWidgets import (QAction, QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QComboBox, QPushButton, QLabel, QShortcut,
    QTabWidget, QLineEdit, QProgressBar, QFileDialog, QMessageBox,
    QTableWidgetItem)

import settings
import theme
import widgets
from adb_manager import Adb
from client import PhoneClient
from dialogs import ConnectionDialog
from terminal import TerminalWidget

# Phone-reported connection state -> palette key for the picker dot.
CONNECTION_DOT = {
    "running": "dot_connected",
    "starting": "dot_connecting",
    "error": "dot_error",
    "stopped": "dot_disconnected",
}

TERMINAL_STATES = {
    "detached": ("dot_disconnected", "Not attached"),
    "attaching": ("dot_connecting", "Attaching…"),
    "attached": ("dot_connected", "Attached"),
    "error": ("dot_error", "Attach failed"),
}

SHORTCUTS = [
    ("Ctrl+,", "Open connection settings (device, token, PC name)"),
    ("F5", "Refresh the USB device list"),
    ("Ctrl+K", "Connect to the selected device"),
    ("Ctrl+R", "Refresh the phone's SSH connection list"),
    ("Ctrl+Enter", "Send the typed message"),
    ("F11", "Toggle full-screen terminal"),
    ("Escape", "Leave full screen (when no session is attached)"),
    ("Ctrl+=  /  Ctrl+-", "Bigger / smaller terminal text"),
    ("Ctrl+0", "Reset terminal text size"),
    ("Ctrl+Shift+C", "Copy from the terminal"),
    ("Ctrl+Shift+V", "Paste into the terminal"),
    ("Ctrl+C", "Terminal: copy if text is selected, else interrupt the remote"),
    ("Ctrl+Q", "Quit"),
]


def state_dot_icon(color_key: str) -> QIcon:
    """Small filled circle used to mark connection state in the picker."""
    pm = QPixmap(10, 10)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.color(color_key)))
    painter.drawEllipse(0, 0, 9, 9)
    painter.end()
    return QIcon(pm)

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
        self._conn_state = "disconnected"
        self._fullscreen = False
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
        self.dlg = ConnectionDialog(self)
        self.dlg.connect_requested.connect(self.connect_phone)
        self.dlg.disconnect_requested.connect(self.disconnect_phone)
        self.dlg.refresh_requested.connect(self.refresh)
        self.dlg.device_changed.connect(self._on_device_changed)
        self.dlg.set_pc_name(self.client.identity.pc_name)
        self.dlg.set_identity(self.client.identity.short_id, self.client.identity.pc_id)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._msg_tab(), "Messages")
        self.tabs.addTab(self._file_tab(), "Files")
        self.tabs.addTab(self._term_tab(), "Remote Terminal")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.strip = self._build_strip()
        root = QWidget(); root.setObjectName("root")
        lay = QVBoxLayout(root)
        # No bottom margin: the tab pane should sit straight on the status bar.
        lay.setContentsMargins(theme.SPACE_S, theme.SPACE_XS, theme.SPACE_S, 0)
        lay.setSpacing(theme.SPACE_S)
        lay.addWidget(self.strip)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self._build_menus()
        self.statusBar().setSizeGripEnabled(False)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.send_text)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_connection_dialog)

        self._restore_window_state()
        self.refresh()
        self._on_tab_changed(self.tabs.currentIndex())

    # ---- Menu bar and status bar ----
    def _menu_action(self, menu, text, slot, shortcut=None, checkable=False):
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.setCheckable(checkable)
        # triggered only fires on user activation, so a checkable action must use
        # toggled for setChecked() from code (startup restore) to take effect.
        if checkable:
            act.toggled.connect(slot)
        else:
            act.triggered.connect(slot)
        menu.addAction(act)
        return act

    def _build_menus(self):
        bar = self.menuBar()
        m_file = bar.addMenu("&File")
        self._menu_action(m_file, "&Connection settings…",
                          self.open_connection_dialog, "Ctrl+,")
        m_file.addSeparator()
        self._menu_action(m_file, "&Refresh devices", self.refresh, "F5")
        self._menu_action(m_file, "&Connect", self.connect_phone, "Ctrl+K")
        self._menu_action(m_file, "&Disconnect", self.disconnect_phone)
        m_file.addSeparator()
        self._menu_action(m_file, "Open &received folder",
                          lambda: widgets.open_in_file_manager(self.client.save_dir))
        m_file.addSeparator()
        self._menu_action(m_file, "E&xit", self.close, "Ctrl+Q")

        m_conn = bar.addMenu("&Connection")
        self._menu_action(m_conn, "&Refresh list", self.refresh_connections, "Ctrl+R")
        self._menu_action(m_conn, "&Attach", self.attach_conn)
        self._menu_action(m_conn, "De&tach", self.detach_conn)

        m_view = bar.addMenu("&View")
        self.act_fullscreen = self._menu_action(
            m_view, "&Full screen terminal", self._set_fullscreen,
            "F11", checkable=True)
        m_view.addSeparator()
        self.act_dark = self._menu_action(
            m_view, "&Dark mode", self._set_dark, checkable=True)
        m_view.addSeparator()
        self._menu_action(m_view, "&Bigger terminal text",
                          lambda: self.term.zoom(1), "Ctrl+=")
        self._menu_action(m_view, "&Smaller terminal text",
                          lambda: self.term.zoom(-1), "Ctrl+-")
        self._menu_action(m_view, "&Reset terminal text size",
                          lambda: self.term.reset_zoom(), "Ctrl+0")

        m_help = bar.addMenu("&Help")
        self._menu_action(m_help, "&Keyboard shortcuts", self._show_shortcuts)
        self._menu_action(m_help, "&About", self._show_about)

    def _set_dark(self, on: bool):
        theme.set_palette("dark" if on else "light")
        settings.put(settings.KEY_PALETTE, theme.palette_name())
        self._apply_theme()

    def _apply_theme(self):
        """Re-style everything that does not come from the app style sheet."""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.stylesheet())
        self.chip.apply()
        self.term_pill.apply()
        self.dlg.apply_theme()
        self.ftab.apply_theme()
        self.term.apply_theme()
        for i in range(self.cmb_conn.count()):
            state = self.cmb_conn.itemData(i, Qt.UserRole + 1)
            if state:
                self.cmb_conn.setItemIcon(i, state_dot_icon(CONNECTION_DOT.get(
                    state, "dot_disconnected")))
        if self.act_dark.isChecked() != (theme.palette_name() == "dark"):
            self.act_dark.setChecked(theme.palette_name() == "dark")

    def _status(self, text: str, timeout: int = 0):
        self.statusBar().showMessage(text, timeout)

    def _on_tab_changed(self, index: int):
        settings.put(settings.KEY_TAB, index)
        if index == 2:
            self._status("Copy: Ctrl+Shift+C · Paste: Ctrl+Shift+V · "
                         "Zoom: Ctrl+= / Ctrl+- / Ctrl+0")
        else:
            self._status("")

    def _show_shortcuts(self):
        rows = "".join(
            f"<tr><td style='padding-right:18px'><b>{key}</b></td>"
            f"<td>{desc}</td></tr>" for key, desc in SHORTCUTS)
        QMessageBox.information(
            self, "Keyboard shortcuts", f"<table>{rows}</table>")

    def _show_about(self):
        QMessageBox.about(
            self, "About USB Bridge",
            "<b>USB Bridge Client</b><br><br>"
            "Talks to the USB Bridge Android app over an adb-forwarded socket.<br>"
            "After the handshake the link is sealed with ECDH + AES-GCM; "
            "SSH credentials stay on the phone.")

    # ---- Persistence ----
    def _restore_window_state(self):
        theme.set_palette(settings.get(settings.KEY_PALETTE, "light"))
        self.act_dark.setChecked(theme.palette_name() == "dark")
        self._apply_theme()
        geometry = settings.get(settings.KEY_GEOMETRY)
        if geometry is not None:
            self.restoreGeometry(geometry)
        try:
            tab = int(settings.get(settings.KEY_TAB, 0))
        except (TypeError, ValueError):
            tab = 0
        if 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)
        try:
            size = int(settings.get(settings.KEY_FONT_SIZE, 11))
        except (TypeError, ValueError):
            size = 11
        self.term.zoom(size - self.term.font_size())

    def closeEvent(self, event):
        settings.put(settings.KEY_GEOMETRY, self.saveGeometry())
        settings.put(settings.KEY_TAB, self.tabs.currentIndex())
        settings.put(settings.KEY_DEVICE, self.dlg.selected_serial() or "")
        settings.put(settings.KEY_PALETTE, theme.palette_name())
        settings.put(settings.KEY_FONT_SIZE, self.term.font_size())
        settings.sync()
        if self.ch is not None:
            try:
                self.client.detach(self.ch)
            except Exception:
                pass
            self.ch = None
        self.client.close()
        super().closeEvent(event)

    def _build_strip(self):
        """One slim row of connection chrome; the form itself lives in the dialog."""
        card = QWidget(); card.setObjectName("header")
        row = QHBoxLayout(card)
        row.setContentsMargins(theme.SPACE_M, theme.SPACE_XS, theme.SPACE_M, theme.SPACE_XS)
        row.setSpacing(theme.SPACE_S)

        self.btn_connection = QPushButton("Connection…")
        self.btn_connection.setToolTip("Device, token, PC name and fingerprint (Ctrl+,)")
        self.btn_connection.clicked.connect(self.open_connection_dialog)
        self.lbl_device = theme.caption("No device")
        self.lbl_device.setToolTip("Device selected in the connection dialog")
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setProperty("variant", "primary")
        self.btn_connect.setToolTip("Forward the USB port and handshake with the phone (Ctrl+K)")
        self.btn_connect.clicked.connect(self.connect_phone)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self.disconnect_phone)

        row.addWidget(self.btn_connection)
        row.addWidget(theme.vertical_separator())
        row.addWidget(self.lbl_device)
        row.addStretch(1)
        row.addWidget(self.btn_connect)
        row.addWidget(self.btn_disconnect)
        row.addWidget(theme.vertical_separator())

        self.chip = theme.connection_pill()
        row.addWidget(self.chip, 0, Qt.AlignRight)
        return card

    # ---- Connection dialog ----
    def open_connection_dialog(self):
        self.dlg.present()

    def _on_device_changed(self):
        serial = self.dlg.selected_serial()
        label = self.dlg.device_label()
        self.lbl_device.setText(label or "No device")
        self.lbl_device.setToolTip(serial or "")
        settings.put(settings.KEY_DEVICE, serial or "")

    def _refresh_device_list(self):
        devices = []
        try:
            for d in self.adb.devices():
                if d["state"] == "device":
                    devices.append((d["desc"], d["serial"]))
        except Exception as e:
            self._on_status(f"[adb unavailable] {e}")
        self.dlg.set_devices(devices)

    def _flash(self, button, restore: str, temporary: str):
        button.setText(temporary)
        QTimer.singleShot(1200, lambda: button.setText(restore))

    def _set_conn_state(self, state: str, detail: str = ""):
        """Single source of truth for the chip and for which actions are available."""
        self._conn_state = state
        self.chip.set_state(state, detail)
        self.dlg.set_state(state, detail)
        self.btn_connect.setEnabled(state in ("disconnected", "error"))
        # Disconnect doubles as "cancel" while a handshake is still in flight.
        self.btn_disconnect.setEnabled(state in ("connecting", "connected"))

    # ---- Tab1 Messages ----
    def _msg_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(theme.SPACE_M, theme.SPACE_M, theme.SPACE_M, theme.SPACE_M)
        v.setSpacing(theme.SPACE_S)
        self.msg_log = widgets.MessageLog()
        h = QHBoxLayout()
        h.setSpacing(theme.SPACE_S)
        self.msg_input = QLineEdit()
        self.msg_input.setPlaceholderText("Type a message and press Enter")
        self.msg_input.returnPressed.connect(self.send_text)
        self.btn_send = QPushButton("Send")
        self.btn_send.setProperty("variant", "primary")
        self.btn_send.setToolTip("Send to the phone (Ctrl+Enter)")
        self.btn_send.clicked.connect(self.send_text)
        self.btn_copy_all = QPushButton("Copy all")
        self.btn_copy_all.setProperty("variant", "subtle")
        self.btn_copy_all.setToolTip("Copy the whole conversation to the clipboard")
        self.btn_copy_all.clicked.connect(self._copy_transcript)
        h.addWidget(self.msg_input, 1)
        h.addWidget(self.btn_send)
        h.addWidget(self.btn_copy_all)
        v.addWidget(self.msg_log, 1)
        v.addLayout(h)
        return w

    def _copy_transcript(self):
        QApplication.clipboard().setText(self.msg_log.transcript())
        self._flash(self.btn_copy_all, "Copy all", "Copied")

    def send_text(self):
        t = self.msg_input.text().strip()
        if t and self.client.tp and self.client.tp.alive:
            self.client.send_text(t)
            self.msg_log.add_message(t, from_me=True)
            self.msg_input.clear()

    def _on_text(self, t):
        self.msg_log.add_message(t, from_me=False)
        self.tabs.setCurrentIndex(0)

    def _on_status(self, s):
        # Connection-level notices belong in the log, not as a fake chat bubble.
        self.msg_log.add_notice(s)
        low = s.lower()
        if "[connection closed]" in low:
            self._set_conn_state("disconnected")
            self._reset_attach()
        elif "bad_token" in low:
            self._set_conn_state("error", "wrong token")
            self._reset_attach()
        elif "pc_rejected" in low:
            self._set_conn_state("error", "rejected on phone")
            self._reset_attach()
        elif "pc_already_connected" in low:
            self._set_conn_state("error", "already connected")
            self._reset_attach()
        elif "pc_key_changed" in low:
            self._set_conn_state("error", "PC key changed")
            self._reset_attach()
        elif "adb unavailable" in low:
            self._set_conn_state("error", "adb unavailable")
        if "error" in low or "bad_token" in low or "rejected" in low or "unavailable" in low:
            self._status(s, 8000)

    def _hello_ack(self, info):
        serial = self.dlg.selected_serial() or "?"
        self._set_conn_state("connected", f"{serial} · encrypted")
        self._status(f"Connected to {serial} — link sealed", 6000)
        self.msg_log.add_notice(
            f"Phone connected: {info.get('device', '?')} · link sealed")
        self.client.list_connections()

    # ---- Tab2 Files ----
    def _file_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(theme.SPACE_M, theme.SPACE_M, theme.SPACE_M, theme.SPACE_M)
        v.setSpacing(theme.SPACE_S)
        bar = QHBoxLayout()
        bar.setSpacing(theme.SPACE_S)
        self.btn_add_files = QPushButton("Add files…")
        self.btn_add_files.setProperty("variant", "primary")
        self.btn_add_files.setToolTip("Choose files to send to the phone")
        self.btn_add_files.clicked.connect(self.pick_send)
        bar.addWidget(self.btn_add_files)
        bar.addWidget(theme.caption("or drag files onto the list"))
        bar.addStretch(1)
        v.addLayout(bar)

        self.ftab = widgets.FileDropTable()
        self.ftab.files_dropped.connect(self.send_files)
        v.addWidget(self.ftab, 1)

        foot = QHBoxLayout()
        foot.setSpacing(theme.SPACE_S)
        folder = QLabel(self.client.save_dir)
        folder.setProperty("role", "mono")
        folder.setToolTip(self.client.save_dir)
        self.btn_open_folder = QPushButton("Open folder")
        self.btn_open_folder.setProperty("variant", "subtle")
        self.btn_open_folder.setToolTip("Open the folder that receives files from the phone")
        self.btn_open_folder.clicked.connect(
            lambda: widgets.open_in_file_manager(self.client.save_dir))
        foot.addWidget(theme.caption("Received files:"))
        foot.addWidget(folder, 1)
        foot.addWidget(self.btn_open_folder)
        v.addLayout(foot)
        return w

    def pick_send(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose files to send")
        if paths:
            self.send_files(paths)

    def send_files(self, paths):
        if not (self.client.tp and self.client.tp.alive):
            return QMessageBox.warning(self, "Notice", "Please connect to the phone first")
        if len(paths) > 20:
            n = len(paths)
            if QMessageBox.question(
                    self, "Send files",
                    f"Send {n} files to the phone?") != QMessageBox.Yes:
                return
        for p in paths:
            self.client.send_file(p)

    def _row_for(self, fid, name, direction):
        st = self._rows.get(fid)
        if st:
            return st
        r = self.ftab.rowCount()
        self.ftab.insertRow(r)
        self.ftab.setItem(r, 0, QTableWidgetItem(name))
        self.ftab.setItem(r, 1, QTableWidgetItem("Send → Phone" if direction == "up" else "Phone → PC"))
        bar = QProgressBar(); bar.setValue(0); bar.setTextVisible(False)
        bar.setFixedHeight(8)
        # A cell widget fills its cell, so centre the thin bar inside a column.
        bar_holder = QWidget()
        bar_lay = QVBoxLayout(bar_holder)
        bar_lay.setContentsMargins(0, 0, 0, 0)
        bar_lay.addStretch(1); bar_lay.addWidget(bar); bar_lay.addStretch(1)
        self.ftab.setCellWidget(r, 2, bar_holder)
        status = QTableWidgetItem("Transferring")
        self.ftab.setItem(r, 3, status)
        action = QPushButton("Cancel")
        action.setProperty("variant", "subtle")
        action.clicked.connect(lambda _=False, f=fid: self._row_action(f))
        self.ftab.setCellWidget(r, 4, action)
        self.ftab.setRowHeight(r, 34)
        st = {"row": r, "bar": bar, "status": status, "action": action,
              "name": name, "direction": direction, "state": "transferring", "path": ""}
        self._rows[fid] = st
        self.ftab.refresh_empty_state()
        return st

    def _row_action(self, fid):
        st = self._rows.get(fid)
        if not st:
            return
        if st["state"] == "transferring":
            self.client.cancel_upload(fid)
        elif st["state"] == "done" and st["direction"] == "down" and st["path"]:
            widgets.open_in_file_manager(st["path"])

    def _on_fprog(self, fid, name, sent, total, d):
        st = self._row_for(fid, name, d)
        st["bar"].setValue(int(sent * 100 / max(total, 1)))
        self.ftab.scrollToItem(self.ftab.item(st["row"], 0))

    def _on_fdone(self, fid, name, ok, d, path):
        st = self._row_for(fid, name, d)
        st["bar"].setValue(100 if ok else st["bar"].value())
        st["state"] = "done" if ok else "failed"
        st["path"] = path
        if ok:
            text = f"Done → {path}" if path else "Done"
        else:
            text = f"Failed — {path}" if path else "Failed"
        st["status"].setText(("✓ " if ok else "✗ ") + text)
        st["status"].setToolTip(text)
        # Reveal only makes sense for a file that actually landed on this PC.
        # A cell widget cannot be hidden reliably (the view re-shows it), so the
        # unused state is a disabled placeholder instead.
        if st["state"] == "done" and d == "down" and path:
            st["action"].setText("Reveal")
            st["action"].setEnabled(True)
        else:
            st["action"].setText("—")
            st["action"].setEnabled(False)

    # ---- Tab3 Remote terminal (attach by phone connection ID) ----
    def _term_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(theme.SPACE_M, theme.SPACE_M, theme.SPACE_M, theme.SPACE_M)
        v.setSpacing(theme.SPACE_S)
        h = QHBoxLayout()
        h.setSpacing(theme.SPACE_S)
        self.cmb_conn = QComboBox()
        self.cmb_conn.setMinimumWidth(320)
        self.cmb_conn.setToolTip("SSH profiles on the phone; the dot shows whether it is running")
        self.btn_conn_refresh = QPushButton("Refresh list")
        self.btn_conn_refresh.setToolTip("Ask the phone for its connection list (Ctrl+R)")
        self.btn_conn_refresh.clicked.connect(self.refresh_connections)
        self.btn_conn_attach = QPushButton("Attach")
        self.btn_conn_attach.setProperty("variant", "primary")
        self.btn_conn_attach.setToolTip("Open an interactive terminal on the selected connection")
        self.btn_conn_attach.clicked.connect(self.attach_conn)
        self.btn_conn_detach = QPushButton("Detach")
        self.btn_conn_detach.setToolTip("Leave the session running on the phone")
        self.btn_conn_detach.clicked.connect(self.detach_conn)
        self.btn_fullscreen = QPushButton("Full screen")
        self.btn_fullscreen.setCheckable(True)
        self.btn_fullscreen.setToolTip("Hide all chrome and give the terminal the window (F11)")
        self.btn_fullscreen.toggled.connect(self._set_fullscreen)
        h.addWidget(theme.field_label("Connection"))
        h.addWidget(self.cmb_conn, 1)
        h.addWidget(self.btn_conn_refresh)
        h.addWidget(self.btn_conn_attach)
        h.addWidget(self.btn_conn_detach)
        h.addWidget(theme.vertical_separator())
        h.addWidget(self.btn_fullscreen)
        v.addLayout(h)

        self.term_pill = theme.StatePill(TERMINAL_STATES, "detached")
        v.addWidget(self.term_pill, 0, Qt.AlignLeft)

        self.term = TerminalWidget()
        self.term.bytes_out.connect(self._term_bytes)
        self.term.escape_out.connect(self._exit_fullscreen)
        v.addWidget(self.term, 1)
        self._set_term_state("detached", "start one on the phone, then Attach")
        return w

    def _set_fullscreen(self, on: bool):
        """Terminal-only mode: drop every piece of chrome around the terminal."""
        self._fullscreen = on
        self.menuBar().setVisible(not on)
        self.strip.setVisible(not on)
        self.tabs.tabBar().setVisible(not on)
        self.statusBar().setVisible(not on)
        self.btn_fullscreen.setText("Exit full screen" if on else "Full screen")
        if self.act_fullscreen.isChecked() != on:
            self.act_fullscreen.setChecked(on)
        if on:
            self.tabs.setCurrentIndex(2)
            self.term.setFocus()

    def _exit_fullscreen(self):
        if getattr(self, "_fullscreen", False):
            self.btn_fullscreen.setChecked(False)

    def _set_term_state(self, state: str, detail: str = ""):
        self.term_pill.set_state(state, detail)

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
            label = (f"{cid}  {c.get('name', '')}  "
                     f"{c.get('user', '')}@{c.get('host', '')}  · {state}")
            self.cmb_conn.addItem(
                state_dot_icon(CONNECTION_DOT.get(state, "dot_disconnected")), label, cid)
            # Remember the raw state so a palette change can redraw the dot.
            self.cmb_conn.setItemData(self.cmb_conn.count() - 1, state, Qt.UserRole + 1)
            if state != "running":
                self.cmb_conn.setItemData(
                    self.cmb_conn.count() - 1,
                    "Only a running connection can be attached — start it on the phone",
                    Qt.ToolTipRole)
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
        self._set_term_state("attaching", str(cid))
        self.ch = self.client.attach(cid)

    def detach_conn(self):
        if self.ch is not None:
            self.client.detach(self.ch)
        self._reset_attach()

    def _reset_attach(self):
        self.ch = None
        self.term.set_attached(False)
        self._set_term_state("detached", "start one on the phone, then Attach")

    def _on_attach(self, h, backlog):
        if not h.get("ok"):
            self._set_term_state("error", str(h.get("message", "")))
            self.ch = None
            return
        cid = h.get("connection_id", "?")
        self.ch = h.get("channel", self.ch)
        self._set_term_state("attached", f"{cid} · channel {self.ch}")
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
            self._set_term_state("detached", f"session closed ({code})")
            self.term.set_attached(False)
            self.ch = None

    def _on_rerr(self, ch, code, h):
        msg = h.get("message", "")
        self._set_term_state("error", f"{code}: {msg}" if msg else str(code))
        if ch == self.ch or code in ("CONN_NOT_RUNNING", "CONN_BUSY", "CONN_NOT_FOUND"):
            self.term.set_attached(False)
            if ch == self.ch:
                self.ch = None
        self.msg_log.add_notice(f"Remote error {code}: {msg}")

    # ---- USB connection management ----
    def refresh(self):
        self._refresh_device_list()
        self._on_device_changed()

    def connect_phone(self):
        serial = self.dlg.selected_serial()
        if not serial:
            self.open_connection_dialog()
            return QMessageBox.warning(
                self, "Notice",
                "Please refresh and select a device first (USB debugging must be enabled and authorized)")
        try:
            name = self.dlg.pc_name()
            if name:
                self.client.identity.set_name(name)
            self.adb.forward(serial, PC_PORT, PHONE_PORT)
            self._set_conn_state("connecting", "approve on phone")
            self.client.connect("127.0.0.1", PC_PORT, self.dlg.token())
        except Exception as e:
            self._set_conn_state("error", "connect failed")
            QMessageBox.critical(self, "Connection failed", str(e))

    def disconnect_phone(self):
        if self.ch is not None:
            try:
                self.client.detach(self.ch)
            except Exception:
                pass
        self._reset_attach()
        self.client.close()
        self._set_conn_state("disconnected")
        serial = self.dlg.selected_serial()
        if serial:
            try:
                self.adb.remove_forward(serial, PC_PORT)
            except Exception:
                pass


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    # Paint in the saved palette from the first frame to avoid a light flash.
    theme.set_palette(str(settings.get(settings.KEY_PALETTE, "light")))
    app.setStyleSheet(theme.stylesheet())
    w = MainWindow(); w.show(); sys.exit(app.exec_())
