# USB 手机桥接客户端 —— 需求 · 设计 · 实现

## 一、需求与范围

### 1.1 原始需求

在 PC 端实现一个客户端，通过 USB 与手机端服务通信，实现：

1. 相互传送文件；
2. 相互传输文本消息；
3. PC 端远程操作：手机端服务作为代理，执行远程服务调用（例如手机上的服务 SSH 到一台远程服务器，PC 通过手机操作这台远程服务器）。

### 1.2 功能拆解与验收标准

| 编号 | 功能点 | 验收标准 |
|---|---|---|
| F1 | PC → 手机 传文件 | 选文件后进度可见；手机收到完整文件；SHA256 一致；重名不覆盖已有文件 |
| F2 | 手机 → PC 传文件 | 手机端选文件发送；PC 落到 `./downloads/`；SHA256 校验失败自动删除并报错 |
| F3 | PC → 手机 发文本 | 手机收到通知栏提醒 |
| F4 | 手机 → PC 发文本 | PC 消息面板实时显示手机发来的文本 |
| F5 | 交互式远程终端 | PC 填目标服务器 IP/端口/账号 → 经手机建立 SSH shell → 命令输出实时回流 → 断开后手机侧会话释放 |
| F6 | 一次性远程命令（exec） | 可执行单条命令并回传 stdout/stderr 与退出码 |
| F7 | 首次连接主机指纹确认 | 目标主机未知时拒绝连接并回传指纹，PC 端确认后写入手机 known_hosts，后续不再询问 |
| F8 | 主机密钥变更保护 | 目标主机指纹与已知不一致时**拒绝连接**并报警，不允许静默继续 |
| F9 | 设备发现与连接管理 | 列出 adb 在线设备、连接、断开（断开同时清理 adb forward 与本地半成品文件） |
| F10 | 链路鉴权 | 无正确 token 的连接被拒绝并断开 |
| F11 | 心跳保活 | 双方 15s 心跳；网络线程异常退出时 UI 明确提示"连接已断开" |

### 1.3 非功能需求

| 维度 | 要求 |
|---|---|
| 平台 | PC：Windows/Linux/macOS + Python 3.8+ + PyQt5 + adb(platform-tools)；手机：minSdk 24（Android 7.0），推荐 Android 8.0(API 26)+（前台服务通知渠道） |
| 文件大小 | 单文件 ≤ 2GB（分块 256KB 流式传输，不整文件载入内存） |
| 并发 | 同一时刻支持 1 个 PC 连接；连接内文本/文件/远程通道并发可用 |
| 断线策略 | 不自动重连，断线只做清理 + 提示，由用户手动重连 |
| 数据安全 | USB 链路 + 回环绑定 + token；SSH 凭据仅在内存中使用，不落盘；私钥不写入 App 目录 |
| 可观测性 | 关键事件（连接/断开、文件完成、远程通道开关、错误码）在 PC 界面可见；Android 侧异常打印到 Logcat |

### 1.4 异常场景与处理策略

| 场景 | 处理 |
|---|---|
| 手机未开 USB 调试 / 未授权 | `adb devices` 状态非 `device`，PC 提示先授权，不进入连接流程 |
| adb forward 失败 / 残留 | 连接前先 `adb forward --remove-all` 再建立，避免端口占用 |
| token 不匹配 | 手机回 `ERROR{code:BAD_TOKEN}` 并关闭连接；PC 提示 token 错误 |
| 目标主机不可达 / 端口拒绝 | `ERROR{code:HOST_UNREACHABLE}` |
| 主机指纹未知 | `ERROR{code:UNKNOWN_HOST, host, fingerprint}` → PC 弹窗确认 → 带 `trust_fingerprint` 重发 |
| 主机指纹变更 | `ERROR{code:HOST_KEY_CHANGED}` → PC 红色告警，不提供"继续"按钮 |
| SSH 认证失败 | `ERROR{code:AUTH_FAILED}` |
| 传输中断（断线） | 关闭句柄并删除两端未完成的临时文件 |
| SHA256 不一致 | 删除落盘文件，`FILE_END`/`ACK` 标记 `ok:false` |
| 磁盘写入失败 | `ERROR{code:FILE_WRITE_FAILED}`，保留 PC 侧提示 |

### 1.5 明确不做（Out of scope）

- 免 adb 方案（需 root 或定制 ROM）；
- 断点续传、多设备并行、文件目录树浏览；
- 手机端完整终端仿真（vim/htop 等全屏程序的 ANSI 渲染），见 §8。

