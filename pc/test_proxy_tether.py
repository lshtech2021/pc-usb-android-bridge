"""End-to-end regression test for reverse tethering (SOCKS5 over the sealed bridge).

A stub phone speaks the real protocol on loopback; the real PhoneClient and
SocksProxyServer sit on the other side, driven by several concurrent SOCKS5 clients —
what a browser does the moment it is pointed at 127.0.0.1:<port>.

Regression: Transport.send() sealed frames *outside* the send lock, but the AES-GCM nonce
is a counter, so two tunnels sealing at once could swap nonces. The phone then failed to
unseal, its read loop threw, the session closed and the PC showed "[Connection closed]"
(Disconnected) within seconds of a browser starting to use the proxy.
"""
import os
import queue
import socket
import struct
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
from proxy import SocksProxyServer  # noqa: E402

TOKEN = "deadbeef"
CLIENTS = 6
ROUNDS = 8
PAYLOAD = 32 * 1024
DEADLINE = 30.0


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


class EchoServer:
    """Stands in for a website: echoes bytes back, then answers EOF with a short trailer."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self.handled = 0
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.handled += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _serve(conn):
        with conn:
            while True:
                chunk = conn.recv(64 * 1024)
                if not chunk:
                    break
                conn.sendall(chunk)
            conn.sendall(b"<eof>")      # exercises the half-close path back to the client


class StubPhone:
    """Minimal Android stand-in: HELLO/ACK handshake, then relays PROXY_* over real sockets.

    Frames are sealed and written under one lock, mirroring the fixed SessionHandler.send.
    """

    def __init__(self, ack_gate=None):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.ack_gate = ack_gate       # holds the ACK, like the phone's approve-PC prompt
        self.hello_seen = threading.Event()
        self.errors = []
        self.conn = None
        self.seal = None
        self.send_lock = threading.Lock()
        self.streams = {}            # sid -> Stream
        self.opened = 0
        self.proxy_frames = 0
        self.open_frames = 0
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
            t, h, _ = decode_frame(read, seal=None)      # HELLO is cleartext
            if t != MsgType.HELLO or h.get("token") != TOKEN:
                raise AssertionError(f"bad HELLO: {t} {h}")
            self.hello_seen.set()
            if self.ack_gate is not None:
                self.ack_gate.wait(DEADLINE)             # PC approval blocks the read loop
            priv, pub_der = generate_keypair()
            shared = ecdh(priv, b64decode(h["eph_pub"]))
            key = derive_session_key(shared, TOKEN, h["pc_id"])
            self.send(MsgType.ACK, {"ok": True, "device": "StubPhone",
                                    "eph_pub": b64encode(pub_der).decode("ascii")})
            self.seal = LinkSeal(key)
            while True:
                t, h, p = decode_frame(read, seal=self.seal)
                self._handle(t, h, p)
        except Exception as e:
            self.errors.append(f"{type(e).__name__}: {e}")

    def _handle(self, mtype, h, payload):
        if mtype == MsgType.PING:
            self.send(MsgType.PONG, {})
        elif mtype == MsgType.PROXY_OPEN:
            self.open_frames += 1
            sid, host, port = h["sid"], h["host"], h["port"]
            st = self.streams[sid] = Stream(self, sid)
            st.start(host, port)          # connect off the read loop, like ProxySession.start
        elif mtype == MsgType.PROXY_DATA:
            self.proxy_frames += 1
            st = self.streams.get(h["sid"])
            if st:
                st.to_socket.put(payload)
        elif mtype == MsgType.PROXY_CLOSE:
            st = self.streams.get(h["sid"])
            if st:
                if h.get("dir") == "c2s":
                    st.to_socket.put(None)               # flush, then shutdown(SHUT_WR)
                else:
                    self.streams.pop(h["sid"], None)
                    st.close()

    @property
    def open_streams(self):
        return len(self.streams)


class Stream:
    """One tunnel on the stub phone: reader (socket -> PC) plus a writer queue."""

    def __init__(self, phone, sid):
        self.phone, self.sid = phone, sid
        self.sock = None
        self.to_socket = queue.Queue()
        self.reader = None

    def start(self, host, port):
        threading.Thread(target=self._connect, args=(host, port), daemon=True).start()

    def _connect(self, host, port):
        try:
            sock = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            self.phone.send(MsgType.PROXY_OPENED, {"sid": self.sid, "ok": False,
                                                   "code": "PROXY_REFUSED",
                                                   "message": str(e)})
            self.phone.streams.pop(self.sid, None)
            return
        self.sock = sock
        self.phone.opened += 1
        self.phone.send(MsgType.PROXY_OPENED, {"sid": self.sid, "ok": True})
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def _read_loop(self):
        try:
            while True:
                chunk = self.sock.recv(64 * 1024)
                if not chunk:
                    break
                self.phone.send(MsgType.PROXY_DATA, {"sid": self.sid}, chunk)
            self.phone.send(MsgType.PROXY_CLOSE, {"sid": self.sid, "dir": "s2c",
                                                  "code": 0, "reason": "remote EOF"})
        except OSError:
            pass

    def _write_loop(self):
        try:
            while True:
                item = self.to_socket.get()
                if item is None:
                    self.sock.shutdown(socket.SHUT_WR)   # half-close, mirroring ProxySession
                    continue
                self.sock.sendall(item)
        except OSError:
            pass

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


class SocksRefused(Exception):
    def __init__(self, rep):
        super().__init__(f"CONNECT refused: rep=0x{rep:02x}")
        self.rep = rep


def socks_connect(proxy_port, host, port):
    """SOCKS5 CONNECT with a domain name, like a browser sends."""
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    s.settimeout(DEADLINE)
    s.sendall(b"\x05\x01\x00")
    assert recv_exact(s, 2) == b"\x05\x00", "no-auth method rejected"
    hb = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    rep = recv_exact(s, 10)
    if rep[1] != 0x00:
        s.close()
        raise SocksRefused(rep[1])
    return s


def browse(proxy_port, echo_port, idx, failures):
    """One browser tab: several request/response rounds, then a half-closed request."""
    stage = "socks handshake"
    started = time.monotonic()
    try:
        s = socks_connect(proxy_port, "localhost", echo_port)
        blob = bytes((idx * 31 + i) & 0xFF for i in range(PAYLOAD))
        for r in range(ROUNDS):
            stage = f"round {r}"
            s.sendall(blob)
            got = recv_exact(s, len(blob))
            assert got == blob, f"echo mismatch at {sum(1 for a, b in zip(got, blob) if a != b)} bytes"
        stage = "half-close"
        s.shutdown(socket.SHUT_WR)                     # request body finished
        assert recv_exact(s, 5) == b"<eof>", "no trailer after half-close"
        s.close()
    except Exception as e:
        failures.append(f"client {idx} at {stage} after {time.monotonic() - started:.1f}s: "
                        f"{type(e).__name__}: {e}")


def test_browser_load_keeps_the_bridge_up():
    echo = EchoServer()
    phone = StubPhone()
    client = PhoneClient()
    notices = []
    client.on_status = notices.append
    proxy = SocksProxyServer(lambda: client, port=0, on_status=notices.append)
    deadline = time.monotonic() + DEADLINE
    try:
        client.connect("127.0.0.1", phone.port, TOKEN)
        for _ in range(200):                            # wait for ACK + seal
            if client.tp and client.tp.alive and phone.seal is not None:
                break
            time.sleep(0.02)
        assert client.tp and client.tp.alive, f"handshake failed: {notices}"
        assert phone.opened == 0

        proxy.start()
        proxy_port = proxy._srv.getsockname()[1]

        failures = []
        threads = [threading.Thread(target=browse,
                                    args=(proxy_port, echo.port, i, failures))
                   for i in range(CLIENTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(DEADLINE)
        assert not any(t.is_alive() for t in threads), "a browsing client hung"

        # The whole point: after the load the bridge is still sealed and up.
        diagnosis = []
        if phone.errors:
            diagnosis.append(f"phone read loop died: {phone.errors}")
        if not (client.tp and client.tp.alive):
            diagnosis.append(f"bridge dropped: {notices}")
        if [n for n in notices if "closed" in n.lower()]:
            diagnosis.append(f"link closed under the browser: {notices}")
        if failures:
            diagnosis.append(f"clients: {failures}")
        assert not diagnosis, " | ".join(diagnosis)
        assert phone.opened == CLIENTS, f"opened {phone.opened} of {CLIENTS} tunnels"
        assert phone.proxy_frames >= CLIENTS * ROUNDS, phone.proxy_frames
        assert echo.handled == CLIENTS, echo.handled
        assert proxy.running
        assert time.monotonic() < deadline
    finally:
        proxy.stop()
        client.close(silent=True)
        phone.srv.close()
        echo.srv.close()


def test_proxy_traffic_before_the_seal_does_not_kill_the_handshake():
    """A browser already pointed at the proxy must not poison the handshake.

    Until the seal exists there is no way to hand a PROXY_OPEN to the phone: it arrives as
    cleartext, which the phone treats as a protocol violation and closes the session over.
    The user sees the connection drop mid-handshake and the UI blames the token ("wrong
    token") on a bridge that was never wrong.
    """
    echo = EchoServer()
    gate = threading.Event()
    phone = StubPhone(ack_gate=gate)          # hold the ACK: the "approve this PC?" prompt
    client = PhoneClient()
    notices = []
    client.on_status = notices.append
    proxy = SocksProxyServer(lambda: client, port=0, on_status=notices.append)
    try:
        client.connect("127.0.0.1", phone.port, TOKEN)
        assert phone.hello_seen.wait(DEADLINE), "HELLO never reached the phone"
        assert phone.seal is None, "handshake finished before the test could race it"

        proxy.start()
        proxy_port = proxy._srv.getsockname()[1]

        try:
            socks_connect(proxy_port, "localhost", echo.port).close()
            raise AssertionError("proxy carried traffic while the link was still cleartext")
        except SocksRefused as e:
            assert e.rep == 0x01, f"expected a clean refusal, got rep=0x{e.rep:02x}"

        # Nothing may have been pushed at the phone, and the handshake must still be alive.
        assert phone.open_frames == 0, "a PROXY_OPEN was sent before the link was sealed"
        assert client.tp and client.tp.alive, f"handshake killed: {notices}"
        assert not notices, notices

        gate.set()                                        # finish the handshake
        for _ in range(200):
            if phone.seal is not None and client.tp and client.tp.alive:
                break
            time.sleep(0.02)
        assert phone.seal is not None, f"no seal after approval: {notices}"
        # Whatever was queued behind the prompt is read here: a cleartext tunnel frame
        # makes the phone's read loop die on the first decode.
        time.sleep(0.3)
        assert not phone.errors, f"phone read loop died: {phone.errors}"
        assert client.tp and client.tp.alive, f"bridge dropped: {notices}"
        assert not notices, notices

        # The tether works normally once the link is up.
        s = socks_connect(proxy_port, "localhost", echo.port)
        blob = b"after the seal" * 4096
        s.sendall(blob)
        assert recv_exact(s, len(blob)) == blob, "echo mismatch after the handshake"
        s.shutdown(socket.SHUT_WR)
        assert recv_exact(s, 5) == b"<eof>", "no trailer after half-close"
        s.close()
        assert phone.open_frames == 1, f"{phone.open_frames} tunnels opened (expected 1)"
        assert not phone.errors, phone.errors
        assert not notices, notices
    finally:
        gate.set()
        proxy.stop()
        client.close(silent=True)
        phone.srv.close()
        echo.srv.close()


if __name__ == "__main__":
    test_browser_load_keeps_the_bridge_up()
    test_proxy_traffic_before_the_seal_does_not_kill_the_handshake()
    print("ok")
