"""End-to-end regression test for the remote file explorer (FS_* over the sealed bridge).

A stub phone speaks the real protocol on loopback and behaves like ConnectionHub + an SFTP
channel: FS_OPEN/FS_OPENED, listings, management ops, chunked uploads/downloads with
SHA-256 verification, aborts and teardown. The real PhoneClient sits on the other side.

Covered: open ok + each failure code, list delivery, mkdir op result, upload (happy path,
cancel, meta rejection, phone-side SHA mismatch, local oversize), download (happy path,
SHA mismatch, FS_DL_RESULT error, oversize -> local abort, mid-flight abort), phone-initiated
FS_CLOSE failing in-flight rows, disconnect cleanup, and JSON null omission in headers.
"""
import hashlib
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
# Keep the test out of the user's ~/.usbbridge identity.
os.environ.setdefault("USBBRIDGE_HOME", tempfile.mkdtemp(prefix="usbbridge-test-"))

from base64 import b64decode, b64encode  # noqa: E402

from client import PhoneClient  # noqa: E402
from link_crypto import LinkSeal, derive_session_key, ecdh, generate_keypair  # noqa: E402
from protocol import MsgType, decode_frame, encode_frame  # noqa: E402

TOKEN = "deadbeef"
DEADLINE = 20.0
CHUNK = 256 * 1024


def wait_until(pred, what="condition"):
    end = time.monotonic() + DEADLINE
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


class StubDownload(threading.Thread):
    """Phone-side download stream, like FsDownload: its own thread plus abort support.
    An optional gate parks the thread before the first chunk, so tests can abort
    deterministically while the phone's read loop stays free to process FS_ABORT."""

    def __init__(self, phone, channel, fid, payload, sha, error, gate):
        super().__init__(daemon=True)
        self.phone, self.channel, self.fid = phone, channel, fid
        self.payload, self.sha, self.error, self.gate = payload, sha, error, gate
        self.aborted = threading.Event()

    def run(self):
        try:
            if self.gate is not None:
                self.gate.wait(DEADLINE)
            if not self.aborted.is_set():
                for pos in range(0, len(self.payload), CHUNK):
                    if self.aborted.is_set():
                        break
                    self.phone.send(MsgType.FS_CHUNK,
                                    {"channel": self.channel, "id": self.fid},
                                    self.payload[pos:pos + CHUNK])
            if self.aborted.is_set():
                self._end(False, "aborted")
            elif self.sha is not None:
                self._end(True, self.sha)
            else:
                self._end(False, self.error)
        except OSError:
            pass                                 # bridge closed during the stream

    def _end(self, ok, detail):
        h = {"channel": self.channel, "id": self.fid, "ok": ok}
        h["sha256" if ok else "error"] = detail
        self.phone.send(MsgType.FS_END, h)


class Recorder:
    """Collects the PhoneClient FS callbacks (all fired on the network thread)."""

    def __init__(self, client):
        self.opened, self.closed, self.lists, self.ops = [], [], [], []
        self.progress, self.done = [], []
        client.on_fs_opened = self.opened.append
        client.on_fs_closed = lambda *a: self.closed.append(a)
        client.on_fs_list = self.lists.append
        client.on_fs_op_result = self.ops.append
        client.on_fs_progress = lambda *a: self.progress.append(a)
        client.on_fs_done = lambda *a: self.done.append(a)