## 二、总体架构

USB 通信在应用层最可靠的落地方案是 **ADB 端口转发**：PC 通过 `adb forward` 把本地 TCP 端口映射到手机端口，之后双方就像普通 TCP Socket 一样通信，USB 电缆承载实际数据。

```
┌────────────┐  adb forward   ┌──────────────────┐   网络(WiFi/蜂窝)  ┌──────────┐
│  PC 客户端  │◄────USB───────►│ 手机 Bridge 服务  │◄─────────────────►│ 远程服务器 │
│  (PyQt5)   │ 127.0.0.1:12580│ 127.0.0.1:9999   │    SSH (JSch)     │  sshd:22 │
└────────────┘  = 手机:9999    └──────────────────┘                   └──────────┘
```

- **功能1/2（双向）**：PC ↔ 手机的文件、文本共用**同一条已建立的 TCP 连接**双向收发（协议本身全双工）；
  手机端不需要再反向连回 PC，也**不需要 `adb reverse`** —— 手机侧持有 `accept()` 得到的 socket，直接在其上发送即可。
- **功能3**：PC 下发 `REMOTE_OPEN` → 手机用 JSch SSH 到目标服务器（手机作跳板代理）→ 键盘输入/输出双向回流。
- 手机侧监听端口绑定在 **回环地址**，并把 `adb forward` 打进来的连接视作唯一可信来源 + token 二次校验（见 §7）。

## 三、自定义应用层协议（双端一致）

**帧格式（大端）：**

```
+----------+---------+-------------+-------------------+-------------+---------+
| magic 2B | type 1B | headerLen 2B| header(JSON,UTF-8)| payloadLen 4B| payload |
| AB CD    |         |             |                   |             |         |
+----------+---------+-------------+-------------------+-------------+---------+
```

**消息类型：**

| type | 值 | 方向 | 说明 |
|---|---|---|---|
| HELLO / ACK | 0x00 / 0x20 | PC→手机 / 回 | 握手，header: `{client, version, token}`；手机校验 token |
| TEXT | 0x01 | 双向 | header: `{id, text}` |
| FILE_META / CHUNK / END | 0x02/03/04 | 双向 | 分块传文件，SHA256 双端校验 |
| REMOTE_OPEN | 0x10 | PC→手机 | `{channel, kind: ssh/exec, host, port, user, password 或 private_key+passphrase, command, trust_fingerprint?}` |
| REMOTE_DATA | 0x11 | PC→手机 | stdin 数据（payload） |
| REMOTE_OUTPUT | 0x12 | 手机→PC | stdout/stderr（payload，`stream` 区分） |
| REMOTE_CLOSE | 0x13 | 双向 | 关闭通道，header: `{channel, code, reason}` |
| ERROR | 0x21 | 双向 | `{code, message, channel?, host?, fingerprint?}` |
| PING / PONG | 0x30/0x31 | 双向 | 心跳 |

**ERROR code 约定：**

| code | 含义 |
|---|---|
| `BAD_TOKEN` | 握手 token 不匹配（随后手机主动断开） |
| `BAD_FRAME` | 帧解析失败（magic/长度异常），当前实现直接断开连接，该码保留 |
| `UNKNOWN_HOST` | 目标主机指纹未在已知主机库中，需 PC 确认 |
| `HOST_KEY_CHANGED` | 目标主机指纹与已知不一致（疑似 MITM），拒绝连接 |
| `AUTH_FAILED` | SSH 认证失败 |
| `HOST_UNREACHABLE` | 目标主机不可达 / 端口拒绝 |
| `CHANNEL_OPEN_FAILED` | 通道打开失败（其他） |
| `FILE_WRITE_FAILED` | 手机侧落盘失败 |

**id 命名空间约定（避免双向 id 冲突）：**

- PC 分配：`1 ~ 0x3FFFFFFF`（`_next_id` 从 1 递增）；
- 手机分配：`0x40000000 ~`（`FrameIO.nextId()` 从 `0x40000000` 递增）。

两个方向同时传文件时，`FILE_*` 的 `id` 与 `REMOTE_*` 的 `channel` 都不会撞号，UI 才能按 id 正确分行显示进度。

**JSON 空值陷阱：** 可选字段一律"没有就不带"，不要写成 `null` —— Android 侧 `JSONObject.optString("x")` 对 JSON `null` 会返回字符串 `"null"`。因此 PC 端组 header 时对 `None` 值做过滤。

