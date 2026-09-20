"""TCP 传输层：连接、收发帧、断线回调。"""
import socket
import threading

from protocol import encode_frame, decode_frame


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
        try:
            self.sock.close()
        except Exception:
            pass

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
