# USB 手机桥接客户端 —— 完整方案与实现

## 一、总体架构

USB 通信在应用层最可靠的落地方案是 **ADB 端口转发**：PC 通过 `adb forward` 把本地 TCP 端口映射到手机端口，之后双方就像普通 TCP Socket 一样通信，USB 电缆承载实际数据。

```
┌────────────┐  adb forward   ┌──────────────────┐   网络(WiFi/蜂窝)  ┌──────────┐
│  PC 客户端  │◄────USB───────►│ 手机 Bridge 服务  │◄─────────────────►│ 远程服务器 │
│  (PyQt5)   │ 127.0.0.1:12580│ ServerSocket:9999│    SSH (JSch)     │  sshd:22 │
└────────────┘  = 手机:9999    └──────────────────┘                   └──────────┘
```

- **功能1/2**：PC ↔ 手机双向文件、文本走自定义协议；
- **功能3**：PC 下发 `REMOTE_OPEN` → 手机用 JSch SSH 到目标服务器 → 键盘输入/输出双向回流，手机相当于跳板代理。

## 二、自定义应用层协议（双端一致）

**帧格式：**

```
+--------+------+------------+-----------------+--------------+---------+
| magic 2B | type 1B | headerLen 2B | header(JSON,UTF-8) | payloadLen 4B | payload |
+--------+------+------------+-----------------+--------------+---------+
```

**消息类型：**

| type | 值 | 方向 | 说明 |
|---|---|---|---|
| HELLO / ACK | 0x00 / 0x20 | PC→手机 / 回 | 握手 |
| TEXT | 0x01 | 双向 | header: `{id, text}` |
| FILE_META / CHUNK / END | 0x02/03/04 | 双向 | 分块传文件，SHA256 校验 |
| REMOTE_OPEN | 0x10 | PC→手机 | `{channel, kind: ssh/exec, host, port, user, password, command}` |
| REMOTE_DATA | 0x11 | PC→手机 | stdin 数据（payload） |
| REMOTE_OUTPUT | 0x12 | 手机→PC | stdout/stderr（payload，`stream` 区分） |
| REMOTE_CLOSE | 0x13 | 双向 | 关闭通道 |
| PING / PONG | 0x30/0x31 | 双向 | 心跳 |

---

## 三、PC 端实现