---

## 四、PC 端实现

依赖清单 `requirements.txt`：

```
PyQt5>=5.15
```

另外需安装 [adb](https://developer.android.com/tools/releases/platform-tools) 并加入 PATH（`adb version` 可用即可，无最低版本硬性要求）。

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
    """read(n) 返回恰好 n 字节，否则抛异常"""
    if read(2) != MAGIC:
        raise IOError("协议 magic 错误")
    msg_type, hlen = struct.unpack(">BH", read(3))   # type 1B + headerLen 2B
    header = json.loads(read(hlen).decode("utf-8")) if hlen else {}
    (plen,) = struct.unpack(">I", read(4))
    return msg_type, header, (read(plen) if plen else b"")
```

> 与 Kotlin 实现的字段顺序必须严格一致：`magic(2) | type(1) | headerLen(2) | header | payloadLen(4) | payload`，总长度 `9 + headerLen + payloadLen`。

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
        """把 PC 的 local_port 转发到手机上的 remote_port（先清理可能残留的 forward）"""
        self._run("-s", serial, "forward", "--remove-all", check=False)
        self._run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}")

    def remove_forward(self, serial, local_port):
        self._run("-s", serial, "forward", "--remove", f"tcp:{local_port}", check=False)
```

### `client.py`（核心业务：文本 / 文件 / 远程通道）

```python
import os, time, threading, hashlib
from protocol import MsgType, ErrCode
from transport import Transport

CHUNK_SIZE = 256 * 1024

class PhoneClient:
    def __init__(self):
        self.tp = None
        self._id_lock = threading.Lock()
        self._next_id = 1                      # PC 侧 id 空间：1 起
        # ---- 事件回调（网络线程中触发，UI 层自行切线程）----
        self.on_text = None            # fn(text)
        self.on_hello_ack = None       # fn(info)
        self.on_status = None          # fn(str)
        self.on_file_progress = None   # fn(fid, name, sent, total, direction)
        self.on_file_done = None       # fn(fid, name, ok, direction, saved_path)
        self.on_remote_output = None   # fn(channel, stream, bytes)
        self.on_remote_close = None    # fn(channel, code, reason)
        self.on_remote_error = None    # fn(channel, code, header) 通道级错误（含主机指纹确认）
        self._recv, self.channels = {}, {}
        self.save_dir = os.path.abspath("downloads")
        os.makedirs(self.save_dir, exist_ok=True)

    # ---------- 连接 ----------
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
        """断线时关闭句柄并删除写了一半的文件，避免残留与句柄泄漏"""
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
        stem, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(path):          # 与手机端保持同样的 name(1).ext 规则
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

    # ---------- 功能3：远程调用（手机作代理） ----------
    def remote_open(self, kind: str, trust_fingerprint: str = None, **params) -> int:
        ch = self._new_id()
        header = {"channel": ch, "kind": kind, **params}
        if trust_fingerprint:                 # 不传 None，规避 JSON null 陷阱
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
            elif t == M.ERROR:
                if "channel" in h and self.on_remote_error:
                    self.on_remote_error(h["channel"], h.get("code", ""), h)
                else:
                    self.on_status and self.on_status(
                        f"[错误] {h.get('code','')} {h.get('message','')}")
        except Exception as e:
            self.on_status and self.on_status(f"[处理异常] {e}")
```

### `ui.py`（PyQt5 界面，三个 Tab 对应三个功能）

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
        self.setWindowTitle("USB Bridge 客户端"); self.resize(820, 600)
        self.adb, self.client, self.ch = Adb(), PhoneClient(), None
        self._pending = None          # 最近一次远程打开参数，用于指纹确认后重发
        self._rows = {}               # fid -> 表格行号
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
        self.ed_token = QLineEdit(); self.ed_token.setPlaceholderText("手机端 token")
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
        if t and self.client.tp and self.client.tp.alive:
            self.client.send_text(t)
            self.msg_view.appendPlainText(f"[PC] {t}")
            self.msg_input.clear()

    def _on_text(self, t):
        self.msg_view.appendPlainText(f"[手机] {t}")
        self.tabs.setCurrentIndex(0)

    def _on_status(self, s):
        self.msg_view.appendPlainText(s)

    # ---- Tab2 文件（按 fid 分行，支持并发传输） ----
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

    # ---- Tab3 远程终端（手机代理 SSH） ----
    def _term_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.ed_host = QLineEdit(); self.ed_host.setPlaceholderText("远程服务器 IP")
        self.ed_port = QLineEdit("22"); self.ed_port.setFixedWidth(50)
        self.ed_user = QLineEdit(); self.ed_user.setPlaceholderText("用户名")
        self.ed_pass = QLineEdit(); self.ed_pass.setPlaceholderText("密码")
        self.ed_pass.setEchoMode(QLineEdit.Password)
        for x in (self.ed_host, self.ed_port, self.ed_user, self.ed_pass): h.addWidget(x)
        v.addLayout(h)

        h2 = QHBoxLayout()
        self.chk_key = QCheckBox("使用私钥")
        self.ed_key = QLineEdit(); self.ed_key.setPlaceholderText("私钥文件（OpenSSH/PKCS#8 PEM）")
        bk = QPushButton("选择…"); bk.clicked.connect(self.pick_key)
        self.ed_phrase = QLineEdit(); self.ed_phrase.setPlaceholderText("私钥口令(可选)")
        self.ed_phrase.setEchoMode(QLineEdit.Password); self.ed_phrase.setFixedWidth(130)
        for x in (self.chk_key, self.ed_key, bk, self.ed_phrase): h2.addWidget(x)
        h2.setStretch(1, 1)
        v.addLayout(h2)

        h3 = QHBoxLayout()
        b1 = QPushButton("经手机 SSH 连接(shell)"); b1.clicked.connect(lambda: self.open_remote("ssh"))
        self.ed_cmd = QLineEdit(); self.ed_cmd.setPlaceholderText("一次性命令，如: uname -a")
        b2 = QPushButton("exec 执行一次"); b2.clicked.connect(lambda: self.open_remote("exec"))
        b3 = QPushButton("断开"); b3.clicked.connect(self.close_channel)
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
            self.client.remote_input(self.ch, (line + "\n").encode())  # 行模式，适合执行命令

    def _on_rout(self, ch, stream, data):
        self.term.insertPlainText(data.decode("utf-8", "ignore"))
        self.term.moveCursor(self.term.textCursor().End)

    def _on_rclose(self, ch, code, reason):
        self.term.appendPlainText(f"\n** 通道关闭 code={code} {reason} **")
        if ch == self.ch:
            self.ch = None

    def _on_rerr(self, ch, code, h):
        host, fp = h.get("host", ""), h.get("fingerprint", "")
        if code == "UNKNOWN_HOST":                     # 首次连接：由用户确认指纹
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
            self.adb.remove_forward(serial, PC_PORT)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow(); w.show(); sys.exit(app.exec_())
```

> 注意 `_on_rerr` 里"确认指纹后重发"的写法：把已确认的 `trust_fingerprint` 合并进 header 后重新 `remote_open`（每次都会分配新的 `channel`，旧通道由手机侧在异常分支里清理）。

---

## 五、Android 端实现

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
import java.util.concurrent.atomic.AtomicInteger

object FrameIO {
    const val HELLO=0x00; const val TEXT=0x01
    const val FILE_META=0x02; const val FILE_CHUNK=0x03; const val FILE_END=0x04
    const val REMOTE_OPEN=0x10; const val REMOTE_DATA=0x11
    const val REMOTE_OUTPUT=0x12; const val REMOTE_CLOSE=0x13
    const val ACK=0x20; const val ERROR=0x21; const val PING=0x30; const val PONG=0x31

    /** 手机侧 id 空间从 0x40000000 起，避免与 PC 侧(1 起)撞号 */
    private val idSeq = AtomicInteger(0x40000000)
    fun nextId(): Int = idSeq.getAndIncrement()

    data class Frame(val type: Int, val header: JSONObject, val payload: ByteArray)

    fun encode(type: Int, header: JSONObject, payload: ByteArray = ByteArray(0)): ByteArray {
        val h = header.toString().toByteArray(Charsets.UTF_8)
        // 布局: magic(2) | type(1) | headerLen(2) | header | payloadLen(4) | payload
        return ByteArray(9 + h.size + payload.size).apply {
            var i = 0
            fun u1(v: Int) { this[i++] = v.toByte() }
            fun u2(v: Int) { u1(v shr 8); u1(v) }
            fun u4(v: Int) { u2(v shr 16); u2(v) }
            u1(0xAB); u1(0xCD); u1(type); u2(h.size)
            h.copyInto(this, i); i += h.size          // 必须写在 i 处，不能写死偏移
            u4(payload.size); payload.copyInto(this, i)
        }
    }

    fun readFrame(input: InputStream): Frame? {
        val magic = ByteArray(2)
        if (!fill(input, magic)) return null                       // 对端关闭
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

### `TofuHostKeys.kt`（已知主机库：首次使用需 PC 端确认指纹）

```kotlin
package com.example.usbbridge

import android.util.Base64
import com.jcraft.jsch.HostKey
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.UserInfo
import java.io.File
import java.security.MessageDigest

/**
 * TOFU（Trust On First Use）已知主机库。
 * 存储格式：每行 "host<TAB>SHA256:xxx"，与 OpenSSH 的 SHA256 指纹一致，
 * 便于用户用 `ssh-keygen -lf` 交叉核对。
 *
 * @param approvedFingerprint PC 端已确认的指纹；仅当它等于实际指纹时才写入并放行。
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
            recorded == null -> NOT_INCLUDED                    // 未知主机：交 PC 端确认
            recorded == fp -> OK
            else -> CHANGED                                     // 指纹变更：拒绝
        }
        return lastResult
    }

    fun trust(host: String, fingerprint: String) {
        known[host] = fingerprint
        store.writeText(known.entries.joinToString("\n") { "${it.key}\t${it.value}" })
    }

    override fun add(hostkey: HostKey, ui: UserInfo?) = trust(hostkey.host, fingerprint(hostkey.key))
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

### `RemoteSession.kt`（JSch SSH 代理，功能3核心）

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
    private val stdinPipe = PipedInputStream(stdin, 1 shl 20)   // 1MB，降低被背压阻塞的概率

    /** 交互式 shell：PC 的 REMOTE_DATA 写入 stdin，远端输出经 onOutput 回传 */
    fun openSsh(host: String, port: Int, user: String, auth: Auth) = connect(host, port, user, auth) {
        val ch = session!!.openChannel("shell") as ChannelShell
        ch.setPty(true)
        ch.setPtySize(120, 30, 0, 0)          // 列/行：与 PC 终端宽度匹配，全屏程序才不会被截断
        runCatching { ch.setEnv("TERM", "xterm-256color") }   // 远端未开 AcceptEnv 时会被忽略
        ch.setInputStream(stdinPipe)
        ch.setOutputStream(fwdStream("out"))
        ch.connect(10_000)
        channel = ch
        watchClose(ch) { onClose(0, "shell 已退出") }
    }

    /** 一次性命令执行：kind=exec（支持 stdin，如 sudo -S、docker exec -i） */
    fun execSsh(host: String, port: Int, user: String, auth: Auth, command: String) =
        connect(host, port, user, auth) {
            val ch = session!!.openChannel("exec") as ChannelExec
            ch.setCommand(command)
            ch.setInputStream(stdinPipe)
            ch.setOutputStream(fwdStream("out"))
            ch.setErrStream(fwdStream("err"))
            ch.connect(10_000)
            channel = ch
            watchClose(ch) { onClose(ch.exitStatus, "exec 完成") }
        }

    data class Auth(val password: String?, val privateKey: String?, val passphrase: String?)

    private fun connect(host: String, port: Int, user: String, auth: Auth, block: () -> Unit) {
        val jsch = JSch()
        if (!auth.privateKey.isNullOrBlank()) {           // 私钥认证（内容仅存在内存中）
            jsch.addIdentity("bridge", auth.privateKey.toByteArray(),
                null, auth.passphrase?.takeIf { it.isNotEmpty() }?.toByteArray())
        }
        val s = jsch.getSession(user, host, port)
        auth.password?.takeIf { it.isNotEmpty() }?.let { s.setPassword(it) }
        s.setConfig("StrictHostKeyChecking", "yes")       // 由 TofuHostKeys 决定是否放行
        s.setHostKeyRepository(hostKeys)
        s.connect(10_000)
        session = s
        block()
    }

    /** 在独立线程等待通道结束，避免占用调用线程 */
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

### `BridgeService.kt`（前台服务 + 会话处理）

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
        startForeground(1, buildNotification(this, "USB Bridge 运行中 (127.0.0.1:$PORT)", "Token: $token"))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running) return START_STICKY          // 防重入：重复点按钮/系统重启不再起第二个监听
        running = true
        Thread {
            try {
                // 只绑回环：设备侧 adb forward 通过 127.0.0.1 回连；同时避免其他 App 直接接入
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

        /** 供 SessionHandler 复用线程池（手机端发送文件等耗时操作不能占用服务线程） */
        fun poolExecute(task: Runnable) { pool.execute(task) }

        fun addSession(s: SessionHandler) { sessions.add(s) }
        fun removeSession(s: SessionHandler) { sessions.remove(s) }

        /** 手机端发起文本/文件（复用当前已建立的连接，PC 侧已具备接收逻辑） */
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
    /** 远程通道操作串行执行：保证 OPEN→DATA 顺序，同时不阻塞主读循环（TEXT/FILE/PING 不受 SSH 握手影响） */
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
        // 首个必须是带正确 token 的 HELLO，否则拒绝
        if (!authed) {
            if (f.type == FrameIO.HELLO && optString("token") == BridgeService.token) {
                authed = true
                send(FrameIO.ACK, JSONObject().put("ok", true).put("device", Build.MODEL))
            } else {
                send(FrameIO.ERROR, JSONObject().put("code", "BAD_TOKEN").put("message", "token 校验失败"))
                close()
            }
            return
        }
        when (f.type) {
            FrameIO.PING  -> send(FrameIO.PONG, JSONObject())
            FrameIO.TEXT  -> {                                     // PC → 手机 文本
                notifyText(ctx, optString("text"))
                send(FrameIO.ACK, JSONObject().put("id", optInt("id")))
            }
            FrameIO.FILE_META  -> {
                val id = optInt("id")
                try { sinks[id] = FileSink.create(ctx, f.header) }        // ctx 取外层 SessionHandler 属性
                catch (e: Exception) {                                 // 磁盘满/无权限等
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

    // ---------- 功能1/2：手机 → PC 文本与文件 ----------
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
            if (cleanup) runCatching { file.delete() }       // 发送缓存副本后清理
        }
    }

    // ---------- 功能3：远程会话 ----------
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
            PackageManager.PERMISSION_GRANTED) return       // 未授权则静默跳过，界面仍有日志
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify((System.currentTimeMillis() and 0xFFFF).toInt(),
            BridgeService.buildNotification(ctx, "来自 PC 的消息", text))
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

/** 文件落盘（App 外部私有目录，免存储权限）；带 SHA256 校验与 name(1).ext 重名规则 */
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

        /** 与 PC 端一致：a.txt → a(1).txt */
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

> 要点说明：
> - `handle` 用 `with(f.header)` 展开 header 字段（`optString`/`optInt` 即 JSONObject 的方法），文件落盘时把 `ctx` 显式传给 `FileSink`。
> - 手机端发送文件走 `BridgeService` 的线程池，避免阻塞 socket 读循环。

`MainActivity`（启动服务 + token 显示 + 手机端发起发送）：

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
        info.text = "准备发送: ${displayName(uri)}"
        Thread { runCatching { copyToCache(uri) }
            .onSuccess { BridgeService.broadcastFile(it, cleanup = true); runOnUiThread { info.text = "已发起发送: ${it.name}" } }
            .onFailure { e -> runOnUiThread { info.text = "发送失败: ${e.message}" } } }.start()
    }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        supportRequestWindowFeature(android.view.Window.FEATURE_NO_TITLE)
        if (Build.VERSION.SDK_INT >= 33)
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)   // 否则收不到 PC 文本提醒

        info = TextView(this).apply { textSize = 16f }
        val start = Button(this).apply { text = "启动 USB Bridge 服务" }
        val sendText = Button(this).apply { text = "发送文本到 PC" }
        val sendFile = Button(this).apply { text = "发送文件到 PC" }
        input = EditText(this).apply { hint = "输入要发给 PC 的文本" }

        start.setOnClickListener {
            ContextCompat.startForegroundService(this, Intent(this, BridgeService::class.java))
            info.postDelayed({ info.text = "服务已启动，监听 127.0.0.1:9999\nToken: ${BridgeService.token}" }, 300)
        }
        sendText.setOnClickListener {
            val t = input.text.toString().trim()
            if (t.isNotEmpty()) {
                BridgeService.broadcastText(t); input.text.clear(); info.text = "已发送: $t"
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

## 六、部署与使用步骤

1. **手机**：安装 App → 开启开发者选项 / USB 调试 → 打开 App 点击"启动服务"（首次会申请通知权限）→ USB 连接 PC，弹窗授权调试；
   记下界面/通知栏显示的 **Token**。
2. **PC**：`adb devices` 确认设备在线 → `pip install -r requirements.txt` → `python ui.py`；
3. 点击"刷新设备" → 填入手机上的 Token → "连接"，然后：
   - **消息**：Tab1 双向互发文本；
   - **文件**：Tab2 选文件即传，表格按文件分行显示进度；手机发来的文件在 `./downloads/`；手机端用"发送文件到 PC"回传；
   - **远程代理**：Tab3 填目标服务器 IP/端口/账号（密码或私钥）→ "经手机 SSH 连接" → 底部输入框敲命令，输出实时回流；
     首次连接会弹出目标主机指纹，与管理员核对后选择"信任并继续"，之后该主机不再询问；
   - **exec**：填一次性命令 → "exec 执行一次"，命令结束后通道自动关闭并回传退出码。
4. **断开**：PC 点"断开"（同时移除 adb forward）；手机上划掉 App / 停止服务即可释放监听端口。

## 七、安全与信任边界

| 边界 | 现状 | 说明 |
|---|---|---|
| PC ↔ 手机链路 | `adb forward` 只把端口开在 PC 的 127.0.0.1；手机侧 `ServerSocket` 绑定 `127.0.0.1` | 设备内其他 App 无法直接接入；PC 侧其他本机进程仍可访问转发端口，故再加 token |
| 链路鉴权 | HELLO 携带 8 位十六进制随机 token（每次服务启动重新生成，显示在手机的通知栏与界面上） | token 不对 → `ERROR{BAD_TOKEN}` 并立即断开；token 只防"设备内其他进程"，不防能读屏的人 |
| 凭据 | SSH 密码 / 私钥口令 / 私钥内容只经内存传递，不写文件 | 私钥内容以明文 JSON 经 USB 传给手机（USB 链路本地、不上网），介意的场景请改用密钥 + 跳板机侧限制来源 |
| 主机身份 | TOFU：首次连接必须由 PC 端人工确认指纹后才写入手机 `known_hosts`，之后每次连接严格比对 | 指纹变更（疑似 MITM）直接拒绝，不提供"忽略"入口；指纹格式为 OpenSSH 的 `SHA256:...`，可用 `ssh-keygen -lf` 交叉核对 |
| 明文传输 | 帧协议本身不加密 | 链路是 USB + 回环，不做二次加密；如需跨不可信环境，可在 `payload` 外层加 AES-GCM（见 §8） |

## 八、已知限制与可扩展点

| 事项 | 说明 |
|---|---|
| **会话串行** | 单连接内 `FILE_*`/`TEXT` 在同一个读循环里同步处理，大文件落盘期间文本消息会延迟（不会丢，TCP 有背压）。若要彻底解耦，可把文件写入也丢给独立队列线程 |
| **遥控终端** | 当前是"行模式"（输入整行 + `\n`），足够执行命令。**注意：JSch 的 `ChannelShell` 默认已请求 pty**，所以瓶颈不是 pty，而是缺少按键直传 + 终端仿真（`TERM` 下发、窗口尺寸 `setPtySize`、ANSI 转义解析与渲染）。要让 `htop`/`vim` 可用，需要：① PC 侧改为逐键发送并处理方向键/控制键；② 用 `pyte` 之类的终端模拟器渲染，而不是把原始字节塞进 `QPlainTextEdit` |
| **伪终端参数** | 已下发 `setPtySize(120, 30)`；`setEnv("TERM", ...)` 需目标 sshd 开启 `AcceptEnv`，否则被忽略，可在远端 `export TERM=xterm-256color` 兜底 |
| **背压** | 手机端 `REMOTE_DATA` 写 stdin 的管道为 1MB，写满后写线程会阻塞（只阻塞远程通道队列，不影响文本/文件/心跳）；无上限的快速输入仍可能堆积，必要时改为有界队列 + 丢弃策略 |
| **大文件/断点续传** | 已分块 + 双端 SHA256 校验；续传只需 `FILE_META` 增加 `offset` 字段，接收端 seek 写入 |
| **多设备/多会话** | 服务端支持多条连接，但 PC 端一次只连一个设备；多设备需在 PC 侧按 serial 分别 forward 到不同本地端口 |
| **免装 App 替代** | 手机装 Termux 跑 `sshd`，PC `adb forward` 后先 SSH 进手机，再从手机 SSH 到目标 —— 可快速验证但无法自定义 UI 和文件管理 |
| **免 adb 方案** | 需手机 root 或系统级支持（如车机/定制 ROM 直接暴露 TCP），否则 ADB 是标准做法 |
| **通道加密** | 如需在不可信网络下使用，可在 `TEXT`/`FILE_*`/`REMOTE_*` 的 payload 上做 AES-GCM，密钥由 token 派生（HKDF） |

## 九、变更记录（相对初版）

本次修订相对初版的变更清单（问题来自对初版的静态审查）：

| 级别 | 问题 | 处理 |
|---|---|---|
| P0 | Kotlin `FrameIO.encode` 把 header 写在固定偏移 9，导致第 5–8 字节为 0 且 header 尾部被 payloadLen 覆盖 → PC 端 `json.loads` 必失败、协议完全不通 | `h.copyInto(this, i)`，按 `i` 顺序写入；并在协议章节补充"字段顺序必须严格一致"的说明 |
| P1 | 需求 1/2 的"手机 → PC"方向未实现，且初版建议用 `adb reverse` 绕行 | 明确"复用同一条已建立连接"：新增 `SessionHandler.sendText/sendFile` + `BridgeService.broadcast*` + `MainActivity` 发送入口；协议章节改为双向 |
| P1 | 双向 id 都从 1 递增，同传文件时 PC 表格会把两个文件混成一行 | 约定 id 命名空间：PC 从 1，手机从 `0x40000000`（`FrameIO.nextId()`） |
| P1 | `kind=exec` 无入口、`ChannelExec` 无 stdin | PC 新增"exec 执行一次"入口与命令输入框；`ChannelExec.setInputStream` 支持 stdin，并回传退出码 |
| P2 | `ServerSocket(9999)` 监听 0.0.0.0，设备内任一 App 可白用 SSH 代理 | 绑定 `127.0.0.1` + HELLO token 校验（`BAD_TOKEN` 即断开） |
| P2 | `REMOTE_DATA` 写在唯一读循环里，管道写满会永久卡死整条连接 | 远程通道操作改由每会话单线程 `remoteOps` 串行执行（保证 OPEN→DATA 顺序，但不再阻塞 TEXT/FILE/PING）；stdin 管道扩到 1MB |
| P2 | `REMOTE_OPEN` 同步 SSH 握手最长阻塞 10s，期间心跳/文本全停 | 同上，握手改在 `remoteOps` 中执行 |
| P2 | `onStartCommand` 可重入，重复点按钮导致 `BindException` 静默失败 | 增加 `running` 防重入标记 + 异常记录 |
| P2 | `StrictHostKeyChecking=no` 不做主机校验，密码可被伪主机窃取 | 引入 `TofuHostKeys`（TOFU）与 PC 端指纹确认弹窗；指纹变更直接拒绝；新增私钥认证路径 |
| P2 | 重名规则两端不一致（`(1)name` vs `name(1).ext`），手机端不校验 SHA256 | 统一为 `name(1).ext`；`FileSink.finish(ok, sha256)` 校验失败即删除 |
| P2 | 断线只 `_recv.clear()`，文件句柄泄漏且残留半个文件 | 新增 `_cleanup_partial()`：关闭句柄并删除未完成文件 |
| P2 | Android 13+ 未申请通知权限 → "PC 文本到达"提示静默失效；`Notification.Builder(ctx, id)` 在 API<26 崩溃 | `MainActivity` 运行时申请权限；通知构建按版本分支；未授权时跳过通知 |
| P2 | UI：断开未重置 `self.ch`、无"断开"按钮、adb forward 残留、单个进度条无法承载并发 | 新增断开按钮与 `self.ch = None`；forward 前后清理；文件 Tab 改 `QTableWidget` 按 id 分行 |
| P3 | 文档缺少需求/验收/非功能/异常章节 | 新增 §1 需求与范围（F1–F11 验收标准、非功能需求、异常策略、out of scope） |
| P3 | 初版称"支持 vim/htop 需配置 pty"不准确 | 更正为：`ChannelShell` 默认已带 pty，真正缺口是按键直传 + 终端仿真；补充 `setPtySize`/`TERM` 与实现路径 |
| P3 | 笔误与缺项（`requirements:` 反引号、缺依赖清单/版本要求） | 修正排版，补 `requirements.txt` 与 Python/adb 版本说明 |