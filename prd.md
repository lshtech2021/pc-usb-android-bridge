# USB Phone Bridge Client — Requirements · Design · Implementation

## 1. Requirements and Scope

### 1.1 Original requirements

Build a client on the PC side that communicates with a phone-side service over USB, providing:

1. File transfer in both directions;
2. Text message transfer in both directions;
3. PC-side remote operation: the phone-side service acts as a proxy to perform remote service calls (for example, a service on the phone SSHes to a remote server, and the PC operates that remote server through the phone).

### 1.2 Feature breakdown and acceptance criteria

| ID | Feature | Acceptance criteria |
|---|---|---|
| F1 | PC → phone file transfer | Progress is visible after choosing a file; the phone receives the complete file; SHA256 matches; duplicate names do not overwrite existing files |
| F2 | Phone → PC file transfer | The phone picks a file and sends it; the PC saves it to `./downloads/`; on SHA256 mismatch the file is deleted automatically and an error is reported |
| F3 | PC → phone text | The phone raises a notification-bar alert |
| F4 | Phone → PC text | The PC message panel shows phone-sent text in real time |
| F5 | Interactive remote terminal | PC enters target server IP/port/account → an SSH shell is established via the phone → command output streams back in real time → the phone-side session is released on disconnect |
| F6 | One-shot remote command (exec) | A single command can be executed and stdout/stderr plus the exit code are returned |
| F7 | Host fingerprint confirmation on first connection | When the target host is unknown, the connection is refused and the fingerprint is returned; after PC-side confirmation it is written to the phone's known_hosts and no longer asked |
| F8 | Host key change protection | When the target host fingerprint does not match the known one, the connection is **refused** and an alarm is raised; silently continuing is not allowed |
| F9 | Device discovery and connection management | List adb online devices, connect, disconnect (disconnect also clears adb forward and local partial files) |
| F10 | Link authentication | Connections without the correct token are rejected and closed |
| F11 | Heartbeat keepalive | 15s heartbeat on both sides; when the network thread exits abnormally the UI clearly shows "connection closed" |

### 1.3 Non-functional requirements

| Dimension | Requirement |
|---|---|
| Platform | PC: Windows/Linux/macOS + Python 3.8+ + PyQt5 + adb(platform-tools); Phone: minSdk 24 (Android 7.0), Android 8.0 (API 26)+ recommended (foreground service notification channel) |
| File size | Single file ≤ 2GB (streamed in 256KB chunks, never loading the whole file into memory) |
| Concurrency | One PC connection at a time; within a connection, text/file/remote channels are available concurrently |
| Disconnect policy | No automatic reconnect; a disconnect only cleans up + notifies, and the user reconnects manually |
| Data security | USB link + loopback binding + token; SSH credentials are used only in memory and never written to disk; private keys are not written into the app directory |
| Observability | Key events (connect/disconnect, file completion, remote channel open/close, error codes) are visible in the PC UI; Android-side exceptions are printed to Logcat |

### 1.4 Exception scenarios and handling strategy

| Scenario | Handling |
|---|---|
| Phone USB debugging off / not authorized | `adb devices` state is not `device`; the PC prompts to authorize first and does not enter the connection flow |
| adb forward failure / leftovers | Before connecting, run `adb forward --remove-all` and then establish it, to avoid port occupation |
| Token mismatch | The phone replies `ERROR{code:BAD_TOKEN}` and closes the connection; the PC reports a token error |
| Target host unreachable / port refused | `ERROR{code:HOST_UNREACHABLE}` |
| Host fingerprint unknown | `ERROR{code:UNKNOWN_HOST, host, fingerprint}` → PC popup confirmation → resend with `trust_fingerprint` |
| Host fingerprint changed | `ERROR{code:HOST_KEY_CHANGED}` → PC shows a red alarm with no "continue" button |
| SSH authentication failure | `ERROR{code:AUTH_FAILED}` |
| Transfer interrupted (disconnect) | Close handles and delete the unfinished temporary files on both ends |
| SHA256 mismatch | Delete the written file and mark `FILE_END`/`ACK` with `ok:false` |
| Disk write failure | `ERROR{code:FILE_WRITE_FAILED}`, keeping the PC-side notification |

### 1.5 Explicitly out of scope

- An adb-free approach (would need root or a custom ROM);
- Resume-after-interruption, parallel multi-device support, file directory tree browsing;
- Full terminal emulation on the phone (ANSI rendering of full-screen programs such as vim/htop), see §8.

## 2. Overall Architecture

The most reliable way to carry USB communication at the application layer is **ADB port forwarding**: the PC maps a local TCP port to a phone port via `adb forward`, after which both sides communicate like ordinary TCP sockets, with the USB cable carrying the actual data.

```
┌────────────┐  adb forward   ┌──────────────────┐  Network(WiFi/cellular)  ┌────────────┐
│ PC client  │◄────USB───────►│ Phone Bridge svc │◄────────────────────────►│  Remote    │
│  (PyQt5)   │ 127.0.0.1:12580│ 127.0.0.1:9999   │      SSH (JSch)          │  server    │
└────────────┘ = phone:9999   └──────────────────┘                          │  sshd:22   │
                                                                            └────────────┘
```

- **Features 1/2 (bidirectional)**: PC ↔ phone files and text share **the same established TCP connection** in both directions (the protocol itself is full-duplex);
  the phone does not need to connect back to the PC, and **does not need `adb reverse`** — the phone holds the socket returned by `accept()` and simply sends on it.
- **Feature 3 (product path)**: SSH profiles, MFA, and host trust live **on the phone**. The phone starts/stops long-lived sessions in `ConnectionHub` (survive USB disconnect). The PC lists running connection IDs and **attaches by ID**; interactive I/O uses `REMOTE_DATA` / `REMOTE_OUTPUT` with a pyte terminal. The PC never receives SSH secrets. Legacy credential-bearing `REMOTE_OPEN` from the PC is retained in the protocol for internal/legacy use but is not the product UI path.
- The phone-side listening port is bound to the **loopback address**, and connections arriving via `adb forward` are treated as the only trusted source, with a second token check (see §7).

## 3. Custom Application-Layer Protocol (identical on both ends)

**Frame format (big-endian):**

```
+----------+---------+-------------+-------------------+-------------+---------+
| magic 2B | type 1B | headerLen 2B| header(JSON,UTF-8)| payloadLen 4B| payload |
| AB CD    |         |             |                   |             |         |
+----------+---------+-------------+-------------------+-------------+---------+
```

**Message types:**

