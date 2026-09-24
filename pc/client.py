"""Core business logic: text messages, bidirectional file transfer, remote calls (phone as SSH proxy).

PC-side id space starts at 1 (phone side starts at 0x40000000) to avoid id collisions in both directions.
"""
import hashlib
import os
import threading
import time
from base64 import b64decode, b64encode

from identity import PcIdentity
from link_crypto import LinkSeal, derive_session_key, ecdh, generate_keypair
from protocol import ErrCode, MsgType
from transport import Transport

CHUNK_SIZE = 256 * 1024
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB per PRD NFR
HEARTBEAT_INTERVAL = 15
PONG_TIMEOUT = 45


class PhoneClient:
    def __init__(self):
        self.tp = None
        self._id_lock = threading.Lock()
        self._next_id = 1                      # PC-side id space: starts at 1
        self._hb_gen = 0                       # invalidate old heartbeat threads
        self._last_pong = 0.0
        self._pending_up = {}                  # fid -> {"name": str}
        self._cancel_up = set()                # fids the user asked to abort
        # ---- Event callbacks (fired on the network thread; the UI layer switches back to the main thread) ----
        self.on_text = None            # fn(text)
        self.on_hello_ack = None       # fn(info)
        self.on_status = None          # fn(str)
        self.on_file_progress = None   # fn(fid, name, sent, total, direction)
        self.on_file_done = None       # fn(fid, name, ok, direction, saved_path)
        self.on_remote_output = None   # fn(channel, stream, bytes)
        self.on_remote_close = None    # fn(channel, code, reason)
        self.on_remote_error = None    # fn(channel, code, header)
        self.on_conn_list = None       # fn(list[dict])
        self.on_attach_ack = None      # fn(header, backlog_bytes)
        self._recv, self.channels = {}, {}
        self.save_dir = os.path.abspath("downloads")
        os.makedirs(self.save_dir, exist_ok=True)
        self.identity = PcIdentity.load_or_create()
        self._pending_eph_priv = None
        self._pending_token = ""

    # ---------- Connection ----------
    def connect(self, host="127.0.0.1", port=12580, token=""):
        self.close(silent=True)                # drop prior transport without status noise
        self._pending_token = token or ""
        self._pending_eph_priv, eph_pub = generate_keypair()
        self.tp = Transport(host, port, self._on_frame, self._on_disconnect)
        self.tp.start()
        self._last_pong = time.monotonic()
        self.tp.send(MsgType.HELLO, {
            "client": "pc-client",
            "version": 2,
            "token": token,
            "pc_id": self.identity.pc_id,
            "pc_name": self.identity.pc_name,
            "pc_pubkey": self.identity.pub_b64(),
            "eph_pub": b64encode(eph_pub).decode("ascii"),
        })
        # Heartbeat starts only after ACK + seal (see _on_frame)

    def close(self, silent=False):
        self._hb_gen += 1                      # stop any running heartbeat loop
        self._pending_eph_priv = None
        if self.tp:
            old = self.tp
            self.tp = None
            old.on_disconnect = None           # avoid stale recv_loop wiping a new session
            old.close()
            self._cleanup_partial()
            if not silent:
                self.on_status and self.on_status("[Connection closed]")

    def _heartbeat(self, gen):
        while gen == self._hb_gen and self.tp and self.tp.alive:
            time.sleep(HEARTBEAT_INTERVAL)
            if gen != self._hb_gen or not self.tp or not self.tp.alive:
                break
            if time.monotonic() - self._last_pong > PONG_TIMEOUT:
                self.on_status and self.on_status("[Connection closed] heartbeat timeout")
                try:
                    self.tp.close()
                except Exception:
                    pass
                break
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
        """On disconnect, close handles and delete half-written files to avoid leftovers and handle leaks."""
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
        for fid, st in list(self._pending_up.items()):
            self.on_file_done and self.on_file_done(fid, st["name"], False, "up", "disconnected")
        self._pending_up.clear()
        self._cancel_up.clear()

    def _on_disconnect(self):
        self._cleanup_partial()
        self.on_status and self.on_status("[Connection closed]")

    def _fail_upload(self, fid, message=""):
        self._cancel_up.discard(fid)
        st = self._pending_up.pop(fid, None)
        if st:
            self.on_file_done and self.on_file_done(fid, st["name"], False, "up", message)

    def cancel_upload(self, fid: int):
        """Abort an in-flight upload; the chunk loop stops and tells the phone to delete its partial."""
        if fid in self._pending_up:
            self._cancel_up.add(fid)

    # ---------- Feature 2: text messages (bidirectional) ----------
    def send_text(self, text: str):
        self.tp.send(MsgType.TEXT, {"id": self._new_id(), "text": text})

    # ---------- Feature 1: file transfer (PC -> phone) ----------
    def send_file(self, path: str):
        def worker():
            name, size = os.path.basename(path), os.path.getsize(path)
            if size > MAX_FILE_SIZE:
                fid = self._new_id()
                self.on_file_done and self.on_file_done(
                    fid, name, False, "up", "file exceeds 2GB limit")
                return
            fid = self._new_id()
            self._pending_up[fid] = {"name": name}
            sha = hashlib.sha256()
            sent = 0
            try:
                self.tp.send(MsgType.FILE_META, {"id": fid, "name": name, "size": size})
                with open(path, "rb") as f:
                    while chunk := f.read(CHUNK_SIZE):
                        if fid in self._cancel_up:
                            # ok:false makes the phone finish() and delete its partial file.
                            self.tp.send(MsgType.FILE_END, {"id": fid, "ok": False})
                            self._fail_upload(fid, "cancelled")
                            return
                        sha.update(chunk)
                        self.tp.send(MsgType.FILE_CHUNK, {"id": fid}, chunk)
                        sent += len(chunk)
                        self.on_file_progress and self.on_file_progress(fid, name, sent, size, "up")
                self.tp.send(MsgType.FILE_END, {"id": fid, "ok": True, "sha256": sha.hexdigest()})
                # Done/Failed comes from phone ACK{file_id, ok} (or FILE_WRITE_FAILED)
            except Exception as e:
                self._fail_upload(fid, str(e))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- File receive (phone -> PC) ----------
    def _on_file_meta(self, h):
        size = h.get("size", 0) or 0
        if size > MAX_FILE_SIZE:
            self.on_status and self.on_status(
                f"[Error] {ErrCode.FILE_WRITE_FAILED} file exceeds 2GB limit")
            return
        safe = os.path.basename(h.get("name", "unnamed"))
        path = os.path.join(self.save_dir, safe)
        stem, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(path):          # Keep the same name(1).ext rule as the phone side
            path = f"{stem}({i}){ext}"
            i += 1
        self._recv[h["id"]] = {"fp": open(path, "wb"), "name": safe,
                               "size": size, "got": 0,
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

    # ---------- Feature 3: attach to phone-managed SSH connections ----------
    def list_connections(self):
        if self.tp and self.tp.alive:
            self.tp.send(MsgType.CONN_LIST, {})

    def attach(self, connection_id: str) -> int:
        ch = self._new_id()
        self.channels[ch] = {"connection_id": connection_id}
        self.tp.send(MsgType.CONN_ATTACH, {"channel": ch, "connection_id": connection_id})
        return ch

    def detach(self, ch: int):
        if ch in self.channels:
            self.tp.send(MsgType.CONN_DETACH, {"channel": ch})
            self.channels.pop(ch, None)

    def remote_input(self, ch: int, data: bytes):
        self.tp.send(MsgType.REMOTE_DATA, {"channel": ch}, data)

    def remote_close(self, ch: int):
        """Legacy alias: detach without stopping phone SSH."""
        self.detach(ch)

    # ---------- Message dispatch ----------
    def _on_frame(self, t, h, p):
        M = MsgType
        try:
            if t == M.TEXT:
                self.on_text and self.on_text(h.get("text", ""))
            elif t == M.ACK:
                if "device" in h:
                    eph_b64 = h.get("eph_pub")
                    if eph_b64 and self._pending_eph_priv and self.tp:
                        try:
                            shared = ecdh(self._pending_eph_priv, b64decode(eph_b64))
                            key = derive_session_key(
                                shared, self._pending_token, self.identity.pc_id)
                            self.tp.enable_seal(LinkSeal(key))
                            self._pending_eph_priv = None
                            self._last_pong = time.monotonic()
                            self._hb_gen += 1
                            gen = self._hb_gen
                            threading.Thread(
                                target=self._heartbeat, args=(gen,), daemon=True).start()
                        except Exception as e:
                            self.on_status and self.on_status(
                                f"[Error] link crypto failed: {e}")
                            self.close()
                            return
                    elif self._pending_eph_priv:
                        self.on_status and self.on_status(
                            "[Error] phone ACK missing eph_pub (upgrade phone app)")
                        self.close()
                        return
                    self.on_hello_ack and self.on_hello_ack(h)
                elif "file_id" in h:
                    fid = h["file_id"]
                    st = self._pending_up.pop(fid, None)
                    if st:
                        ok = bool(h.get("ok"))
                        path = h.get("path", "") if ok else h.get("path", "") or ""
                        tip = path if ok else (path or "phone rejected / SHA mismatch")
                        self.on_file_done and self.on_file_done(fid, st["name"], ok, "up", tip)
                elif h.get("connection_id") is not None and "channel" in h:
                    self.on_attach_ack and self.on_attach_ack(h, p or b"")
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
            elif t == M.CONN_LIST_RESULT:
                self.on_conn_list and self.on_conn_list(h.get("connections") or [])
            elif t == M.REMOTE_OUTPUT:
                self.on_remote_output and self.on_remote_output(h["channel"], h.get("stream", "out"), p)
            elif t == M.REMOTE_CLOSE:
                self.channels.pop(h["channel"], None)
                self.on_remote_close and self.on_remote_close(
                    h["channel"], h.get("code", 0), h.get("reason", ""))
            elif t == M.PING:
                self.tp.send(M.PONG, {})
            elif t == M.PONG:
                self._last_pong = time.monotonic()
            elif t == M.ERROR:
                code = h.get("code", "")
                if code == ErrCode.FILE_WRITE_FAILED and "id" in h:
                    self._fail_upload(h["id"], h.get("message", "") or code)
                    self.on_status and self.on_status(
                        f"[Error] {code} {h.get('message', '')}")
                elif "channel" in h and self.on_remote_error:
                    self.on_remote_error(h["channel"], code, h)
                else:
                    self.on_status and self.on_status(
                        f"[Error] {code} {h.get('message', '')}")
        except Exception as e:
            self.on_status and self.on_status(f"[Handler exception] {e}")
