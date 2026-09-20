"""核心业务：文本消息、文件双向传输、远程调用（手机作 SSH 代理）。

PC 侧 id 空间从 1 起（手机侧从 0x40000000 起），避免双向 id 冲突。
"""
import hashlib
import os
import threading
import time

from protocol import MsgType
from transport import Transport

CHUNK_SIZE = 256 * 1024


class PhoneClient:
    def __init__(self):
        self.tp = None
        self._id_lock = threading.Lock()
        self._next_id = 1                      # PC 侧 id 空间：1 起
        # ---- 事件回调（在网络线程中触发，UI 层负责切回主线程）----
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
            try:
                self.tp.send(MsgType.PING, {})
            except Exception:
                break

    def _new_id(self):
        with self._id_lock:
            v = self._next_id
            self._next_id += 1
            return v

    def _cleanup_partial(self):
        """断线时关闭句柄并删除写了一半的文件，避免残留与句柄泄漏。"""
        for st in self._recv.values():
            try:
                st["fp"].close()
            except Exception:
                pass
            try:
                if os.path.exists(st["path"]):
                    os.remove(st["path"])
            except Exception:
                pass
        self._recv.clear()
        self.channels.clear()

    def _on_disconnect(self):
        self._cleanup_partial()
        self.on_status and self.on_status("[连接已断开]")

    # ---------- 功能2：文本消息（双向） ----------
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
            path = f"{stem}({i}){ext}"
            i += 1
        self._recv[h["id"]] = {"fp": open(path, "wb"), "name": safe,
                               "size": h.get("size", 0), "got": 0,
                               "path": path, "sha": hashlib.sha256()}

    def _on_file_end(self, h):
        st = self._recv.pop(h["id"], None)
        if not st:
            return
        st["fp"].close()
        ok = bool(h.get("ok")) and (not h.get("sha256") or h["sha256"] == st["sha"].hexdigest())
        if not ok:
            try:
                os.remove(st["path"])
            except Exception:
                pass
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
            if t == M.TEXT:
                self.on_text and self.on_text(h.get("text", ""))
            elif t == M.ACK:
                "device" in h and self.on_hello_ack and self.on_hello_ack(h)
            elif t == M.FILE_META:
                self._on_file_meta(h)
            elif t == M.FILE_CHUNK:
                st = self._recv.get(h["id"])
                if st:
                    st["fp"].write(p)
                    st["sha"].update(p)
                    st["got"] += len(p)
                    self.on_file_progress and self.on_file_progress(
                        h["id"], st["name"], st["got"], st["size"], "down")
            elif t == M.FILE_END:
                self._on_file_end(h)
            elif t == M.REMOTE_OUTPUT:
                self.on_remote_output and self.on_remote_output(h["channel"], h.get("stream", "out"), p)
            elif t == M.REMOTE_CLOSE:
                self.channels.pop(h["channel"], None)
                self.on_remote_close and self.on_remote_close(
                    h["channel"], h.get("code", 0), h.get("reason", ""))
            elif t == M.PING:
                self.tp.send(M.PONG, {})
            elif t == M.ERROR:
                if "channel" in h and self.on_remote_error:
                    self.on_remote_error(h["channel"], h.get("code", ""), h)
                else:
                    self.on_status and self.on_status(
                        f"[错误] {h.get('code', '')} {h.get('message', '')}")
        except Exception as e:
            self.on_status and self.on_status(f"[处理异常] {e}")