目录：`requirements: pip install PyQt5`，另外需安装 [adb](https://developer.android.com/tools/releases/platform-tools) 并加入 PATH。

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

def encode_frame(msg_type: int, header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, ensure_ascii=False).encode("utf-8")
    return (MAGIC + struct.pack(">BH", msg_type, len(h))
            + h + struct.pack(">I", len(payload)) + payload)

def decode_frame(read):
    """read(n) 返回恰好 n 字节，否则抛异常"""
    if read(2) != MAGIC:
        raise IOError("协议 magic 错误")
    msg_type, hlen = struct.unpack(">BH", read(3))
    header = json.loads(read(hlen).decode("utf-8")) if hlen else {}
    (plen,) = struct.unpack(">I", read(4))
    return msg_type, header, (read(plen) if plen else b"")
```

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
                raise ConnectionError("对端关闭")
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
        """把 PC 的 local_port 转发到手机上的 remote_port"""
        self._run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}")
```

### `client.py`（核心业务：文本 / 文件 / 远程通道）

```python
import os, time, threading, hashlib
from protocol import MsgType
from transport import Transport

CHUNK_SIZE = 256 * 1024

class PhoneClient:
    def __init__(self):
        self.tp = None
        self._id_lock = threading.Lock()
        self._next_id = 1
        # ---- 事件回调（网络线程中触发，UI 层自行切线程）----
        self.on_text = None            # fn(text)
        self.on_hello_ack = None       # fn(info)
        self.on_status = None          # fn(str)
        self.on_file_progress = None   # fn(fid, name, sent, total, direction)
        self.on_file_done = None       # fn(fid, name, ok, direction, saved_path)
        self.on_remote_output = None   # fn(channel, stream, bytes)
        self.on_remote_close = None    # fn(channel, code, reason)
        self._recv, self.channels = {}, {}
        self.save_dir = os.path.abspath("downloads")
        os.makedirs(self.save_dir, exist_ok=True)

    # ---------- 连接 ----------
    def connect(self, host="127.0.0.1", port=12580):
        self.tp = Transport(host, port, self._on_frame, self._on_disconnect)
        self.tp.start()
        self.tp.send(MsgType.HELLO, {"client": "pc-client", "version": 1})
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def close(self):
        self.tp and self.tp.close()

    def _heartbeat(self):
        while self.tp and self.tp.alive:
            time.sleep(15)
            try: self.tp.send(MsgType.PING, {})
            except Exception: break

    def _on_disconnect(self):
        self._recv.clear(); self.channels.clear()
        self.on_status and self.on_status("[连接已断开]")

    def _new_id(self):
        with self._id_lock:
            v = self._next_id; self._next_id += 1
            return v

    # ---------- 功能2：文本消息 ----------
    def send_text(self, text: str):
        self.tp.send(MsgType.TEXT, {"id": self._new_id(), "text": text})

    # ---------- 功能1：文件传输（PC → 手机） ----------
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

    # ---------- 文件接收（手机 → PC） ----------
    def _on_file_meta(self, h):
        safe = os.path.basename(h.get("name", "unnamed"))
        path = os.path.join(self.save_dir, safe)
        base, ext, i = os.path.splitext(path), 1
        while os.path.exists(path):
            path = f"{base[0]}({i}){base[1]}"; i += 1
        self._recv[h["id"]] = {"fp": open(path, "wb"), "name": safe,
                               "size": h.get("size", 0), "got": 0,
                               "path": path, "sha": hashlib.sha256()}

    def _on_file_end(self, h):
        st = self._recv.pop(h["id"], None)
        if not st: return
        st["fp"].close()
        ok = h.get("ok") and (not h.get("sha256") or h["sha256"] == st["sha"].hexdigest())
        if not ok:
            os.remove(st["path"])
        self.on_file_done and self.on_file_done(
            h["id"], st["name"], ok, "down", st["path"] if ok else "")

    # ---------- 功能3：远程调用（手机作代理） ----------
    def remote_open(self, kind: str, **params) -> int:
        ch = self._new_id()
        self.channels[ch] = {"kind": kind, **params}
        self.tp.send(MsgType.REMOTE_OPEN, {"channel": ch, "kind": kind, **params})
        return ch

    def remote_input(self, ch: int, data: bytes):
        self.tp.send(MsgType.REMOTE_DATA, {"channel": ch}, data)

    def remote_close(self, ch: int):
        if ch in self.channels:
            self.tp.send(MsgType.REMOTE_CLOSE, {"channel": ch})
            self.channels.pop(ch, None)

    # ---------- 消息分发 ----------
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
            elif t == M.ERROR:         self.on_status and self.on_status("[错误] " + h.get("message", ""))
        except Exception as e:
            self.on_status and self.on_status(f"[处理异常] {e}")
```

### `ui.py`（PyQt5 界面，三个 Tab 对应三个功能）

```python
import sys
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QComboBox, QPushButton, QLabel, QTabWidget, QPlainTextEdit,
    QLineEdit, QProgressBar, QFileDialog, QMessageBox)

from adb_manager import Adb
from client import PhoneClient

PC_PORT, PHONE_PORT = 12580, 9999

class MainWindow(QMainWindow):
    sig_text  = pyqtSignal(str)
    sig_status = pyqtSignal(str)
    sig_fprog = pyqtSignal(object, str, int, int, str)
    sig_fdone = pyqtSignal(object, str, bool, str, str)
    sig_rout  = pyqtSignal(object, str, bytes)
    sig_rclose = pyqtSignal(object, int, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("USB Bridge 客户端"); self.resize(780, 560)
        self.adb, self.client, self.ch = Adb(), PhoneClient(), None
        c = self.client
        c.on_text = self.sig_text.emit
        c.on_status = self.sig_status.emit
        c.on_hello_ack = lambda i: self.sig_status.emit(f"[已连接手机: {i.get('device','?')}]")
        c.on_file_progress = lambda *a: self.sig_fprog.emit(*a)
        c.on_file_done = lambda *a: self.sig_fdone.emit(*a)
        c.on_remote_output = lambda *a: self.sig_rout.emit(*a)
        c.on_remote_close = lambda *a: self.sig_rclose.emit(*a)
        for s, slot in ((self.sig_text, self._on_text), (self.sig_status, lambda s: self.msg_view.appendPlainText(s)),
                        (self.sig_fprog, self._on_fprog), (self.sig_fdone, self._on_fdone),
                        (self.sig_rout, self._on_rout), (self.sig_rclose,
                        lambda ch, code, r: self.term.appendPlainText(f"\n** 通道关闭 code={code} {r} **"))):
            s.connect(slot)
        self._build_ui()

    def _build_ui(self):
        top = QHBoxLayout()
        self.cmb = QComboBox()
        b1 = QPushButton("刷新设备"); b1.clicked.connect(self.refresh)
        b2 = QPushButton("连接"); b2.clicked.connect(self.connect_phone)
        self.lbl = QLabel("未连接")
        top.addWidget(QLabel("设备:")); top.addWidget(self.cmb, 1)
        top.addWidget(b1); top.addWidget(b2); top.addWidget(self.lbl)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._msg_tab(), "消息")
        self.tabs.addTab(self._file_tab(), "文件")
        self.tabs.addTab(self._term_tab(), "远程终端")
        root = QWidget(); lay = QVBoxLayout(root)
        lay.addLayout(top); lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self.refresh()

    # ---- Tab1 消息 ----
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
        if t:
            self.client.send_text(t)
            self.msg_view.appendPlainText(f"[PC] {t}")
            self.msg_input.clear()

    def _on_text(self, t):
        self.msg_view.appendPlainText(f"[手机] {t}")
        self.tabs.setCurrentIndex(0)

    # ---- Tab2 文件 ----
    def _file_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        b = QPushButton("选择文件发送到手机…"); b.clicked.connect(self.pick_send)
        self.fbar, self.flbl = QProgressBar(), QLabel("")
        v.addWidget(b); v.addWidget(self.flbl); v.addWidget(self.fbar)
        v.addWidget(QLabel("接收的文件保存在 ./downloads/")); v.addStretch(1)
        return w

    def pick_send(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件")
        if path:
            self.flbl.setText(f"发送中: {path}")
            self.client.send_file(path)

    def _on_fprog(self, fid, name, sent, total, d):
        self.fbar.setValue(int(sent * 100 / max(total, 1)))

    def _on_fdone(self, fid, name, ok, d, path):
        self.flbl.setText(("✓ " if ok else "✗ ") + ("已发送 " if d == "up" else "已接收 ") + name)

    # ---- Tab3 远程终端（手机代理 SSH） ----
    def _term_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.ed_host = QLineEdit(); self.ed_host.setPlaceholderText("远程服务器 IP")
        self.ed_port = QLineEdit("22"); self.ed_port.setFixedWidth(50)
        self.ed_user = QLineEdit(); self.ed_user.setPlaceholderText("用户名")
        self.ed_pass = QLineEdit(); self.ed_pass.setPlaceholderText("密码")
        self.ed_pass.setEchoMode(QLineEdit.Password)
        b1 = QPushButton("经手机 SSH 连接"); b1.clicked.connect(self.open_ssh)
        b2 = QPushButton("断开"); b2.clicked.connect(lambda: self.ch and self.client.remote_close(self.ch))
        for x in (self.ed_host, self.ed_port, self.ed_user, self.ed_pass): h.addWidget(x)
        h.addWidget(b1); h.addWidget(b2)
        self.term = QPlainTextEdit(); self.term.setReadOnly(True)
        h2 = QHBoxLayout()
        self.term_input = QLineEdit(); self.term_input.returnPressed.connect(self.term_send)
        h2.addWidget(self.term_input, 1)
        v.addLayout(h); v.addWidget(self.term, 1); v.addLayout(h2)
        return w

    def open_ssh(self):
        self.ch = self.client.remote_open(
            "ssh", host=self.ed_host.text(), port=int(self.ed_port.text() or 22),
            user=self.ed_user.text(), password=self.ed_pass.text())
        self.term.appendPlainText(f"** 正在通过手机连接 {self.ed_user.text()}@{self.ed_host.text()} … **")

    def term_send(self):
        line = self.term_input.text(); self.term_input.clear()
        if self.ch:
            self.client.remote_input(self.ch, (line + "\n").encode())  # 行模式，适合执行命令

    def _on_rout(self, ch, stream, data):
        self.term.insertPlainText(data.decode("utf-8", "ignore"))
        self.term.moveCursor(self.term.textCursor().End)

    # ---- 连接管理 ----
    def refresh(self):
        self.cmb.clear()
        for d in self.adb.devices():
            if d["state"] == "device":
                self.cmb.addItem(d["desc"], d["serial"])

    def connect_phone(self):
        serial = self.cmb.currentData()
        if not serial:
            return QMessageBox.warning(self, "提示", "请先刷新并选择设备（需开启USB调试并授权）")
        try:
            self.adb.forward(serial, PC_PORT, PHONE_PORT)
            self.client.connect("127.0.0.1", PC_PORT)
            self.lbl.setText("已连接 " + serial)
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow(); w.show(); sys.exit(app.exec_())
```

---

## 四、Android 端实现

`build.gradle` 添加：`implementation("com.jcraft:jsch:0.1.55")`

`AndroidManifest.xml`：

```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC"/>
<uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>
<!-- application 内 -->
<service android:name=".BridgeService" android:exported="false"
         android:foregroundServiceType="dataSync"/>
```

### `FrameIO.kt`（协议镜像实现）

```kotlin
package com.example.usbbridge

import org.json.JSONObject
import java.io.*

object FrameIO {
    const val HELLO=0x00; const val TEXT=0x01
    const val FILE_META=0x02; const val FILE_CHUNK=0x03; const val FILE_END=0x04
    const val REMOTE_OPEN=0x10; const val REMOTE_DATA=0x11
    const val REMOTE_OUTPUT=0x12; const val REMOTE_CLOSE=0x13
    const val ACK=0x20; const val ERROR=0x21; const val PING=0x30; const val PONG=0x31

    data class Frame(val type: Int, val header: JSONObject, val payload: ByteArray)

    fun encode(type: Int, header: JSONObject, payload: ByteArray = ByteArray(0)): ByteArray {
        val h = header.toString().toByteArray(Charsets.UTF_8)
        return ByteArray(9 + h.size + payload.size).apply {
            var i = 0
            fun u1(v: Int) { this[i++] = v.toByte() }
            fun u2(v: Int) { u1(v shr 8); u1(v) }
            fun u4(v: Int) { u2(v shr 16); u2(v) }
            u1(0xAB); u1(0xCD); u1(type); u2(h.size)
            h.copyInto(this, 9); i += h.size
            u4(payload.size); payload.copyInto(this, i)
        }
    }

    fun readFrame(input: InputStream): Frame? {
        val magic = ByteArray(2)
        if (!fill(input, magic)) return null                       // 对端关闭
        if (magic[0] != 0xAB.toByte() || magic[1] != 0xCD.toByte()) throw IOException("bad magic")
        val type = input.read(); if (type < 0) return null
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
    private fun readShort(s: InputStream): Int { val b=ByteArray(2); require(fill(s,b)); return ((b[0].toInt() and 0xFF) shl 8) or (b[1].toInt() and 0xFF) }
    private fun readInt(s: InputStream): Int { val b=ByteArray(4); require(fill(s,b)); return ((b[0].toInt() and 0xFF) shl 24) or ((b[1].toInt() and 0xFF) shl 16) or ((b[2].toInt() and 0xFF) shl 8) or (b[3].toInt() and 0xFF) }
    private fun readN(s: InputStream, n: Int): ByteArray { val b=ByteArray(n); require(fill(s,b)); return b }
}
```

### `RemoteSession.kt`（JSch SSH 代理，功能3核心）

```kotlin
package com.example.usbbridge

import com.jcraft.jsch.*
import java.io.PipedInputStream
import java.io.PipedOutputStream

class RemoteSession(
    private val onOutput: (stream: String, data: ByteArray) -> Unit,
    private val onClose: (code: Int, reason: String) -> Unit
) {
    private var session: Session? = null
    private var shell: ChannelShell? = null
    private var stdin: PipedOutputStream? = null

    /** 交互式 shell：PC 的 REMOTE_DATA 写入 stdin，远端输出经 onOutput 回传 */
    fun openSsh(host: String, port: Int, user: String, password: String) = connect(host, port, user, password) {
        val ch = session!!.openChannel("shell") as ChannelShell
        val pis = PipedInputStream(64 * 1024)
        stdin = PipedOutputStream(pis)
        ch.setInputStream(pis)
        ch.setOutputStream(fwdStream("out"))
        ch.connect(10_000)
        shell = ch
        Thread {
            while (ch.isConnected) try { Thread.sleep(200) } catch (_: InterruptedException) { return@Thread }
            onClose(0, "shell 已退出")
        }.start()
    }

    /** 一次性命令执行：kind=exec */
    fun execSsh(host: String, port: Int, user: String, password: String, command: String) =
        connect(host, port, user, password) {
            val ch = session!!.openChannel("exec") as ChannelExec
            ch.setCommand(command)
            ch.setOutputStream(fwdStream("out"))
            ch.setErrStream(fwdStream("err"))
            ch.connect(10_000)
            Thread {
                while (ch.isConnected) try { Thread.sleep(200) } catch (_: InterruptedException) { return@Thread }
                onClose(ch.exitStatus, "exec 完成")
            }.start()
        }

    private fun connect(host: String, port: Int, user: String, password: String, block: () -> Unit) {
        val s = JSch().getSession(user, host, port)
        s.setPassword(password)
        s.setConfig("StrictHostKeyChecking", "no")
        s.connect(10_000)
        session = s
        block()
    }

    private fun fwdStream(stream: String) = object : java.io.OutputStream() {
        override fun write(b: Int) = write(byteArrayOf(b.toByte()))
        override fun write(b: ByteArray, off: Int, len: Int) = onOutput(stream, b.copyOfRange(off, off + len))
    }

    fun writeStdin(data: ByteArray) { stdin?.apply { write(data); flush() } }

    fun close() {
        runCatching { stdin?.close() }
        runCatching { shell?.disconnect() }
        runCatching { session?.disconnect() }
    }
}
```

### `BridgeService.kt`（前台服务 + 会话处理）

```kotlin
package com.example.usbbridge

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.*
import org.json.JSONObject
import java.io.File
import java.io.OutputStream
import java.net.ServerSocket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

class BridgeService : Service() {
    private var server: ServerSocket? = null
    private val pool = Executors.newCachedThreadPool()

    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        val chId = "usb_bridge"
        if (Build.VERSION.SDK_INT >= 26) {
            (getSystemService(NotificationManager::class.java))
                .createNotificationChannel(NotificationChannel(chId, "USB Bridge", NotificationManager.IMPORTANCE_LOW))
            startForeground(1, Notification.Builder(this, chId)
                .setContentTitle("USB Bridge 运行中 (端口 9999)")
                .setSmallIcon(android.R.drawable.ic_menu_computer).build())
        } else startForeground(1, Notification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Thread {
            server = ServerSocket(9999)
            while (true) {
                val sock = server!!.accept()
                pool.execute { SessionHandler(this, sock).run() }
            }
        }.start()
        return START_STICKY
    }

    override fun onDestroy() { runCatching { server?.close() }; super.onDestroy() }
}

class SessionHandler(private val ctx: Context, private val sock: android.net.Socket) : java.io.Closeable {
    private val out: OutputStream = sock.getOutputStream()
    private val outLock = Any()
    private val remotes = ConcurrentHashMap<Int, RemoteSession>()
    private val sinks = ConcurrentHashMap<Int, FileSink>()
    @Volatile private var closed = false

    fun run() {
        try {
            val input = sock.getInputStream()
            while (!closed) handle(FrameIO.readFrame(input) ?: break)
        } catch (e: Exception) { if (!closed) e.printStackTrace() }
        finally { close() }
    }

    fun send(type: Int, header: JSONObject, payload: ByteArray = ByteArray(0)) {
        val data = FrameIO.encode(type, header, payload)
        synchronized(outLock) { out.write(data); out.flush() }
    }

    private fun handle(f: FrameIO.Frame) = with(f.header) {
        when (f.type) {
            FrameIO.HELLO -> send(FrameIO.ACK, JSONObject().put("ok", true).put("device", Build.MODEL))
            FrameIO.PING  -> send(FrameIO.PONG, JSONObject())
            FrameIO.TEXT  -> {                                            // 收到 PC 文本 → 通知栏提醒
                notifyText(ctx, optString("text"))
                send(FrameIO.ACK, JSONObject().put("id", optInt("id")))
            }
            FrameIO.FILE_META  -> sinks[optInt("id")] = FileSink.create(ctx, this)
            FrameIO.FILE_CHUNK -> sinks[optInt("id")]?.append(f.payload)
            FrameIO.FILE_END   -> {
                val path = sinks.remove(optInt("id"))?.finish(optBoolean("ok"))
                send(FrameIO.ACK, JSONObject().put("file_id", optInt("id")).put("path", path ?: ""))
            }
            FrameIO.REMOTE_OPEN  -> openRemote(f)
            FrameIO.REMOTE_DATA  -> remotes[optInt("channel")]?.writeStdin(f.payload)
            FrameIO.REMOTE_CLOSE -> remotes.remove(optInt("channel"))?.close()
        }
    }

    private fun openRemote(f: FrameIO.Frame) = with(f.header) {
        val ch = optInt("channel")
        try {
            val rs = RemoteSession(
                { stream, data -> send(FrameIO.REMOTE_OUTPUT, JSONObject().put("channel", ch).put("stream", stream), data) },
                { code, reason -> send(FrameIO.REMOTE_CLOSE, JSONObject().put("channel", ch).put("code", code).put("reason", reason)); remotes.remove(ch) })
            if (optString("kind") == "exec")
                rs.execSsh(optString("host"), optInt("port", 22), optString("user"), optString("password"), optString("command"))
            else
                rs.openSsh(optString("host"), optInt("port", 22), optString("user"), optString("password"))
            remotes[ch] = rs
        } catch (e: Exception) {
            send(FrameIO.ERROR, JSONObject().put("channel", ch).put("message", "远程连接失败: ${e.message}"))
        }
    }

    private fun notifyText(ctx: Context, text: String) {
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val n = Notification.Builder(ctx, "usb_bridge")
            .setContentTitle("来自 PC 的消息").setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_email).build()
        nm.notify((System.currentTimeMillis() and 0xFFFF).toInt(), n)
    }

    override fun close() {
        if (closed) return; closed = true
        remotes.values.forEach { it.close() }
        sinks.values.forEach { it.abort() }
        runCatching { sock.close() }
    }
}

/** 文件落盘（App 外部私有目录，免存储权限；如需存公共 Download 请改用 MediaStore） */
class FileSink private constructor(private val file: File) {
    private val fos = file.outputStream()
    fun append(chunk: ByteArray) = synchronized(fos) { fos.write(chunk) }
    fun finish(ok: Boolean): String? = synchronized(fos) {
        fos.close(); if (!ok) { file.delete(); null } else file.absolutePath }
    fun abort() = runCatching { fos.close(); file.delete() }
    companion object {
        fun create(ctx: Context, h: JSONObject): FileSink {
            val dir = ctx.getExternalFilesDir(null) ?: ctx.filesDir
            val base = h.optString("name", "file_${h.optInt("id")}")
            var t = File(dir, base); var i = 1
            while (t.exists()) { t = File(dir, "($i)$base"); i++ }
            return FileSink(t)
        }
    }
}
```

`MainActivity`（一键启动服务）：

```kotlin
class MainActivity : androidx.appcompat.app.AppCompatActivity() {
    override fun onCreate(s: android.os.Bundle?) {
        super.onCreate(s)
        val btn = android.widget.Button(this).apply { text = "启动 USB Bridge 服务" }
        btn.setOnClickListener {
            androidx.core.content.ContextCompat.startForegroundService(
                this, android.content.Intent(this, BridgeService::class.java))
            android.widget.Toast.makeText(this, "服务已启动，监听 9999", android.widget.Toast.LENGTH_SHORT).show()
        }
        setContentView(btn)
    }
}
```

> 手机 → PC 方向的发送（文件/文本），手机侧复用同一协议，直接 `Socket("127.0.0.1", 9999)` 不行 —— 需先由 PC 执行 `adb reverse tcp:9998 tcp:9998` 后手机连 `localhost:9998`，或手机端也实现一份 `send_file`（与 PC 端 `send_file` 逻辑完全对称：META → CHUNK… → END）。

---

## 五、部署与使用步骤

1. **手机**：安装 App → 开启开发者选项/USB 调试 → 打开 App 点击"启动服务"→ USB 连接 PC，弹窗授权调试；
2. **PC**：`adb devices` 确认设备在线 → `pip install PyQt5` → `python ui.py`；
3. 点击"刷新设备"→"连接"，然后：
   - **消息**：Tab1 互发文本；
   - **文件**：Tab2 选文件即传；手机发来的文件在 `./downloads/`；
   - **远程代理**：Tab3 填目标服务器 IP/账号 → "经手机 SSH 连接" → 底部输入框敲命令（如 `htop`、`tail -f /var/log/syslog`），输出实时回流。

## 六、说明与可扩展点

| 事项 | 说明 |
|---|---|
| **信任边界** | adb 仅允许本机访问转发端口，USB 链路天然安全；如需更强认证，可在 HELLO 中带 token 由手机校验 |
| **终端模式** | 当前为"行模式"，足够执行命令；若要支持 vim/htop 等全屏程序，可改为按键直传（去掉 `\n`）并在 JSch 侧配置伪终端参数 |
| **大文件/断点续传** | 已分块 + SHA256 校验；续传只需 FILE_META 增加 offset 字段，接收端 seek 写入 |
| **免装 App 替代** | 手机装 Termux 跑 `sshd`，PC `adb forward` 后先 SSH 进手机，再从手机 SSH 到目标 —— 可快速验证但无法自定义 UI 和文件管理 |
| **免 adb 方案** | 需手机 root 或系统级支持（如车机/定制 ROM 直接暴露 TCP），否则 ADB 是标准做法 |

需要我补充哪一部分（比如手机端发送文件的完整 UI、AES 加密通道、或断点续传）可以继续展开。