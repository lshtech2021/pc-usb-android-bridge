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
        self.on_fs_opened = None       # fn(header)
        self.on_fs_closed = None       # fn(channel, code, reason)
        self.on_fs_list = None         # fn(header)
        self.on_fs_op_result = None    # fn(header)
        self.on_fs_progress = None     # fn(id, name, sent, total, direction)
        self.on_fs_done = None         # fn(id, name, ok, direction, path)
        self.on_fs_error = None        # fn(channel, code, header)   (defensive only)
        self._recv, self.channels = {}, {}
        # Remote file explorer (feature 4): SFTP over the phone's SSH sessions.
        self._fs_pending = {}          # channel -> {"connection_id"}  (FS_OPEN sent, awaiting FS_OPENED)
        self._fs = {}                  # channel -> {"connection_id"}  (after FS_OPENED ok)
        self._fs_recv = {}             # id -> {"channel","name","path","fp","sha","got","size"}
        self._fs_up = {}               # id -> {"channel","name","remote_path","evt","meta_ok","err"}
        self._fs_cancel = set()        # upload ids the user asked to abort
        # Reverse tethering (SOCKS5): sid -> {"opened","data","close"} callbacks. Streams are
        # always PC-initiated, so the PC owns the sid space for them.
        self._proxy = {}
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
        # Wake every SOCKS5 stream so it closes its client socket instead of hanging on a dead link.
        for sid, cbs in list(self._proxy.items()):
            try:
                cbs["close"](sid, "both", 0, "disconnected")
            except Exception:
                pass
        self._proxy.clear()
        for fid, st in list(self._pending_up.items()):
            self.on_file_done and self.on_file_done(fid, st["name"], False, "up", "disconnected")
        self._pending_up.clear()
        self._cancel_up.clear()
        # Remote file explorer: delete half-written downloads, fail in-flight uploads.
        for fid, st in list(self._fs_recv.items()):
            if st["fp"]:
                try:
                    st["fp"].close()
                except Exception:
                    pass
            try:
                if os.path.exists(st["path"]):
                    os.remove(st["path"])
            except Exception:
                pass
            self.on_fs_done and self.on_fs_done(fid, st["name"], False, "down", "disconnected")
        self._fs_recv.clear()
        for fid, st in list(self._fs_up.items()):
            if st["meta_ok"]:
                self.on_fs_done and self.on_fs_done(fid, st["name"], False, "up", "disconnected")
            st["evt"].set()                # unblock workers waiting on the meta accept
        self._fs_up.clear()
        self._fs_cancel.clear()
        self._fs.clear()
        self._fs_pending.clear()

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

    # ---------- Reverse tethering (SOCKS5 over the bridge) ----------
    def alloc_proxy(self, cbs: dict) -> int:
        """Reserve a sid and register the stream callbacks before PROXY_OPEN is sent.

        PROXY_OPENED can arrive on the recv thread the instant the phone accepts the
        connection, so the callback record must exist first.
        """
        sid = self._new_id()
        self._proxy[sid] = cbs
        return sid

    def proxy_send_open(self, sid: int, host: str, port: int):
        self.tp.send(MsgType.PROXY_OPEN, {"sid": sid, "host": host, "port": port})

    def proxy_data(self, sid: int, data: bytes):
        """Best-effort: drop bytes for a stream the phone has already torn down."""
        if sid in self._proxy:
            try:
                self.tp.send(MsgType.PROXY_DATA, {"sid": sid}, data)
            except Exception:
                pass

    def proxy_close(self, sid: int, dir: str, code: int = 0, reason: str = ""):
        """Best-effort close; pops the record only on full teardown."""
        if dir == "both" and self._proxy.pop(sid, None) is None:
            return
        try:
            self.tp.send(MsgType.PROXY_CLOSE,
                         {"sid": sid, "dir": dir, "code": code, "reason": reason})
        except Exception:
            pass

    # ---------- Feature 4: remote file explorer (SFTP over phone-managed SSH) ----------
    def fs_open(self, connection_id: str) -> int:
        ch = self._new_id()
        # Register before send: FS_OPENED can arrive on the recv thread instantly.
        self._fs_pending[ch] = {"connection_id": connection_id}
        self.tp.send(MsgType.FS_OPEN, {"channel": ch, "connection_id": connection_id})
        return ch

    def fs_close(self, ch: int):
        if ch in self._fs or ch in self._fs_pending:
            self.tp.send(MsgType.FS_CLOSE, {"channel": ch})
        self._fs.pop(ch, None)
        self._fs_pending.pop(ch, None)

    def fs_list(self, ch: int, path: str) -> int:
        req = self._new_id()
        self.tp.send(MsgType.FS_LIST, {"channel": ch, "req": req, "path": path})
        return req

    def fs_mkdir(self, ch: int, path: str) -> int:
        req = self._new_id()
        self.tp.send(MsgType.FS_MKDIR, {"channel": ch, "req": req, "path": path})
        return req

    def fs_delete(self, ch: int, path: str) -> int:
        req = self._new_id()
        self.tp.send(MsgType.FS_DELETE, {"channel": ch, "req": req, "path": path})
        return req

    def fs_rename(self, ch: int, path: str, path2: str) -> int:
        req = self._new_id()
        self.tp.send(MsgType.FS_RENAME, {"channel": ch, "req": req, "path": path, "path2": path2})
        return req

    def fs_download(self, ch: int, remote_path: str, local_path: str) -> int:
        """Download a remote file to local_path (resolved by the UI's save-as dialog).
        The local file is opened only when FS_DL_RESULT arrives, after the size check."""
        fid = self._new_id()
        self._fs_recv[fid] = {"channel": ch, "name": os.path.basename(remote_path),
                              "path": local_path, "fp": None, "sha": None, "got": 0, "size": 0}
        self.tp.send(MsgType.FS_DL_META, {"channel": ch, "id": fid, "path": remote_path})
        return fid

    def fs_upload(self, ch: int, local_path: str, remote_path: str) -> int:
        fid = self._new_id()
        threading.Thread(target=self._fs_upload_worker,
                         args=(fid, ch, local_path, remote_path), daemon=True).start()
        return fid

    def cancel_fs_upload(self, fid: int):
        """Abort an in-flight upload; the chunk loop tells the phone to delete its partial."""
        if fid in self._fs_up:
            self._fs_cancel.add(fid)

    def fs_abort(self, fid: int):
        """Cancel an in-flight download; the phone stops reading and reports ok:false."""
        st = self._fs_recv.get(fid)
        if st:
            self.tp.send(MsgType.FS_ABORT, {"channel": st["channel"], "id": fid})

    def _fs_upload_worker(self, fid, ch, local_path, remote_path):
        name = os.path.basename(local_path)
        try:
            size = os.path.getsize(local_path)
        except OSError as e:
            self.on_fs_done and self.on_fs_done(fid, name, False, "up", str(e))
            return
        if size > MAX_FILE_SIZE:
            self.on_fs_done and self.on_fs_done(fid, name, False, "up", "file exceeds 2GB limit")
            return
        evt = threading.Event()
        self._fs_up[fid] = {"channel": ch, "name": name, "remote_path": remote_path,
                            "evt": evt, "meta_ok": False, "err": ""}
        try:
            self.tp.send(MsgType.FS_UL_META, {"channel": ch, "id": fid,
                                              "path": remote_path, "size": size})
            if not evt.wait(15.0):
                raise TimeoutError("phone did not accept the upload")
            if not self._fs_up[fid]["meta_ok"]:
                raise RuntimeError(self._fs_up[fid].get("err") or "phone rejected the upload")
            sha = hashlib.sha256()
            sent = 0
            with open(local_path, "rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    if fid in self._fs_cancel:
                        # ok:false makes the phone delete its partial remote file.
                        self.tp.send(MsgType.FS_END, {"channel": ch, "id": fid, "ok": False})
                        raise RuntimeError("cancelled")
                    sha.update(chunk)
                    self.tp.send(MsgType.FS_CHUNK, {"channel": ch, "id": fid}, chunk)
                    sent += len(chunk)
                    self.on_fs_progress and self.on_fs_progress(fid, name, sent, size, "up")
            self.tp.send(MsgType.FS_END, {"channel": ch, "id": fid, "ok": True,
                                          "sha256": sha.hexdigest()})
            # Done/Failed arrives as ACK{fs_id} -> on_fs_done
        except Exception as e:
            self._fs_cancel.discard(fid)
            st = self._fs_up.get(fid)
            if st is None or (st["evt"].is_set() and st["meta_ok"]):
                return                       # finalized via ACK{fs_id} or a teardown path
            self._fs_up.pop(fid, None)
            self.on_fs_done and self.on_fs_done(fid, name, False, "up", str(e))

    # ---------- Feature 4 receive handlers (download state, channel close) ----------
    def _on_fs_dl_result(self, h):
        st = self._fs_recv.get(h["id"])
        if not st:
            return
        if not bool(h.get("ok")):
            self._fs_recv.pop(h["id"], None)
            self.on_fs_done and self.on_fs_done(
                h["id"], st["name"], False, "down",
                h.get("message") or h.get("code") or "")
            return
        size = h.get("size", 0) or 0
        if size > MAX_FILE_SIZE:                     # phone already aborted; clean up locally
            self._fs_recv.pop(h["id"], None)
            self.tp.send(MsgType.FS_ABORT, {"channel": st["channel"], "id": h["id"]})
            self.on_fs_done and self.on_fs_done(
                h["id"], st["name"], False, "down", "file exceeds 2GB limit")
            return
        st["size"] = size
        try:
            st["fp"] = open(st["path"], "wb")        # save-as path resolved by the UI beforehand
            st["sha"] = hashlib.sha256()
        except OSError as e:
            self._fs_recv.pop(h["id"], None)
            self.tp.send(MsgType.FS_ABORT, {"channel": st["channel"], "id": h["id"]})
            self.on_fs_done and self.on_fs_done(h["id"], st["name"], False, "down", str(e))
            return
        self.on_fs_progress and self.on_fs_progress(h["id"], st["name"], 0, size, "down")

    def _on_fs_end(self, h):
        st = self._fs_recv.pop(h["id"], None)
        if not st or not st["fp"]:
            return
        st["fp"].close()
        ok = bool(h.get("ok")) and (not h.get("sha256") or h["sha256"] == st["sha"].hexdigest())
        if not ok:
            try:
                os.remove(st["path"])
            except OSError:
                pass
        self.on_fs_done and self.on_fs_done(
            h["id"], st["name"], ok, "down",
            st["path"] if ok else (h.get("error") or ""))

    def _on_fs_close(self, h):
        ch = h.get("channel")
        self._fs.pop(ch, None)
        self._fs_pending.pop(ch, None)
        for fid, st in list(self._fs_recv.items()):  # in-flight downloads die with the channel
            if st["channel"] == ch:
                self._fs_recv.pop(fid, None)
                if st["fp"]:
                    try:
                        st["fp"].close()
                    except OSError:
                        pass
                    try:
                        os.remove(st["path"])
                    except OSError:
                        pass
                self.on_fs_done and self.on_fs_done(fid, st["name"], False, "down", "session closed")
        for fid, st in list(self._fs_up.items()):    # streaming uploads: their ACK will never come
            if st["channel"] == ch:
                self._fs_up.pop(fid, None)
                self._fs_cancel.discard(fid)
                if st["evt"].is_set():               # was streaming
                    self.on_fs_done and self.on_fs_done(fid, st["name"], False, "up", "session closed")
                else:                                # waiting on meta: let the worker fail itself
                    st["err"] = "session closed"
                    st["evt"].set()
        self.on_fs_closed and self.on_fs_closed(ch, h.get("code", 0), h.get("reason", ""))

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
                elif "fs_id" in h:                       # FS upload completion (new ACK context)
                    fid = h["fs_id"]
                    st = self._fs_up.pop(fid, None)
                    if st:
                        ok = bool(h.get("ok"))
                        tip = (h.get("path", "") or "") if ok else \
                            (h.get("error") or "phone rejected / SHA mismatch")
                        self.on_fs_done and self.on_fs_done(fid, st["name"], ok, "up", tip)
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
            elif t == M.PROXY_OPENED:
                cb = self._proxy.get(h.get("sid"))
                if cb:
                    cb["opened"](h.get("sid"), bool(h.get("ok")),
                                 h.get("code", ""), h.get("message", ""))
            elif t == M.PROXY_DATA:
                cb = self._proxy.get(h.get("sid"))
                if cb:
                    cb["data"](h.get("sid"), p)
            elif t == M.PROXY_CLOSE:
                sid = h.get("sid")
                cb = self._proxy.get(sid)
                if cb:
                    d = h.get("dir", "both")
                    if d == "both":
                        self._proxy.pop(sid, None)
                    cb["close"](sid, d, h.get("code", 0), h.get("reason", ""))
            elif t == M.FS_OPENED:
                ch = h.get("channel")
                pend = self._fs_pending.pop(ch, None)
                if bool(h.get("ok")) and pend:
                    self._fs[ch] = {"connection_id": pend["connection_id"]}
                self.on_fs_opened and self.on_fs_opened(h)
            elif t == M.FS_CLOSE:
                self._on_fs_close(h)
            elif t == M.FS_LIST_RESULT:
                self.on_fs_list and self.on_fs_list(h)
            elif t == M.FS_OP_RESULT:
                self.on_fs_op_result and self.on_fs_op_result(h)
            elif t == M.FS_DL_RESULT:
                self._on_fs_dl_result(h)
            elif t == M.FS_UL_RESULT:
                st = self._fs_up.get(h["id"])
                if st:
                    if bool(h.get("ok")):
                        st["meta_ok"] = True
                    else:
                        st["err"] = h.get("message") or h.get("code") or "phone rejected"
                    st["evt"].set()
            elif t == M.FS_CHUNK:
                st = self._fs_recv.get(h["id"])
                if st and st["fp"] and h.get("channel") == st["channel"]:
                    st["fp"].write(p)
                    st["sha"].update(p)
                    st["got"] += len(p)
                    self.on_fs_progress and self.on_fs_progress(
                        h["id"], st["name"], st["got"], st["size"], "down")
            elif t == M.FS_END:
                self._on_fs_end(h)
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
                elif "sid" in h and h.get("sid") in self._proxy:
                    sid = h["sid"]
                    cb = self._proxy.pop(sid)
                    cb["close"](sid, "both", code, h.get("message", "") or code)
                elif "channel" in h and h.get("channel") in self._fs and self.on_fs_error:
                    # Defensive only: the phone reports FS failures via result frames.
                    self.on_fs_error(h["channel"], code, h)
                elif "channel" in h and self.on_remote_error:
                    self.on_remote_error(h["channel"], code, h)
                else:
                    self.on_status and self.on_status(
                        f"[Error] {code} {h.get('message', '')}")
        except Exception as e:
            self.on_status and self.on_status(f"[Handler exception] {e}")