class FsStubPhone:
    """Android stand-in: HELLO/ACK handshake, then a configurable FS_* behaviour."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.conn = None
        self.seal = None
        self.send_lock = threading.Lock()
        self.errors = []
        self.frames = []                # every (type, header) the phone received
        self.close_seen = []            # channels the PC closed
        self.aborts = []                # download ids the PC aborted
        # ---- configurable replies ----
        self.fs_opened_reply = {"ok": True}
        self.list_result = {"ok": True}
        self.list_entries = None        # None -> reply exactly list_result (e.g. an error)
        self.op_result_ok = True
        self.dl_result = {"ok": True}
        self.dl_payload = b""
        self.dl_sha = None              # None -> FS_END {ok:false, error:dl_error}
        self.dl_error = "boom"
        self.dl_gate = None             # Event: park the download stream before chunk 1
        self.dls = {}                   # id -> StubDownload
        self.ul_result = {"ok": True}
        self.upload_ack = None          # None -> verify sha and answer accordingly
        self.upload_id = None
        self.upload_md = hashlib.sha256()
        self.upload_got = 0
        self.upload_gate = None         # Event: block chunk intake once set (mid-upload pause)
        self.upload_gate_at = 0         # ...after this many chunks
        self.abort_replies = True       # FS_ABORT -> FS_END {ok:false, error:"aborted"}
        threading.Thread(target=self._accept, daemon=True).start()

    # ---- phone -> PC ----
    def send(self, mtype, header, payload=b""):
        with self.send_lock:
            self.conn.sendall(encode_frame(mtype, header, payload, seal=self.seal))

    def _accept(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        conn.settimeout(DEADLINE)
        self.conn = conn
        f = conn.makefile("rb")

        def read(n):
            data = f.read(n)
            if len(data) < n:
                raise ConnectionError("peer closed")
            return data

        try:
            t, h, _ = decode_frame(read, seal=None)          # HELLO is cleartext
            if t != MsgType.HELLO or h.get("token") != TOKEN:
                raise AssertionError(f"bad HELLO: {t} {h}")
            priv, pub_der = generate_keypair()
            shared = ecdh(priv, b64decode(h["eph_pub"]))
            key = derive_session_key(shared, TOKEN, h["pc_id"])
            self.send(MsgType.ACK, {"ok": True, "device": "FsStubPhone",
                                    "eph_pub": b64encode(pub_der).decode("ascii")})
            self.seal = LinkSeal(key)
            while True:
                t, h, p = decode_frame(read, seal=self.seal)
                self._handle(t, h, p)
        except Exception as e:
            self.errors.append(f"{type(e).__name__}: {e}")

    def _handle(self, mtype, h, p):
        M = MsgType
        self.frames.append((mtype, dict(h)))
        if mtype == M.PING:
            self.send(M.PONG, {})
        elif mtype == M.FS_OPEN:
            self.send(M.FS_OPENED, dict(self.fs_opened_reply, channel=h["channel"]))
        elif mtype == M.FS_CLOSE:
            self.close_seen.append(h.get("channel"))
        elif mtype == M.FS_LIST:
            if self.list_entries is None:
                self.send(M.FS_LIST_RESULT,
                          dict(self.list_result, channel=h["channel"], req=h["req"],
                               path=h["path"]))
            else:
                self.send(M.FS_LIST_RESULT, {"channel": h["channel"], "req": h["req"],
                                             "ok": True, "path": h["path"],
                                             "entries": self.list_entries})
        elif mtype in (M.FS_MKDIR, M.FS_DELETE, M.FS_RENAME):
            op = {M.FS_MKDIR: "mkdir", M.FS_DELETE: "delete", M.FS_RENAME: "rename"}[mtype]
            self.send(M.FS_OP_RESULT, {"channel": h["channel"], "req": h["req"],
                                       "op": op, "ok": self.op_result_ok})
        elif mtype == M.FS_DL_META:
            if self.dl_result.get("ok", True):
                size = self.dl_result.get("size", len(self.dl_payload))
                self.send(M.FS_DL_RESULT, {"channel": h["channel"], "id": h["id"],
                                           "ok": True, "size": size})
                dl = StubDownload(self, h["channel"], h["id"], self.dl_payload,
                                  self.dl_sha, self.dl_error, self.dl_gate)
                self.dls[h["id"]] = dl
                dl.start()
            else:
                self.send(M.FS_DL_RESULT,
                          dict(self.dl_result, channel=h["channel"], id=h["id"]))
        elif mtype == M.FS_UL_META:
            if self.ul_result.get("ok", True):
                self.upload_id = h["id"]
                self.upload_md = hashlib.sha256()
                self.upload_got = 0
                self.send(M.FS_UL_RESULT, {"channel": h["channel"], "id": h["id"],
                                           "ok": True})
            else:
                self.send(M.FS_UL_RESULT,
                          dict(self.ul_result, channel=h["channel"], id=h["id"]))
        elif mtype == M.FS_CHUNK:
            self.upload_md.update(p)
            self.upload_got += len(p)
            if self.upload_gate is not None and self.upload_got >= self.upload_gate_at * CHUNK:
                self.upload_gate.wait(DEADLINE)     # pause mid-upload, like a slow server
        elif mtype == M.FS_END:
            if self.upload_ack is not None:
                self.send(M.ACK, dict(self.upload_ack, fs_id=h["id"]))
            elif h.get("ok"):
                ok = h.get("sha256") == self.upload_md.hexdigest()
                self.send(M.ACK, {"fs_id": h["id"], "ok": ok,
                                  **({"path": "/srv/x.bin"} if ok
                                     else {"error": "SHA mismatch"})})
            else:
                self.send(M.ACK, {"fs_id": h["id"], "ok": False, "error": "cancelled"})
        elif mtype == M.FS_ABORT:
            self.aborts.append(h.get("id"))
            dl = self.dls.get(h.get("id"))
            if dl is not None:
                dl.aborted.set()                 # the stream thread answers FS_END "aborted"


def make_pair():
    phone = FsStubPhone()
    client = PhoneClient()
    rec = Recorder(client)
    client.connect("127.0.0.1", phone.port, TOKEN)
    wait_until(lambda: client.tp and client.tp.alive and phone.seal is not None,
               "handshake + seal")
    return phone, client, rec


def open_fs(phone, client, rec):
    ch = client.fs_open("ID_01")
    wait_until(lambda: rec.opened and rec.opened[-1].get("ok"), "FS_OPENED ok")
    return ch


def shut(phone, client):
    client.close(silent=True)
    phone.srv.close()


def test_fs_open_list_op_close():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        assert rec.opened[-1]["channel"] == ch
        assert client._fs.get(ch, {}).get("connection_id") == "ID_01"

        phone.list_entries = [
            {"name": "docs", "type": "d"},
            {"name": "a.txt", "type": "-", "size": 12, "mtime": 1700000000},
            {"name": "ln", "type": "l"},
            {"name": "b.txt", "type": "-", "size": 3},
        ]
        req = client.fs_list(ch, "/home/u")
        wait_until(lambda: rec.lists, "FS_LIST_RESULT")
        lh = rec.lists[0]
        assert lh["ok"] and lh["req"] == req and lh["path"] == "/home/u"
        assert [e["name"] for e in lh["entries"]] == ["docs", "a.txt", "b.txt", "ln"] \
            or [e["name"] for e in lh["entries"]] == ["docs", "a.txt", "ln", "b.txt"]
        assert lh["entries"][1]["size"] == 12 and lh["entries"][1]["mtime"] == 1700000000

        req2 = client.fs_mkdir(ch, "/home/u/new")
        wait_until(lambda: rec.ops, "FS_OP_RESULT")
        op = rec.ops[0]
        assert op["ok"] and op["op"] == "mkdir" and op["req"] == req2

        client.fs_close(ch)
        wait_until(lambda: phone.close_seen == [ch], "FS_CLOSE seen by phone")
    finally:
        shut(phone, client)


def test_fs_open_failure_codes():
    phone, client, rec = make_pair()
    try:
        phone.fs_opened_reply = {"ok": False, "code": "CONN_NOT_RUNNING",
                                 "message": "connection is not running"}
        ch = client.fs_open("ID_09")
        wait_until(lambda: rec.opened, "FS_OPENED error")
        h = rec.opened[0]
        assert not h["ok"] and h["code"] == "CONN_NOT_RUNNING" and h["channel"] == ch
        assert ch not in client._fs and ch not in client._fs_pending

        phone.fs_opened_reply = {"ok": False, "code": "FS_BUSY",
                                 "message": "file browser already attached"}
        client.fs_open("ID_01")
        wait_until(lambda: len(rec.opened) == 2, "second FS_OPENED")
        assert not rec.opened[1]["ok"] and rec.opened[1]["code"] == "FS_BUSY"
    finally:
        shut(phone, client)


def test_fs_upload_flow():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(os.urandom(600_000))                     # 2.3 chunks
            path = f.name
        fid = client.fs_upload(ch, path, "/srv/x.bin")
        wait_until(lambda: rec.done, "upload done")
        fid2, name, ok, d, tip = rec.done[0]
        assert (fid2, name, ok, d) == (fid, os.path.basename(path), True, "up")
        assert tip == "/srv/x.bin"
        assert phone.upload_got == 600_000
        with open(path, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        assert phone.upload_md.hexdigest() == want
        assert any(t == MsgType.FS_CHUNK for t, _ in phone.frames)
        assert any(p[0] == fid and p[4] == "up" for p in rec.progress)
        os.unlink(path)
    finally:
        shut(phone, client)


def test_fs_upload_cancel():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        # Pause the phone's chunk intake after 3 chunks so the cancel lands mid-stream
        # deterministically (an uncapped upload over loopback finishes before the test
        # thread can react). The blocked read loop also backs the worker up in send(),
        # which is exactly the production shape under a slow server.
        phone.upload_gate = threading.Event()
        phone.upload_gate_at = 3
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(os.urandom(32 * 1024 * 1024))            # 128 chunks
            path = f.name
        fid = client.fs_upload(ch, path, "/srv/big.bin")
        wait_until(lambda: any(p[0] == fid for p in rec.progress), "first upload progress")
        wait_until(lambda: sum(1 for t, _ in phone.frames if t == MsgType.FS_CHUNK) >= 3,
                   "phone entered the chunk gate")
        client.cancel_fs_upload(fid)
        phone.upload_gate.set()
        wait_until(lambda: rec.done, "cancel result")
        fid2, name, ok, d, tip = rec.done[0]
        assert (fid2, ok, d) == (fid, False, "up") and tip == "cancelled"
        ends = [h for t, h in phone.frames
                if t == MsgType.FS_END and h.get("id") == fid and not h.get("ok")]
        assert ends, "no cancel FS_END reached the phone"
        os.unlink(path)
    finally:
        shut(phone, client)


def test_fs_upload_rejected_and_oversize():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        phone.ul_result = {"ok": False, "code": "FS_IO_FAILED",
                           "message": "cannot open remote file"}
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b"x" * 1000)
            path = f.name
        fid = client.fs_upload(ch, path, "/bad/x.bin")
        wait_until(lambda: rec.done, "meta rejection")
        assert rec.done[0][0] == fid and not rec.done[0][2]
        assert "cannot open remote file" in rec.done[0][4]
        assert not any(t == MsgType.FS_CHUNK for t, _ in phone.frames), "chunks after rejection"
        os.unlink(path)

        # Local oversize: rejected before any frame reaches the phone.
        big = os.path.join(tempfile.mkdtemp(prefix="usbbridge-test-"), "big.bin")
        with open(big, "wb") as f:
            f.truncate(3 * 1024 * 1024 * 1024)               # sparse, instant
        before = len(phone.frames)
        fid2 = client.fs_upload(ch, big, "/srv/big.bin")
        wait_until(lambda: len(rec.done) == 2, "oversize rejection")
        assert rec.done[1][0] == fid2 and not rec.done[1][2]
        assert "2GB" in rec.done[1][4]
        assert len(phone.frames) == before, "oversize upload reached the phone"
        os.unlink(big)
    finally:
        shut(phone, client)


def test_fs_upload_phone_sha_mismatch():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        phone.upload_ack = {"ok": False, "error": "SHA mismatch"}
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b"y" * 5000)
            path = f.name
        fid = client.fs_upload(ch, path, "/srv/y.bin")
        wait_until(lambda: rec.done, "ACK fs_id fail")
        assert rec.done[0][0] == fid and not rec.done[0][2]
        assert rec.done[0][4] == "SHA mismatch"
        os.unlink(path)
    finally:
        shut(phone, client)


def test_fs_download_flow():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        payload = os.urandom(700_000)
        phone.dl_payload = payload
        phone.dl_sha = hashlib.sha256(payload).hexdigest()
        local = os.path.join(tempfile.mkdtemp(prefix="usbbridge-test-"), "down.bin")
        fid = client.fs_download(ch, "/srv/dl.bin", local)
        wait_until(lambda: rec.done, "download done")
        fid2, name, ok, d, saved = rec.done[0]
        assert (fid2, name, ok, d, saved) == (fid, "dl.bin", True, "down", local)
        with open(local, "rb") as f:
            assert f.read() == payload
        assert any(p[0] == fid and p[4] == "down" for p in rec.progress)
    finally:
        shut(phone, client)


def test_fs_download_sha_mismatch_deletes_partial():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        phone.dl_payload = os.urandom(300_000)
        phone.dl_sha = "0" * 64
        local = os.path.join(tempfile.mkdtemp(prefix="usbbridge-test-"), "down.bin")
        fid = client.fs_download(ch, "/srv/dl.bin", local)
        wait_until(lambda: rec.done, "mismatch result")
        assert rec.done[0][0] == fid and not rec.done[0][2]
        assert not os.path.exists(local), "partial file survived a SHA mismatch"
    finally:
        shut(phone, client)


def test_fs_download_meta_error_and_oversize():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        phone.dl_result = {"ok": False, "code": "FS_IO_FAILED",
                           "message": "cannot open remote file"}
        local = os.path.join(tempfile.mkdtemp(prefix="usbbridge-test-"), "down.bin")
        fid = client.fs_download(ch, "/srv/nope.bin", local)
        wait_until(lambda: rec.done, "FS_DL_RESULT error")
        assert rec.done[0][0] == fid and not rec.done[0][2]
        assert "cannot open remote file" in rec.done[0][4]
        assert not os.path.exists(local)

        # Oversize reported in FS_DL_RESULT: the PC aborts and fails locally.
        phone.dl_result = {"ok": True, "size": 3 * 1024 * 1024 * 1024}
        phone.dl_payload = b""
        fid2 = client.fs_download(ch, "/srv/huge.bin", local)
        wait_until(lambda: len(rec.done) == 2, "oversize download result")
        assert rec.done[1][0] == fid2 and not rec.done[1][2]
        assert "2GB" in rec.done[1][4]
        wait_until(lambda: fid2 in phone.aborts, "FS_ABORT for oversize download")
        assert not os.path.exists(local)
    finally:
        shut(phone, client)


def test_fs_download_abort():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        # Park the download stream before its first chunk: the phone's read loop stays
        # free to process FS_ABORT, so the abort is deterministic (like production, where
        # the stream runs on a dedicated thread).
        phone.dl_payload = os.urandom(32 * 1024 * 1024)
        phone.dl_sha = hashlib.sha256(phone.dl_payload).hexdigest()
        phone.dl_gate = threading.Event()
        local = os.path.join(tempfile.mkdtemp(prefix="usbbridge-test-"), "down.bin")
        fid = client.fs_download(ch, "/srv/dl.bin", local)
        wait_until(lambda: any(p[0] == fid for p in rec.progress), "FS_DL_RESULT")
        client.fs_abort(fid)
        wait_until(lambda: fid in phone.aborts, "FS_ABORT seen by phone")
        phone.dl_gate.set()
        wait_until(lambda: rec.done, "abort result")
        fid2, name, ok, d, tip = rec.done[0]
        assert (fid2, ok, d) == (fid, False, "down") and tip == "aborted"
        assert not os.path.exists(local), "aborted download left a partial file"
    finally:
        shut(phone, client)


def test_fs_close_fails_inflight_rows():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        # Park the stream so the download is still in flight when the hub tears down.
        phone.dl_payload = os.urandom(2 * 1024 * 1024)
        phone.dl_sha = hashlib.sha256(phone.dl_payload).hexdigest()
        phone.dl_gate = threading.Event()
        local = os.path.join(tempfile.mkdtemp(prefix="usbbridge-test-"), "down.bin")
        fid = client.fs_download(ch, "/srv/dl.bin", local)
        wait_until(lambda: any(p[0] == fid for p in rec.progress), "FS_DL_RESULT")
        phone.send(MsgType.FS_CLOSE, {"channel": ch, "code": 0, "reason": "stopped on phone"})
        wait_until(lambda: rec.done and rec.closed, "FS_CLOSE handling")
        assert rec.closed[0][0] == ch and rec.closed[0][2] == "stopped on phone"
        assert rec.done[0][0] == fid and not rec.done[0][2]
        assert rec.done[0][4] == "session closed"
        assert not os.path.exists(local), "closed download left a partial file"
        assert ch not in client._fs
        phone.dl_gate.set()          # release the parked stream; late chunks are ignored
    finally:
        shut(phone, client)


def test_fs_disconnect_cleanup():
    phone, client, rec = make_pair()
    try:
        ch = open_fs(phone, client, rec)
        # Park both transfers mid-flight so the disconnect tears them down, not EOF.
        phone.dl_payload = os.urandom(2 * 1024 * 1024)
        phone.dl_sha = hashlib.sha256(phone.dl_payload).hexdigest()
        phone.dl_gate = threading.Event()
        phone.upload_gate = threading.Event()
        phone.upload_gate_at = 3
        local = os.path.join(tempfile.mkdtemp(prefix="usbbridge-test-"), "down.bin")
        dfid = client.fs_download(ch, "/srv/dl.bin", local)
        wait_until(lambda: any(p[0] == dfid for p in rec.progress), "FS_DL_RESULT")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(os.urandom(32 * 1024 * 1024))
            path = f.name
        ufid = client.fs_upload(ch, path, "/srv/up.bin")
        wait_until(lambda: sum(1 for t, _ in phone.frames if t == MsgType.FS_CHUNK) >= 3,
                   "upload mid-flight")

        client.close(silent=True)                        # USB disconnect
        phone.dl_gate.set()                              # release parked stub threads
        phone.upload_gate.set()
        downs = [r for r in rec.done if r[0] == dfid]
        ups = [r for r in rec.done if r[0] == ufid]
        assert downs and downs[0][3] == "down" and not downs[0][2]
        assert downs[0][4] == "disconnected"
        assert ups and ups[0][3] == "up" and not ups[0][2]
        assert ups[0][4] == "disconnected"
        assert not os.path.exists(local), "disconnect left a partial download"
        assert client._fs_recv == {} and client._fs_up == {} and client._fs == {}
        os.unlink(path)
    finally:
        shut(phone, client)


def test_json_null_omission():
    frame = encode_frame(MsgType.FS_END, {"channel": 1, "id": 2, "ok": True, "sha256": None})
    assert b"null" not in frame, "None values must be omitted, never encoded as JSON null"
    pos = 0

    def read(n):
        nonlocal pos
        data = frame[pos:pos + n]
        assert len(data) == n, "short read"
        pos += n
        return data

    t, h, p = decode_frame(read, seal=None)
    assert (t, h, p) == (MsgType.FS_END, {"channel": 1, "id": 2, "ok": True}, b"")


if __name__ == "__main__":
    test_fs_open_list_op_close()
    test_fs_open_failure_codes()
    test_fs_upload_flow()
    test_fs_upload_cancel()
    test_fs_upload_rejected_and_oversize()
    test_fs_upload_phone_sha_mismatch()
    test_fs_download_flow()
    test_fs_download_sha_mismatch_deletes_partial()
    test_fs_download_meta_error_and_oversize()
    test_fs_download_abort()
    test_fs_close_fails_inflight_rows()
    test_fs_disconnect_cleanup()
    test_json_null_omission()
    print("ok")
