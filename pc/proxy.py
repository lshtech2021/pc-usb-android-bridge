"""Local SOCKS5 server that tunnels TCP CONNECT streams over the encrypted USB bridge.

The PC never dials out itself: each SOCKS client connection becomes a PROXY_OPEN, and the
phone opens the outbound socket on its own network (mobile data / Wi-Fi). Domain-name ATYP
is forwarded verbatim, so DNS resolves on the phone.

Stdlib only. The accept thread spawns two daemon threads per stream: a handler (client ->
phone, also runs the SOCKS handshake) and a writer (phone -> client). Neither the bridge
receive thread nor the accept thread ever blocks on socket I/O.
"""
import os
import socket
import struct
import threading
import time

from protocol import ErrCode

MAX_STREAMS = 128
PROXY_CHUNK = 64 * 1024          # smaller than the 256 KiB file chunk to bound per-stream memory
BUF_LIMIT = 1024 * 1024          # per-stream downstream buffer
HANDSHAKE_TIMEOUT = 15
OPEN_TIMEOUT = 12
IDLE_TIMEOUT = 600               # only while a stream is half-closed, never on a live tunnel

DEFAULT_PORT = 1080

# SOCKS5 reply codes
REP_OK = 0x00
REP_GENERAL = 0x01
REP_HOST_UNREACHABLE = 0x04
REP_REFUSED = 0x05
REP_CMD_UNSUPPORTED = 0x07
REP_ATYP_UNSUPPORTED = 0x08


class SocksError(Exception):
    """Handshake-level failure carrying the SOCKS REP byte to send back."""

    def __init__(self, rep: int, message: str = ""):
        super().__init__(message)
        self.rep = rep


# Control sentinels in the downstream queue. They must bypass byte-size accounting.
_EOF = object()      # remote write side finished -> shutdown the client's write side
_FINISH = object()   # flush whatever is queued, then tear the stream down
_CLOSED = object()   # hard close (queue was closed under the writer)


