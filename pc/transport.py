"""TCP transport layer: connect, send/receive frames, optional post-HELLO AES-GCM seal."""
import socket
import threading

from protocol import encode_frame, decode_frame


class Transport:
    def __init__(self, host, port, on_frame=None, on_disconnect=None):
        self.host, self.port = host, port
        self.on_frame, self.on_disconnect = on_frame, on_disconnect
        self.sock, self._running = None, False
        self._send_lock = threading.Lock()
        self._seal = None

    @property
    def alive(self):
        return self._running

    @property
    def sealed(self) -> bool:
        """True once the post-HELLO seal is active. Before that the link is cleartext and
        the phone is still unauthenticated: anything but a HELLO reads as a violation."""
        return self._seal is not None

    def start(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.settimeout(None)
        self._running = True
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def enable_seal(self, seal):
        """Enable AES-GCM for all subsequent frames (call from ACK handler before next decode)."""
        self._seal = seal

    def send(self, msg_type, header, payload=b""):
        # Sealing must happen under the send lock: the AEAD nonce is a counter, so the order
        # frames are encrypted in has to be the order they reach the wire. Sealing outside the
        # lock lets two threads swap nonces, which desyncs the peer's counter on the first
        # frame and kills the session (PROXY_DATA is sent from one thread per tunnel).
        with self._send_lock:
            data = encode_frame(msg_type, header, payload, seal=self._seal)
            self.sock.sendall(data)

    def close(self):
        self._running = False
        try:
            self.sock.close()
        except Exception:
            pass

    def _recv_loop(self):
        f = self.sock.makefile("rb")

        def read(n):
            data = f.read(n)
            if len(data) < n:
                raise ConnectionError("peer closed")
            return data

        try:
            while self._running:
                t, h, p = decode_frame(read, seal=self._seal)
                self.on_frame and self.on_frame(t, h, p)
        except Exception:
            pass
        finally:
            self.close()
            self.on_disconnect and self.on_disconnect()