| type | Value | Direction | Description |
|---|---|---|---|
| HELLO / ACK | 0x00 / 0x20 | PC→phone / reply | Handshake, header: `{client, version, token}`; the phone verifies the token |
| TEXT | 0x01 | Bidirectional | header: `{id, text}` |
| FILE_META / CHUNK / END | 0x02/03/04 | Bidirectional | Chunked file transfer, SHA256 verified on both ends |
| REMOTE_OPEN | 0x10 | PC→phone | Legacy/internal: `{channel, kind: ssh/exec, host, …credentials…}`. Product path uses phone-managed profiles + `CONN_ATTACH` instead |
| REMOTE_DATA | 0x11 | PC→phone | stdin data (payload) |
| REMOTE_OUTPUT | 0x12 | phone→PC | stdout/stderr (payload, distinguished by `stream`) |
| REMOTE_CLOSE | 0x13 | Bidirectional | Close the channel, header: `{channel, code, reason}` |
| CONN_LIST | 0x14 | PC→phone | `{}` — request phone connection profiles + live state |
| CONN_LIST_RESULT | 0x15 | phone→PC | `{connections:[{id,name,host,port,user,state}]}` (`stopped`\|`starting`\|`running`\|`error`) |
| CONN_ATTACH | 0x16 | PC→phone | `{channel, connection_id}` — attach to a **running** hub session; ack may include backlog bytes |
| CONN_DETACH | 0x17 | PC→phone | `{channel}` — detach PC from hub session (SSH stays up until phone Stop) |
| ERROR | 0x21 | Bidirectional | `{code, message, channel?, host?, fingerprint?}` |
| PING / PONG | 0x30/0x31 | Bidirectional | Heartbeat |

**ERROR code conventions:**

| code | Meaning |
|---|---|
| `BAD_TOKEN` | Handshake token mismatch (the phone then disconnects) |
| `BAD_FRAME` | Frame parse failure (bad magic/length); the current implementation closes the connection directly, this code is reserved |
| `UNKNOWN_HOST` | Target host fingerprint is not in the known-hosts store (managed Start: confirm on phone) |
| `CONN_NOT_RUNNING` | `CONN_ATTACH` for an ID that is not live |
| `CONN_BUSY` | Another PC channel is already attached to that connection (v1: one attach) |
| `CONN_NOT_FOUND` | Unknown connection ID |
| `HOST_KEY_CHANGED` | Target host fingerprint differs from the known one (suspected MITM), connection refused |
| `AUTH_FAILED` | SSH authentication failed |
| `HOST_UNREACHABLE` | Target host unreachable / port refused |
| `CHANNEL_OPEN_FAILED` | Channel open failed (other) |
| `FILE_WRITE_FAILED` | Phone-side write to disk failed |

**id namespace conventions (to avoid id collisions in both directions):**

- PC allocates: `1 ~ 0x3FFFFFFF` (`_next_id` increments from 1);
- Phone allocates: `0x40000000 ~` (`FrameIO.nextId()` increments from `0x40000000`).

When both directions transfer files at the same time, the `FILE_*` `id` and the `REMOTE_*` `channel` never collide, so the UI can display progress in the correct row per id.

**JSON null pitfall:** for optional fields, always "omit it rather than sending null" — do not write `null` — because on the Android side `JSONObject.optString("x")` returns the string `"null"` for JSON `null`. The PC therefore filters out `None` values when building headers.

---

## 4. PC-Side Implementation

Dependency list `requirements.txt`:

```
PyQt5>=5.15
```