class ByteQueue:
    """Bounded byte queue with a never-blocking ``offer``.

    ``offer`` runs on the bridge receive thread, so it must not block: overflow drops the
    whole stream (the caller sends PROXY_CLOSE) instead of stalling the shared recv loop.
    """

    def __init__(self, limit: int = BUF_LIMIT):
        self._limit = limit
        self._buf = []
        self._size = 0
        self._closed = False
        self._cv = threading.Condition()

    def offer(self, data: bytes) -> bool:
        with self._cv:
            if self._closed or self._size + len(data) > self._limit:
                return False
            self._buf.append(data)
            self._size += len(data)
            self._cv.notify()
            return True

    def offer_control(self, item) -> None:
        """Sentinel path: not counted against the byte limit."""
        with self._cv:
            if self._closed:
                return
            self._buf.append(item)
            self._cv.notify()

    def take(self, timeout=None):
        """Return the next item, ``_CLOSED`` once drained after close, or None on timeout."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while not self._buf:
                if self._closed:
                    return _CLOSED
                if end is None:
                    self._cv.wait()
                else:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cv.wait(remaining)
            item = self._buf.pop(0)
            if isinstance(item, (bytes, bytearray)):
                self._size -= len(item)
            return item

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class ProxyStream:
    """One SOCKS client connection relayed over the bridge (half-close aware)."""

    def __init__(self, server, sock: socket.socket):
        self._server = server
        self._sock = sock
        self._client = None
        self._sid = None
        self._queue = ByteQueue()
        self._lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._done = False
        self._finish_queued = False
        self._eof_c2s = False       # client -> phone finished (client read side closed)
        self._eof_s2c = False       # phone -> client finished
        self._code = 0
        self._reason = ""
        self._opened_evt = threading.Event()
        self._open_ok = False
        self._open_rep = REP_GENERAL
        self._serving = False       # writer started: it owns teardown from here on

    # ---- bridge-thread callbacks (registered via client.alloc_proxy) ----
    def on_opened(self, sid, ok, code, msg):
        self._open_ok = bool(ok)
        self._open_rep = REP_OK if ok else _rep_for(code)
        self._opened_evt.set()

    def on_data(self, sid, data):
        if not self._queue.offer(data):
            # Never block the bridge recv thread: drop this one stream instead.
            self._fail_both(0, "buffer overflow")

    def on_close(self, sid, dir, code, reason):
        self._code, self._reason = code, reason
        if dir == "s2c":
            with self._lock:
                if self._eof_s2c:
                    return
                self._eof_s2c = True
            self._queue.offer_control(_EOF)          # flush, then shutdown client write
            self._maybe_finish()
        else:                                        # "c2s" or "both"
            with self._lock:
                self._eof_c2s = True
                self._eof_s2c = True
            _shutdown_read(self._sock)               # unblock the handler's recv
            self._opened_evt.set()                   # a close during "opening" must not wait out OPEN_TIMEOUT
            self._queue.offer_control(_EOF)
            self._queue.offer_control(_FINISH)

    # ---- handler thread ----
    def run(self):
        self._client = self._server.get_client()
        try:
            self._sock.settimeout(HANDSHAKE_TIMEOUT)
            host, port = self._handshake()
            self._sock.settimeout(None)
            if not (self._client.tp and self._client.tp.alive):
                self._reply(REP_GENERAL)
                return
            self._sid = self._client.alloc_proxy({
                "opened": self.on_opened, "data": self.on_data, "close": self.on_close})
            try:
                self._client.proxy_send_open(self._sid, host, port)
            except Exception:
                self._reply(REP_GENERAL)             # link dropped mid-open
                return
            if not self._opened_evt.wait(OPEN_TIMEOUT):
                self._reply(REP_GENERAL)
                return
            if not self._open_ok:
                self._reply(self._open_rep)
                return
            self._reply(REP_OK)
            self._serving = True                     # the writer now owns teardown
            threading.Thread(target=self._writer_loop, name="socks-writer",
                             daemon=True).start()
            self._serve_c2s()
        except SocksError as e:
            self._reply(e.rep)
        except Exception:
            pass
        finally:
            # Once serving, a client EOF is a half-close: the writer keeps the read side
            # alive and tears down later. Only pre-serving failures close the stream here.
            if not self._serving:
                self._fail_both(self._code, self._reason or "closed")

    def run_refused(self):
        """Stream cap hit: finish the handshake just far enough to answer REP 0x01."""
        try:
            self._sock.settimeout(HANDSHAKE_TIMEOUT)
            self._handshake()
            self._reply(REP_GENERAL)
        except Exception:
            pass
        finally:
            try:
                self._sock.close()
            except Exception:
                pass
            self._server.unregister(self)

    def _serve_c2s(self):
        try:
            while True:
                data = self._sock.recv(PROXY_CHUNK)
                if not data:
                    break
                self._client.proxy_data(self._sid, data)
        except OSError:
            pass
        finally:
            self._on_local_eof()

    def _writer_loop(self):
        try:
            while True:
                item = self._queue.take(IDLE_TIMEOUT)
                if item is _FINISH or item is _CLOSED:
                    break
                if item is _EOF:
                    _shutdown_write(self._sock)
                    continue
                if item is None:                     # idle timeout
                    if self._eof_c2s or self._eof_s2c:
                        break                        # half-closed and silent -> reap
                    continue                         # healthy fully-open idle tunnel: keep waiting
                self._sock.sendall(item)
        except OSError:
            pass
        finally:
            self._teardown()

    def _on_local_eof(self):
        with self._lock:
            if self._eof_c2s:
                return
            self._eof_c2s = True
        self._client.proxy_close(self._sid, "c2s", 0, "client closed")
        self._maybe_finish()

    def _maybe_finish(self):
        with self._lock:
            if not (self._eof_c2s and self._eof_s2c) or self._finish_queued:
                return
            self._finish_queued = True
        self._queue.offer_control(_FINISH)

    def _fail_both(self, code=0, reason=""):
        self._code, self._reason = code, reason
        self._teardown()

    def _teardown(self):
        with self._finish_lock:
            if self._done:
                return
            self._done = True
        if self._sid is not None:
            self._client.proxy_close(self._sid, "both", self._code, self._reason)
        self._queue.close()
        try:
            self._sock.close()
        except Exception:
            pass
        self._server.unregister(self)

    def abort(self):
        self._fail_both(0, "proxy stopped")

    # ---- SOCKS5 wire handling ----
    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise SocksError(REP_GENERAL, "client closed during handshake")
            buf += chunk
        return buf

    def _handshake(self):
        ver, nmethods = self._recv_exact(2)
        if ver != 5:
            raise SocksError(REP_GENERAL, "not SOCKS5")
        methods = self._recv_exact(nmethods)
        if 0x00 not in methods:
            self._sock.sendall(b"\x05\xFF")
            raise SocksError(REP_GENERAL, "no acceptable auth method")
        self._sock.sendall(b"\x05\x00")              # no auth

        ver, cmd, _rsv, atyp = self._recv_exact(4)
        if ver != 5:
            raise SocksError(REP_GENERAL, "bad request version")
        if cmd != 0x01:
            raise SocksError(REP_CMD_UNSUPPORTED, "only CONNECT is supported")
        if atyp == 0x01:
            host = ".".join(str(b) for b in self._recv_exact(4))
        elif atyp == 0x04:
            raw = self._recv_exact(16)
            host = ":".join("%x" % v for v in struct.unpack(">8H", raw))
        elif atyp == 0x03:
            length = self._recv_exact(1)[0]
            host = self._recv_exact(length).decode("utf-8", "replace")
        else:
            raise SocksError(REP_ATYP_UNSUPPORTED, "unsupported address type")
        (port,) = struct.unpack(">H", self._recv_exact(2))
        return host, port

    def _reply(self, rep: int):
        try:
            # BND.ADDR/BND.PORT are unused; 0.0.0.0:0 is conventional.
            self._sock.sendall(b"\x05" + bytes([rep]) + b"\x00\x01" + b"\x00" * 6)
        except Exception:
            pass


def _rep_for(code: str) -> int:
    if code == ErrCode.PROXY_REFUSED:
        return REP_REFUSED
    if code in (ErrCode.PROXY_HOST_UNREACHABLE, ErrCode.PROXY_TIMEOUT):
        return REP_HOST_UNREACHABLE
    return REP_GENERAL                                # BAD_REQUEST / LIMIT / anything else


def _shutdown_read(sock):
    try:
        sock.shutdown(socket.SHUT_RD)
    except OSError:
        pass


def _shutdown_write(sock):
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


class SocksProxyServer:
    """SOCKS5 listener on loopback; refuses traffic while the bridge is down."""

    def __init__(self, get_client, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 on_status=None, on_state=None):
        self._get_client = get_client
        self.host = host
        self.port = port
        self._on_status = on_status or (lambda _s: None)
        self._on_state = on_state or (lambda _s, _d: None)
        self._lock = threading.Lock()
        self._srv = None
        self._running = False
        self._live = set()          # all accepted ProxyStreams (incl. handshaking ones)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self):
        with self._lock:
            if self._running:
                return
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # Windows: no SO_REUSEADDR, so a second instance fails loudly with "port in use".
                # POSIX: reuse is harmless and avoids TIME_WAIT rebind pain.
                if os.name != "nt":
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((self.host, self.port))
                srv.listen(64)
            except Exception:
                srv.close()
                raise
            srv.settimeout(1.0)     # poll _running so stop() is prompt on Windows too
            self._srv = srv
            self._running = True
            threading.Thread(
                target=self._accept_loop, name="socks-accept", daemon=True).start()
        self._on_state("listening", self.address)

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            srv, self._srv = self._srv, None
            streams = list(self._live)
        try:
            srv.close()
        except Exception:
            pass
        for st in streams:
            st.abort()
        self._on_state("off", "")

    def unregister(self, stream: "ProxyStream"):
        with self._lock:
            self._live.discard(stream)

    def get_client(self):
        return self._get_client()

    def _accept_loop(self):
        while self._running:
            srv = self._srv
            if srv is None:
                break
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            stream = ProxyStream(self, conn)
            with self._lock:
                over = len(self._live) >= MAX_STREAMS
                if not over:
                    self._live.add(stream)
            if over:
                threading.Thread(target=stream.run_refused,
                                 name="socks-refuse", daemon=True).start()
                self._on_status("[Tether] stream limit reached; refused a client")
                continue
            threading.Thread(target=stream.run, name="socks-handler", daemon=True).start()