You also need [adb](https://developer.android.com/tools/releases/platform-tools) installed and on PATH (`adb version` working is enough; there is no hard minimum version).

### `protocol.py`

```python
import json, struct

MAGIC = b"\xAB\xCD"

class MsgType:
    HELLO, TEXT = 0x00, 0x01
    FILE_META, FILE_CHUNK, FILE_END = 0x02, 0x03, 0x04
    REMOTE_OPEN, REMOTE_DATA, REMOTE_OUTPUT, REMOTE_CLOSE = 0x10, 0x11, 0x12, 0x13
    ACK, ERROR = 0x20, 0x21
    PING, PONG = 0x30, 0x31

class ErrCode:
    BAD_TOKEN = "BAD_TOKEN"
    BAD_FRAME = "BAD_FRAME"
    UNKNOWN_HOST = "UNKNOWN_HOST"
    HOST_KEY_CHANGED = "HOST_KEY_CHANGED"
    AUTH_FAILED = "AUTH_FAILED"
    HOST_UNREACHABLE = "HOST_UNREACHABLE"
    CHANNEL_OPEN_FAILED = "CHANNEL_OPEN_FAILED"

def encode_frame(msg_type: int, header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, ensure_ascii=False).encode("utf-8")
    return (MAGIC + struct.pack(">BH", msg_type, len(h))
            + h + struct.pack(">I", len(payload)) + payload)

def decode_frame(read):
    """read(n) returns exactly n bytes, otherwise it raises."""
    if read(2) != MAGIC:
        raise IOError("protocol magic mismatch")
    msg_type, hlen = struct.unpack(">BH", read(3))   # type 1B + headerLen 2B
    header = json.loads(read(hlen).decode("utf-8")) if hlen else {}
    (plen,) = struct.unpack(">I", read(4))
    return msg_type, header, (read(plen) if plen else b"")
```

> The field order must match the Kotlin implementation exactly: `magic(2) | type(1) | headerLen(2) | header | payloadLen(4) | payload`, total length `9 + headerLen + payloadLen`.

### `transport.py`

```python
import socket, threading
from protocol import decode_frame

class Transport:
    def __init__(self, host, port, on_frame=None, on_disconnect=None):
        self.host, self.port = host, port
        self.on_frame, self.on_disconnect = on_frame, on_disconnect
        self.sock, self._running = None, False
        self._send_lock = threading.Lock()

    @property
    def alive(self):
        return self._running

    def start(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.settimeout(None)
        self._running = True
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def send(self, msg_type, header, payload=b""):
        data = encode_frame(msg_type, header, payload)
        with self._send_lock:
            self.sock.sendall(data)

    def close(self):
        self._running = False
        try: self.sock.close()
        except Exception: pass

    def _recv_loop(self):
        f = self.sock.makefile("rb")
        def read(n):
            data = f.read(n)
            if len(data) < n:
                raise ConnectionError("peer closed")
            return data
        try:
            while self._running:
                t, h, p = decode_frame(read)
                self.on_frame and self.on_frame(t, h, p)
        except Exception:
            pass
        finally:
            self.close()
            self.on_disconnect and self.on_disconnect()
```

### `adb_manager.py`

```python
import shutil, subprocess

class Adb:
    def __init__(self, path="adb"):
        self.path = shutil.which(path) or path

    def _run(self, *args, check=True):
        return subprocess.run([self.path, *args], capture_output=True, text=True, check=check)

    def devices(self):
        out = []
        for line in self._run("devices", "-l").stdout.splitlines()[1:]:
            if line.strip():
                p = line.split()
                out.append({"serial": p[0], "state": p[1], "desc": line})
        return out

    def forward(self, serial, local_port, remote_port):
        """Forward the PC's local_port to remote_port on the phone (clears leftover forwards first)"""
        self._run("-s", serial, "forward", "--remove-all", check=False)
        self._run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}")

    def remove_forward(self, serial, local_port):
        self._run("-s", serial, "forward", "--remove", f"tcp:{local_port}", check=False)
```

### `client.py` (core business: text / files / remote channels)

```python
import os, time, threading, hashlib
from protocol import MsgType, ErrCode
from transport import Transport

CHUNK_SIZE = 256 * 1024

class PhoneClient:
    def __init__(self):
        self.tp = None
        self._id_lock = threading.Lock()
        self._next_id = 1                      # PC-side id space: starts at 1
        # ---- Event callbacks (fired on the network thread; the UI layer switches threads itself) ----
        self.on_text = None            # fn(text)
        self.on_hello_ack = None       # fn(info)
        self.on_status = None          # fn(str)
        self.on_file_progress = None   # fn(fid, name, sent, total, direction)
        self.on_file_done = None       # fn(fid, name, ok, direction, saved_path)
        self.on_remote_output = None   # fn(channel, stream, bytes)
        self.on_remote_close = None    # fn(channel, code, reason)
        self.on_remote_error = None    # fn(channel, code, header) channel-level error (incl. host fingerprint confirmation)
        self._recv, self.channels = {}, {}
        self.save_dir = os.path.abspath("downloads")
        os.makedirs(self.save_dir, exist_ok=True)

    # ---------- Connection ----------
    def connect(self, host="127.0.0.1", port=12580, token=""):
        self.tp = Transport(host, port, self._on_frame, self._on_disconnect)
        self.tp.start()
        self.tp.send(MsgType.HELLO, {"client": "pc-client", "version": 1, "token": token})
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def close(self):
        self.tp and self.tp.close()

    def _heartbeat(self):
        while self.tp and self.tp.alive:
            time.sleep(15)
            try: self.tp.send(MsgType.PING, {})
            except Exception: break

    def _cleanup_partial(self):
        """On disconnect, close handles and delete half-written files to avoid leftovers and handle leaks"""
        for st in self._recv.values():
            try: st["fp"].close()
            except Exception: pass
            try:
                if os.path.exists(st["path"]):
                    os.remove(st["path"])
            except Exception: pass
        self._recv.clear()
        self.channels.clear()

    def _on_disconnect(self):
        self._cleanup_partial()
        self.on_status and self.on_status("[Connection closed]")

    def _new_id(self):
        with self._id_lock:
            v = self._next_id; self._next_id += 1
            return v

    # ---------- Feature 2: text messages ----------
    def send_text(self, text: str):
        self.tp.send(MsgType.TEXT, {"id": self._new_id(), "text": text})

    # ---------- Feature 1: file transfer (PC → phone) ----------
    def send_file(self, path: str):
        def worker():
            name, size = os.path.basename(path), os.path.getsize(path)
            fid = self._new_id()
            sha = hashlib.sha256()
            self.tp.send(MsgType.FILE_META, {"id": fid, "name": name, "size": size})
            sent = 0
            try:
                with open(path, "rb") as f:
                    while chunk := f.read(CHUNK_SIZE):
                        sha.update(chunk)
                        self.tp.send(MsgType.FILE_CHUNK, {"id": fid}, chunk)
                        sent += len(chunk)
                        self.on_file_progress and self.on_file_progress(fid, name, sent, size, "up")
                self.tp.send(MsgType.FILE_END, {"id": fid, "ok": True, "sha256": sha.hexdigest()})
                self.on_file_done and self.on_file_done(fid, name, True, "up", "")
            except Exception as e:
                self.on_file_done and self.on_file_done(fid, name, False, "up", str(e))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- File receive (phone → PC) ----------
    def _on_file_meta(self, h):
        safe = os.path.basename(h.get("name", "unnamed"))
        path = os.path.join(self.save_dir, safe)
        stem, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(path):          # Keep the same name(1).ext rule as the phone side
            path = f"{stem}({i}){ext}"; i += 1
        self._recv[h["id"]] = {"fp": open(path, "wb"), "name": safe,
                               "size": h.get("size", 0), "got": 0,
                               "path": path, "sha": hashlib.sha256()}

    def _on_file_end(self, h):
        st = self._recv.pop(h["id"], None)
        if not st: return
        st["fp"].close()
        ok = bool(h.get("ok")) and (not h.get("sha256") or h["sha256"] == st["sha"].hexdigest())
        if not ok:
            try: os.remove(st["path"])
            except Exception: pass
        self.on_file_done and self.on_file_done(
            h["id"], st["name"], ok, "down", st["path"] if ok else "")

    # ---------- Feature 3: remote calls (phone as proxy) ----------
    def remote_open(self, kind: str, trust_fingerprint: str = None, **params) -> int:
        ch = self._new_id()
        header = {"channel": ch, "kind": kind, **params}
        if trust_fingerprint:                 # Do not pass None, to avoid the JSON null pitfall
            header["trust_fingerprint"] = trust_fingerprint
        self.channels[ch] = header
        self.tp.send(MsgType.REMOTE_OPEN, header)
        return ch

    def remote_input(self, ch: int, data: bytes):
        self.tp.send(MsgType.REMOTE_DATA, {"channel": ch}, data)

    def remote_close(self, ch: int):
        if ch in self.channels:
            self.tp.send(MsgType.REMOTE_CLOSE, {"channel": ch})
            self.channels.pop(ch, None)

    # ---------- Message dispatch ----------
    def _on_frame(self, t, h, p):
        M = MsgType
        try:
            if   t == M.TEXT:          self.on_text and self.on_text(h.get("text", ""))
            elif t == M.ACK:           "device" in h and self.on_hello_ack and self.on_hello_ack(h)
            elif t == M.FILE_META:     self._on_file_meta(h)
            elif t == M.FILE_CHUNK:
                st = self._recv.get(h["id"])
                if st:
                    st["fp"].write(p); st["sha"].update(p); st["got"] += len(p)
                    self.on_file_progress and self.on_file_progress(
                        h["id"], st["name"], st["got"], st["size"], "down")
            elif t == M.FILE_END:      self._on_file_end(h)
            elif t == M.REMOTE_OUTPUT: self.on_remote_output and self.on_remote_output(h["channel"], h.get("stream", "out"), p)
            elif t == M.REMOTE_CLOSE:
                self.channels.pop(h["channel"], None)
                self.on_remote_close and self.on_remote_close(h["channel"], h.get("code", 0), h.get("reason", ""))
            elif t == M.PING:          self.tp.send(M.PONG, {})
            elif t == M.ERROR:
                if "channel" in h and self.on_remote_error:
                    self.on_remote_error(h["channel"], h.get("code", ""), h)
                else:
                    self.on_status and self.on_status(
                        f"[Error] {h.get('code','')} {h.get('message','')}")
        except Exception as e:
            self.on_status and self.on_status(f"[Handler exception] {e}")
```

### `ui.py` (PyQt5 UI, three tabs for the three features)

```python
import sys
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QComboBox, QPushButton, QLabel, QTabWidget, QPlainTextEdit,
    QLineEdit, QProgressBar, QFileDialog, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox)

from adb_manager import Adb
from client import PhoneClient

PC_PORT, PHONE_PORT = 12580, 9999

class MainWindow(QMainWindow):
    sig_text   = pyqtSignal(str)
    sig_status = pyqtSignal(str)
    sig_fprog  = pyqtSignal(object, str, int, int, str)
    sig_fdone  = pyqtSignal(object, str, bool, str, str)
    sig_rout   = pyqtSignal(object, str, bytes)
    sig_rclose = pyqtSignal(object, int, str)
    sig_rerr   = pyqtSignal(object, str, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("USB Bridge Client"); self.resize(820, 600)
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
        self.ed_token = QLineEdit(); self.ed_token.setPlaceholderText("Phone token")
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
        for x in (self.ed_host, self.ed_port, self.ed_user, self.ed_pass): h.addWidget(x)
        v.addLayout(h)

        h2 = QHBoxLayout()
        self.chk_key = QCheckBox("Use private key")
        self.ed_key = QLineEdit(); self.ed_key.setPlaceholderText("Private key file (OpenSSH/PKCS#8 PEM)")
        bk = QPushButton("Choose..."); bk.clicked.connect(self.pick_key)
        self.ed_phrase = QLineEdit(); self.ed_phrase.setPlaceholderText("Key passphrase (optional)")
        self.ed_phrase.setEchoMode(QLineEdit.Password); self.ed_phrase.setFixedWidth(130)
        for x in (self.chk_key, self.ed_key, bk, self.ed_phrase): h2.addWidget(x)
        h2.setStretch(1, 1)
        v.addLayout(h2)

        h3 = QHBoxLayout()
        b1 = QPushButton("SSH via phone (shell)"); b1.clicked.connect(lambda: self.open_remote("ssh"))
        self.ed_cmd = QLineEdit(); self.ed_cmd.setPlaceholderText("One-shot command, e.g. uname -a")
        b2 = QPushButton("Run exec once"); b2.clicked.connect(lambda: self.open_remote("exec"))
        b3 = QPushButton("Disconnect"); b3.clicked.connect(self.close_channel)
        for x in (b1, self.ed_cmd, b2, b3): h3.addWidget(x)
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
        self.term.insertPlainText(data.decode("utf-8", "ignore"))
        self.term.moveCursor(self.term.textCursor().End)

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
        for d in self.adb.devices():
            if d["state"] == "device":
                self.cmb.addItem(d["desc"], d["serial"])

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
            self.adb.remove_forward(serial, PC_PORT)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow(); w.show(); sys.exit(app.exec_())
```

> Note the "resend after fingerprint confirmation" approach in `_on_rerr`: after merging the confirmed `trust_fingerprint` into the header, `remote_open` is called again (each call allocates a new `channel`; the old channel is cleaned up by the phone in the error branch).

---

## 5. Android-Side Implementation

Add to `build.gradle`: `implementation("com.jcraft:jsch:0.1.55")`

`AndroidManifest.xml`:

```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC"/>
<uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>
<!-- inside application -->
<service android:name=".BridgeService" android:exported="false"
         android:foregroundServiceType="dataSync"/>
```

### `FrameIO.kt` (protocol mirror implementation)

```kotlin
package com.example.usbbridge

import org.json.JSONObject
import java.io.*
import java.util.concurrent.atomic.AtomicInteger

object FrameIO {
    const val HELLO=0x00; const val TEXT=0x01
    const val FILE_META=0x02; const val FILE_CHUNK=0x03; const val FILE_END=0x04
    const val REMOTE_OPEN=0x10; const val REMOTE_DATA=0x11
    const val REMOTE_OUTPUT=0x12; const val REMOTE_CLOSE=0x13
    const val ACK=0x20; const val ERROR=0x21; const val PING=0x30; const val PONG=0x31

    /** Phone-side id space starts at 0x40000000 to avoid colliding with the PC side (starting at 1) */
    private val idSeq = AtomicInteger(0x40000000)
    fun nextId(): Int = idSeq.getAndIncrement()

    data class Frame(val type: Int, val header: JSONObject, val payload: ByteArray)

    fun encode(type: Int, header: JSONObject, payload: ByteArray = ByteArray(0)): ByteArray {
        val h = header.toString().toByteArray(Charsets.UTF_8)
        // Layout: magic(2) | type(1) | headerLen(2) | header | payloadLen(4) | payload
        return ByteArray(9 + h.size + payload.size).apply {
            var i = 0
            fun u1(v: Int) { this[i++] = v.toByte() }
            fun u2(v: Int) { u1(v shr 8); u1(v) }
            fun u4(v: Int) { u2(v shr 16); u2(v) }
            u1(0xAB); u1(0xCD); u1(type); u2(h.size)
            h.copyInto(this, i); i += h.size          // Must write at i, not a hardcoded offset
            u4(payload.size); payload.copyInto(this, i)
        }
    }

    fun readFrame(input: InputStream): Frame? {
        val magic = ByteArray(2)
        if (!fill(input, magic)) return null                       // Peer closed
        if (magic[0] != 0xAB.toByte() || magic[1] != 0xCD.toByte()) throw IOException("bad magic")
        val type = readByte(input) ?: return null
        val hLen = readShort(input)
        val header = if (hLen > 0) JSONObject(String(readN(input, hLen), Charsets.UTF_8)) else JSONObject()
        val pLen = readInt(input)
        return Frame(type, header, if (pLen > 0) readN(input, pLen) else ByteArray(0))
    }

    private fun fill(input: InputStream, buf: ByteArray): Boolean {
        var off = 0
        while (off < buf.size) {
            val n = input.read(buf, off, buf.size - off)
            if (n < 0) return false
            off += n
        }
        return true
    }
    private fun readByte(s: InputStream): Int? { val b=ByteArray(1); return if (fill(s,b)) b[0].toInt() and 0xFF else null }
    private fun readShort(s: InputStream): Int { val b=ByteArray(2); require(fill(s,b)); return ((b[0].toInt() and 0xFF) shl 8) or (b[1].toInt() and 0xFF) }
    private fun readInt(s: InputStream): Int { val b=ByteArray(4); require(fill(s,b)); return ((b[0].toInt() and 0xFF) shl 24) or ((b[1].toInt() and 0xFF) shl 16) or ((b[2].toInt() and 0xFF) shl 8) or (b[3].toInt() and 0xFF) }
    private fun readN(s: InputStream, n: Int): ByteArray { val b=ByteArray(n); require(fill(s,b)); return b }
}
```

### `TofuHostKeys.kt` (known-hosts store: first use requires PC-side fingerprint confirmation)

```kotlin
package com.example.usbbridge

import android.util.Base64
import com.jcraft.jsch.HostKey
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.UserInfo
import java.io.File
import java.security.MessageDigest

/**
 * TOFU (Trust On First Use) known-hosts store.
 * Storage format: one "host<TAB>SHA256:xxx" per line, matching OpenSSH's SHA256 fingerprint,
 * so users can cross-check with `ssh-keygen -lf`.
 *
 * @param approvedFingerprint fingerprint already confirmed on the PC side; only written and allowed through when it equals the actual fingerprint.
 */
class TofuHostKeys(
    private val store: File,
    private val approvedFingerprint: String? = null
) : HostKeyRepository {

    private val known = HashMap<String, String>()

    @Volatile var lastHost: String? = null; private set
    @Volatile var lastFingerprint: String? = null; private set
    @Volatile var lastResult: Int = OK; private set

    init {
        if (store.exists()) {
            store.readLines().forEach { line ->
                val parts = line.split('\t')
                if (parts.size == 2 && parts[1].isNotBlank()) known[parts[0]] = parts[1]
            }
        }
    }

    override fun check(host: String, key: ByteArray): Int {
        val fp = fingerprint(key)
        lastHost = host; lastFingerprint = fp
        val recorded = known[host]
        lastResult = when {
            recorded == null && approvedFingerprint == fp -> { trust(host, fp); OK }
            recorded == null -> NOT_INCLUDED                    // Unknown host: leave to PC side for confirmation
            recorded == fp -> OK
            else -> CHANGED                                     // Fingerprint changed: reject
        }
        return lastResult
    }

    fun trust(host: String, fingerprint: String) {
        known[host] = fingerprint
        store.writeText(known.entries.joinToString("\n") { "${it.key}\t${it.value}" })
    }

    /** HostKey's key field is protected and unreadable from outside, so reuse the fingerprint cached during check() */
    override fun add(hostkey: HostKey, ui: UserInfo?) {
        val h = lastHost ?: return
        val fp = lastFingerprint ?: return
        trust(h, fp)
    }
    override fun remove(host: String, type: String?) = known.remove(host).let { persist() }
    override fun remove(host: String, type: String?, key: ByteArray?) { known.remove(host); persist() }
    override fun getKnownHostsRepositoryID(): String = store.absolutePath
    override fun getHostKey(): Array<HostKey> = emptyArray()
    override fun getHostKey(host: String?, type: String?): Array<HostKey> = emptyArray()

    private fun persist() {
        store.writeText(known.entries.joinToString("\n") { "${it.key}\t${it.value}" })
    }

    companion object {
        fun fingerprint(key: ByteArray): String = "SHA256:" + Base64.encodeToString(
            MessageDigest.getInstance("SHA-256").digest(key),
            Base64.NO_WRAP or Base64.NO_PADDING)
    }
}
```

### `RemoteSession.kt` (JSch SSH proxy, core of feature 3)

```kotlin
package com.example.usbbridge

import com.jcraft.jsch.*
import java.io.PipedInputStream
import java.io.PipedOutputStream

class RemoteSession(
    private val hostKeys: TofuHostKeys,
    private val onOutput: (stream: String, data: ByteArray) -> Unit,
    private val onClose: (code: Int, reason: String) -> Unit
) {
    private var session: Session? = null
    private var channel: Channel? = null
    private val stdin = PipedOutputStream()
    private val stdinPipe = PipedInputStream(stdin, 1 shl 20)   // 1MB, reduces the chance of blocking on backpressure

    /** Interactive shell: PC's REMOTE_DATA is written to stdin, remote output is sent back via onOutput */
    fun openSsh(host: String, port: Int, user: String, auth: Auth) = connect(host, port, user, auth) {
        val ch = session!!.openChannel("shell") as ChannelShell
        ch.setPty(true)
        ch.setPtySize(120, 30, 0, 0)          // cols/rows: match the PC terminal width so full-screen programs are not truncated
        runCatching { ch.setEnv("TERM", "xterm-256color") }   // Ignored when AcceptEnv is not enabled on the remote
        ch.setInputStream(stdinPipe)
        ch.setOutputStream(fwdStream("out"))
        ch.connect(10_000)
        channel = ch
        watchClose(ch) { onClose(0, "shell exited") }
    }

    /** One-shot command execution: kind=exec (supports stdin, e.g. sudo -S, docker exec -i) */
    fun execSsh(host: String, port: Int, user: String, auth: Auth, command: String) =
        connect(host, port, user, auth) {
            val ch = session!!.openChannel("exec") as ChannelExec
            ch.setCommand(command)
            ch.setInputStream(stdinPipe)
            ch.setOutputStream(fwdStream("out"))
            ch.setErrStream(fwdStream("err"))
            ch.connect(10_000)
            channel = ch
            watchClose(ch) { onClose(ch.exitStatus, "exec finished") }
        }

    data class Auth(val password: String?, val privateKey: String?, val passphrase: String?)

    private fun connect(host: String, port: Int, user: String, auth: Auth, block: () -> Unit) {
        val jsch = JSch()
        if (!auth.privateKey.isNullOrBlank()) {           // Private-key auth (contents exist only in memory)
            jsch.addIdentity("bridge", auth.privateKey.toByteArray(),
                null, auth.passphrase?.takeIf { it.isNotEmpty() }?.toByteArray())
        }
        val s = jsch.getSession(user, host, port)
        auth.password?.takeIf { it.isNotEmpty() }?.let { s.setPassword(it) }
        s.setConfig("StrictHostKeyChecking", "yes")       // Whether to allow is decided by TofuHostKeys
        s.setHostKeyRepository(hostKeys)
        s.connect(10_000)
        session = s
        block()
    }

    /** Wait for the channel to end on a dedicated thread to avoid occupying the calling thread */
    private fun watchClose(ch: Channel, done: () -> Unit) = Thread {
        while (ch.isConnected) {
            try { Thread.sleep(200) } catch (_: InterruptedException) { return@Thread }
        }
        done()
    }.apply { isDaemon = true }.start()

    private fun fwdStream(stream: String) = object : java.io.OutputStream() {
        override fun write(b: Int) = write(byteArrayOf(b.toByte()))
        override fun write(b: ByteArray, off: Int, len: Int) = onOutput(stream, b.copyOfRange(off, off + len))
    }

    fun writeStdin(data: ByteArray) { stdin.write(data); stdin.flush() }

    fun close() {
        runCatching { stdin.close() }
        runCatching { channel?.disconnect() }
        runCatching { session?.disconnect() }
    }
}
```

### `BridgeService.kt` (foreground service + session handling)

```kotlin
package com.example.usbbridge

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.*
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.JSchException
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors

class BridgeService : Service() {
    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        if (token.isEmpty()) token = randomToken()
        if (Build.VERSION.SDK_INT >= 26) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "USB Bridge", NotificationManager.IMPORTANCE_LOW))
        }
        startForeground(1, buildNotification(this, "USB Bridge running (127.0.0.1:$PORT)", "Token: $token"))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running) return START_STICKY          // Re-entry guard: repeated button taps / system restart won't start a second listener
        running = true
        Thread {
            try {
                // Bind loopback only: device-side adb forward reconnects via 127.0.0.1, and other apps cannot connect directly
                val srv = ServerSocket(PORT, 8, InetAddress.getByName("127.0.0.1"))
                server = srv
                while (running) {
                    val sock = srv.accept()
                    val handler = SessionHandler(applicationContext, sock)
                    addSession(handler)
                    pool.execute(handler)
                }
            } catch (e: Exception) {
                if (running) e.printStackTrace()
            } finally {
                running = false
            }
        }.start()
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        runCatching { server?.close() }
        sessions.forEach { runCatching { it.close() } }
        sessions.clear()
        super.onDestroy()
    }

    companion object {
        const val PORT = 9999
        const val CHANNEL_ID = "usb_bridge"

        @Volatile var token: String = ""
            private set
        @Volatile var running: Boolean = false
            private set

        private var server: ServerSocket? = null
        private val pool = Executors.newCachedThreadPool()
        private val sessions = CopyOnWriteArrayList<SessionHandler>()

        fun randomToken(): String =
            ByteArray(4).also { SecureRandom().nextBytes(it) }
                .joinToString("") { "%02x".format(it) }

        /** Shared thread pool for SessionHandler (time-consuming phone-side operations like sending files must not occupy the service thread) */
        fun poolExecute(task: Runnable) { pool.execute(task) }

        fun addSession(s: SessionHandler) { sessions.add(s) }
        fun removeSession(s: SessionHandler) { sessions.remove(s) }

        /** Phone-side text/file send (reuses the already-established connection; the PC side already has receive logic) */
        fun broadcastText(text: String) = sessions.forEach { it.sendText(text) }
        fun broadcastFile(file: File, cleanup: Boolean = false) =
            sessions.forEach { it.sendFile(file, cleanup) }

        fun buildNotification(ctx: Context, title: String, text: String): Notification =
            if (Build.VERSION.SDK_INT >= 26)
                Notification.Builder(ctx, CHANNEL_ID)
                    .setContentTitle(title).setContentText(text)
                    .setSmallIcon(android.R.drawable.ic_menu_computer).build()
            else @Suppress("DEPRECATION")
                Notification.Builder(ctx)
                    .setContentTitle(title).setContentText(text)
                    .setSmallIcon(android.R.drawable.ic_menu_computer).build()
    }
}

class SessionHandler(private val ctx: Context, private val sock: java.net.Socket) : Runnable, Closeable {
    private val out: OutputStream = sock.getOutputStream()
    private val outLock = Any()
    private val remotes = ConcurrentHashMap<Int, RemoteSession>()
    private val sinks = ConcurrentHashMap<Int, FileSink>()
    /** Remote channel operations run serially: guarantees OPEN→DATA ordering without blocking the main read loop (TEXT/FILE/PING are unaffected by the SSH handshake) */
    private val remoteOps = Executors.newSingleThreadExecutor()
    @Volatile private var closed = false
    @Volatile private var authed = false

    override fun run() {
        try {
            val input = sock.getInputStream()
            while (!closed) handle(FrameIO.readFrame(input) ?: break)
        } catch (e: Exception) {
            if (!closed) e.printStackTrace()
        } finally { close() }
    }

    fun send(type: Int, header: JSONObject, payload: ByteArray = ByteArray(0)) {
        if (closed) return
        val data = FrameIO.encode(type, header, payload)
        synchronized(outLock) { out.write(data); out.flush() }
    }

    private fun handle(f: FrameIO.Frame) = with(f.header) {
        // The first frame must be a HELLO with the correct token, otherwise reject
        if (!authed) {
            if (f.type == FrameIO.HELLO && optString("token") == BridgeService.token) {
                authed = true
                send(FrameIO.ACK, JSONObject().put("ok", true).put("device", Build.MODEL))
            } else {
                send(FrameIO.ERROR, JSONObject().put("code", "BAD_TOKEN").put("message", "token verification failed"))
                close()
            }
            return
        }
        when (f.type) {
            FrameIO.PING  -> send(FrameIO.PONG, JSONObject())
            FrameIO.TEXT  -> {                                     // PC → phone text
                notifyText(ctx, optString("text"))
                send(FrameIO.ACK, JSONObject().put("id", optInt("id")))
            }
            FrameIO.FILE_META  -> {
                val id = optInt("id")
                try { sinks[id] = FileSink.create(ctx, f.header) }        // ctx uses the outer SessionHandler property
                catch (e: Exception) {                                 // Disk full / no permission, etc.
                    send(FrameIO.ERROR, JSONObject().put("code", "FILE_WRITE_FAILED")
                        .put("id", id).put("message", e.message ?: ""))
                }
            }
            FrameIO.FILE_CHUNK -> sinks[optInt("id")]?.append(f.payload)
            FrameIO.FILE_END   -> {
                val id = optInt("id")
                val path = sinks.remove(id)?.finish(optBoolean("ok"), optString("sha256"))
                send(FrameIO.ACK, JSONObject()
                    .put("file_id", id).put("ok", path != null).put("path", path ?: ""))
            }
            FrameIO.REMOTE_OPEN  -> { val fr = f; remoteOps.execute { openRemote(fr) } }
            FrameIO.REMOTE_DATA  -> { val d = f.payload; val c = optInt("channel")
                                      remoteOps.execute { runCatching { remotes[c]?.writeStdin(d) } } }
            FrameIO.REMOTE_CLOSE -> { val c = optInt("channel")
                                      remoteOps.execute { remotes.remove(c)?.close() } }
        }
    }

    // ---------- Feature 1/2: phone → PC text and files ----------
    fun sendText(text: String) =
        send(FrameIO.TEXT, JSONObject().put("id", FrameIO.nextId()).put("text", text))

    fun sendFile(file: File, cleanup: Boolean = false) {
        BridgeService.poolExecute {
            val id = FrameIO.nextId()
            runCatching {
                val md = MessageDigest.getInstance("SHA-256")
                send(FrameIO.FILE_META, JSONObject()
                    .put("id", id).put("name", file.name).put("size", file.length()))
                file.inputStream().use { ins ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        val n = ins.read(buf); if (n <= 0) break
                        md.update(buf, 0, n)
                        send(FrameIO.FILE_CHUNK, JSONObject().put("id", id), buf.copyOf(n))
                    }
                }
                val hex = md.digest().joinToString("") { "%02x".format(it) }
                send(FrameIO.FILE_END, JSONObject().put("id", id).put("ok", true).put("sha256", hex))
            }.onFailure {
                send(FrameIO.FILE_END, JSONObject().put("id", id).put("ok", false)
                    .put("error", it.message ?: ""))
            }
            if (cleanup) runCatching { file.delete() }       // Clean up after sending the cached copy
        }
    }

    // ---------- Feature 3: remote session ----------
    private fun openRemote(f: FrameIO.Frame) = with(f.header) {
        val ch = optInt("channel")
        val host = optString("host")
        val trust = if (has("trust_fingerprint") && !isNull("trust_fingerprint")) optString("trust_fingerprint") else null
        val auth = RemoteSession.Auth(
            password = if (has("password")) optString("password") else null,
            privateKey = if (has("private_key")) optString("private_key") else null,
            passphrase = if (has("passphrase")) optString("passphrase") else null)
        val hostKeys = TofuHostKeys(File(ctx.filesDir, "known_hosts"), trust)
        val rs = RemoteSession(hostKeys,
            { stream, data -> send(FrameIO.REMOTE_OUTPUT,
                JSONObject().put("channel", ch).put("stream", stream), data) },
            { code, reason ->
                send(FrameIO.REMOTE_CLOSE, JSONObject().put("channel", ch)
                    .put("code", code).put("reason", reason))
                remotes.remove(ch)
            })
        try {
            if (optString("kind") == "exec")
                rs.execSsh(host, optInt("port", 22), optString("user"), auth, optString("command"))
            else
                rs.openSsh(host, optInt("port", 22), optString("user"), auth)
            remotes[ch] = rs
        } catch (e: Exception) {
            rs.close()
            remotes.remove(ch)
            val code = when {
                hostKeys.lastResult == HostKeyRepository.CHANGED -> "HOST_KEY_CHANGED"
                hostKeys.lastResult == HostKeyRepository.NOT_INCLUDED -> "UNKNOWN_HOST"
                e is JSchException && e.message.orEmpty().contains("Auth fail", true) -> "AUTH_FAILED"
                e.message.orEmpty().let { m ->
                    m.contains("Connection refused", true) || m.contains("UnknownHost", true) ||
                        m.contains("timeout", true) || m.contains("Network is unreachable", true) } -> "HOST_UNREACHABLE"
                else -> "CHANNEL_OPEN_FAILED"
            }
            send(FrameIO.ERROR, JSONObject().put("channel", ch).put("code", code)
                .put("message", e.message ?: "").put("host", host)
                .put("fingerprint", hostKeys.lastFingerprint ?: ""))
        }
    }

    private fun notifyText(ctx: Context, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED) return       // Silently skip when unauthorized; the UI still has the log
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify((System.currentTimeMillis() and 0xFFFF).toInt(),
            BridgeService.buildNotification(ctx, "Message from PC", text))
    }

    override fun close() {
        if (closed) return; closed = true
        remoteOps.shutdownNow()
        remotes.values.forEach { it.close() }
        sinks.values.forEach { it.abort() }
        BridgeService.removeSession(this)
        runCatching { sock.close() }
    }
}

/** Write file to disk (app external private dir, no storage permission needed); with SHA256 verification and the name(1).ext duplicate-name rule */
class FileSink private constructor(private val file: File) {
    private val fos = file.outputStream()
    private val md = MessageDigest.getInstance("SHA-256")

    fun append(chunk: ByteArray) = synchronized(fos) { fos.write(chunk); md.update(chunk) }

    fun finish(ok: Boolean, sha256: String?): String? = synchronized(fos) {
        fos.close()
        val actual = md.digest().joinToString("") { "%02x".format(it) }
        val good = ok && (sha256.isNullOrEmpty() || sha256 == actual)
        if (!good) { file.delete(); null } else file.absolutePath
    }

    fun abort() { runCatching { fos.close() }; runCatching { file.delete() } }

    companion object {
        fun createDir(ctx: Context): File = ctx.getExternalFilesDir(null) ?: ctx.filesDir

        /** Consistent with the PC side: a.txt → a(1).txt */
        fun uniqueName(dir: File, name: String): File {
            val safe = File(name).name
            var f = File(dir, safe)
            if (!f.exists()) return f
            val dot = safe.lastIndexOf('.')
            val stem = if (dot > 0) safe.substring(0, dot) else safe
            val ext = if (dot > 0) safe.substring(dot) else ""
            var i = 1
            while (f.exists()) { f = File(dir, "$stem($i)$ext"); i++ }
            return f
        }

        fun create(ctx: Context, h: JSONObject): FileSink =
            FileSink(uniqueName(createDir(ctx), h.optString("name", "file_${h.optInt("id")}")))
    }
}
```

> Key points:
> - `handle` uses `with(f.header)` to unwrap the header fields (`optString`/`optInt` are JSONObject methods), and passes `ctx` explicitly to `FileSink` when writing files.
> - Phone-side file sending goes through `BridgeService`'s thread pool, to avoid blocking the socket read loop.

`MainActivity` (start service + token display + phone-side send initiation):

```kotlin
package com.example.usbbridge

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import java.io.File

class MainActivity : AppCompatActivity() {
    private lateinit var info: TextView
    private lateinit var input: EditText

    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}
    private val pickFile = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri == null) return@registerForActivityResult
        info.text = "Preparing to send: ${displayName(uri)}"
        Thread { runCatching { copyToCache(uri) }
            .onSuccess { BridgeService.broadcastFile(it, cleanup = true); runOnUiThread { info.text = "Send started: ${it.name}" } }
            .onFailure { e -> runOnUiThread { info.text = "Send failed: ${e.message}" } } }.start()
    }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        supportRequestWindowFeature(android.view.Window.FEATURE_NO_TITLE)
        if (Build.VERSION.SDK_INT >= 33)
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)   // Otherwise PC text alerts are not received

        info = TextView(this).apply { textSize = 16f }
        val start = Button(this).apply { text = "Start USB Bridge service" }
        val sendText = Button(this).apply { text = "Send text to PC" }
        val sendFile = Button(this).apply { text = "Send file to PC" }
        input = EditText(this).apply { hint = "Enter text to send to the PC" }

        start.setOnClickListener {
            ContextCompat.startForegroundService(this, Intent(this, BridgeService::class.java))
            info.postDelayed({ info.text = "Service started, listening on 127.0.0.1:9999\nToken: ${BridgeService.token}" }, 300)
        }
        sendText.setOnClickListener {
            val t = input.text.toString().trim()
            if (t.isNotEmpty()) {
                BridgeService.broadcastText(t); input.text.clear(); info.text = "Sent: $t"
            }
        }
        sendFile.setOnClickListener { pickFile.launch("*/*") }

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 40, 40, 40)
            addView(info); addView(start); addView(input); addView(sendText); addView(sendFile)
        })
    }

    private fun displayName(uri: Uri): String =
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (i >= 0 && c.moveToFirst()) c.getString(i) else null
        } ?: "file_${System.currentTimeMillis()}"

    private fun copyToCache(uri: Uri): File {
        val f = File(cacheDir, displayName(uri))
        contentResolver.openInputStream(uri)!!.use { ins -> f.outputStream().use { ins.copyTo(it) } }
        return f
    }
}
```

---

## 6. Deployment and Usage Steps

0. **Build the phone app**: open the `android/` directory in Android Studio (AGP 8.1.4 / Gradle 8.2 / JDK 17, bundled with AS) → Sync → Run to the phone;
   the artifact can also be produced yourself with `gradlew assembleDebug`.
1. **Phone**: install the app → enable developer options / USB debugging → open the app and tap "Start service" (notification permission is requested on first run) → connect the phone to the PC over USB and authorize debugging in the dialog;
   note the **Token** shown in the UI / notification bar.
2. **PC**: `adb devices` to confirm the device is online → `pip install -r requirements.txt` → `python ui.py`
   (if there is no `python` on the command line, use `py ui.py` on Windows; `adb` must be on PATH, or change `adb_manager.Adb(path=...)` to specify an absolute path);
3. Tap "Refresh Devices" → enter the Token shown on the phone → "Connect", then:
   - **Messages**: Tab1 for bidirectional text;
   - **Files**: Tab2 sends a file as soon as it is chosen, with progress shown one row per file; files from the phone are in `./downloads/`; the phone sends them back with "Send file to PC";
   - **Remote proxy**: Tab3 to enter the target server IP/port/account (password or private key) → "SSH via phone (shell)" → type commands in the bottom input box and output streams back in real time;
     the first connection pops up the target host fingerprint; after verifying it with the administrator, choose "Trust and continue", and that host is not asked again;
   - **exec**: enter a one-shot command → "Run exec once"; when the command finishes the channel closes automatically and the exit code is returned.
4. **Disconnect**: tap "Disconnect" on the PC (which also removes the adb forward); swiping the app away / stopping the service on the phone releases the listening port.

## 7. Security and Trust Boundaries

| Boundary | Current state | Notes |
|---|---|---|
| PC ↔ phone link | `adb forward` opens the port only on the PC's 127.0.0.1; the phone's `ServerSocket` binds to `127.0.0.1` | Other apps on the device cannot connect directly; other local processes on the PC can still reach the forwarded port, which is why a token is added on top |
| Link authentication | HELLO carries an 8-hex-digit random token (regenerated on every service start, shown in the phone's notification bar and UI) | A wrong token → `ERROR{BAD_TOKEN}` and immediate disconnect; the token only guards against "other processes on the device", not against someone who can read the screen |
| Credentials | Product path: profiles stored in phone EncryptedSharedPreferences (Keystore); MFA answered on phone Start; **PC never receives SSH secrets** | Legacy `REMOTE_OPEN` with credentials over USB is not the product UI path |
| Host identity | TOFU for managed Starts: fingerprint confirmed on the **phone**, then written to phone `known_hosts`; later connections compare strictly | A fingerprint change (suspected MITM) is refused outright with no "ignore" option; format is OpenSSH `SHA256:...` |
| Plaintext transport | The frame protocol itself is not encrypted | The link is USB + loopback, with no secondary encryption; to work across an untrusted environment, AES-GCM can be added around the `payload` (see §8) |

## 8. Known Limitations and Extensibility Points

| Item | Notes |
|---|---|
| **Serial session** | Within a single connection, `FILE_*`/`TEXT` are handled synchronously in the same read loop, so text messages are delayed while a large file is being written (nothing is lost; TCP provides backpressure). To decouple them completely, file writes could also be handed to a separate queue thread |
| **Remote terminal** | Product path: phone-managed hub + PC attach-by-ID + **pyte** interactive terminal (raw keys, 120×30 PTY). Still not full truecolor / every VT quirk; window-size negotiate is deferred. Sessions outlive USB; Stop on phone ends SSH |
| **Pseudo-terminal parameters** | `setPtySize(120, 30)` is already delivered; `setEnv("TERM", ...)` requires `AcceptEnv` on the target sshd, otherwise it is ignored, and `export TERM=xterm-256color` on the remote can serve as a fallback |
| **Backpressure** | The phone's stdin pipe for `REMOTE_DATA` is 1MB; once full, the writing thread blocks (blocking only the remote channel queue, not text/file/heartbeat); unbounded fast input can still pile up, so switch to a bounded queue + drop policy if needed |
| **Large files / resume** | Already chunked + SHA256 verified on both ends; resuming only requires adding an `offset` field to `FILE_META` and seeking on the receiving side |
| **Multi-device / multi-session** | The server supports multiple connections, but the PC connects to only one device at a time; multiple devices require forwarding each serial to a different local port on the PC |
| **App-free alternative** | Install Termux on the phone and run `sshd`, then after `adb forward` the PC first SSHes into the phone and from there SSHes to the target — quick to validate, but no custom UI or file management |
| **adb-free approach** | Requires phone root or system-level support (e.g. an automotive head unit / custom ROM exposing TCP directly); otherwise ADB is the standard approach |
| **Channel encryption** | To use it over an untrusted network, apply AES-GCM to the `TEXT`/`FILE_*`/`REMOTE_*` payloads, with the key derived from the token (HKDF) |

## 9. Change Log (relative to the initial version)

List of changes in this revision relative to the initial version (issues came from a static review of the initial version):

| Level | Issue | Resolution |
|---|---|---|
| P0 | Kotlin `FrameIO.encode` wrote the header at the fixed offset 9, leaving bytes 5–8 as 0 and the header tail overwritten by payloadLen → PC-side `json.loads` was guaranteed to fail and the protocol did not work at all | `h.copyInto(this, i)`, writing sequentially at `i`; also added a "field order must match exactly" note to the protocol section |
| P1 | The "phone → PC" direction of requirements 1/2 was not implemented, and the initial version suggested working around it with `adb reverse` | Made "reuse the same established connection" explicit: added `SessionHandler.sendText/sendFile` + `BridgeService.broadcast*` + a `MainActivity` send entry point; the protocol section became bidirectional |
| P1 | Both directions incremented ids from 1, so the PC table merged two files into one row when transferring simultaneously | Defined id namespaces: PC from 1, phone from `0x40000000` (`FrameIO.nextId()`) |
| P1 | `kind=exec` had no entry point, and `ChannelExec` had no stdin | Added a "Run exec once" entry point and command input box on the PC; `ChannelExec.setInputStream` supports stdin and the exit code is returned |
| P2 | `ServerSocket(9999)` listened on 0.0.0.0, so any app on the device could freeload on the SSH proxy | Bind `127.0.0.1` + HELLO token verification (`BAD_TOKEN` disconnects immediately) |
| P2 | `REMOTE_DATA` was written in the single read loop, so a full pipe would permanently hang the entire connection | Remote channel operations now run serially on a per-session single-thread `remoteOps` (preserving OPEN→DATA ordering while no longer blocking TEXT/FILE/PING); stdin pipe enlarged to 1MB |
| P2 | `REMOTE_OPEN` synchronously blocked on the SSH handshake for up to 10s, during which heartbeat/text stopped entirely | As above, the handshake now runs in `remoteOps` |
| P2 | `onStartCommand` was re-entrant, so repeated button taps caused a silently failed `BindException` | Added a `running` re-entry flag + exception logging |
| P2 | `StrictHostKeyChecking=no` performed no host verification, so a password could be stolen by a fake host | Introduced `TofuHostKeys` (TOFU) and a PC-side fingerprint confirmation dialog; a fingerprint change is refused outright; added a private-key authentication path |
| P2 | The duplicate-name rule differed between the two ends (`(1)name` vs `name(1).ext`), and the phone did not verify SHA256 | Unified to `name(1).ext`; `FileSink.finish(ok, sha256)` deletes on verification failure |
| P2 | On disconnect only `_recv.clear()` ran, leaking file handles and leaving half a file behind | Added `_cleanup_partial()`: close handles and delete unfinished files |
| P2 | On Android 13+ notification permission was not requested → "text from PC arrived" alerts silently stopped working; `Notification.Builder(ctx, id)` crashed on API<26 | `MainActivity` requests permission at runtime; notification construction branches by API level; notifications are skipped when unauthorized |
| P2 | UI: disconnect did not reset `self.ch`, there was no "Disconnect" button, adb forward was left behind, and a single progress bar could not carry concurrency | Added a Disconnect button and `self.ch = None`; cleanup before and after forward; the file tab became a `QTableWidget` with one row per id |
| P3 | The document lacked requirements/acceptance/non-functional/exception sections | Added §1 Requirements and Scope (F1–F11 acceptance criteria, non-functional requirements, exception strategy, out of scope) |
| P3 | The initial version's claim that "supporting vim/htop requires configuring a pty" was inaccurate | Corrected to: `ChannelShell` already has a pty by default, and the real gap is raw keystroke passthrough + terminal emulation; added `setPtySize`/`TERM` and the implementation path |
| P3 | Typos and omissions (`requirements:` backticks, missing dependency list/version requirements) | Fixed formatting, added `requirements.txt` and the Python/adb version notes |
