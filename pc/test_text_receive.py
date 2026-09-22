"""Regression tests for protocol + PRD discrepancy fixes."""
import json
import struct
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
from protocol import MsgType, encode_frame, decode_frame
from client import MAX_FILE_SIZE, PhoneClient


def test_text_frame_roundtrip():
    payload_header = {"id": 7, "text": "hello from PC 100\u20ac"}
    raw = encode_frame(MsgType.TEXT, payload_header)
    assert raw[:2] == b"\xAB\xCD"
    assert raw[2] == MsgType.TEXT

    buf = memoryview(raw)
    idx = [0]

    def read(n):
        start = idx[0]
        idx[0] = start + n
        chunk = buf[start:idx[0]].tobytes()
        if len(chunk) < n:
            raise AssertionError("short read")
        return chunk

    t, h, p = decode_frame(read)
    assert t == MsgType.TEXT
    assert h["id"] == 7
    assert h["text"] == "hello from PC 100\u20ac"
    assert p == b""


def test_encode_strips_none():
    raw = encode_frame(MsgType.REMOTE_OPEN, {
        "channel": 1, "kind": "ssh", "password": None, "host": "x",
    })
    # header starts at offset 5
    hlen = struct.unpack(">H", raw[3:5])[0]
    header = json.loads(raw[5:5 + hlen].decode("utf-8"))
    assert "password" not in header
    assert header["host"] == "x"
    assert "null" not in raw[5:5 + hlen].decode("utf-8")


def test_message_notification_ids_never_collide_with_fgs():
    """Mirrors BridgeService.nextMessageNotificationId: NOTIF_MSG_BASE + (seq & 0x0FFF)."""
    NOTIF_SERVICE = 1
    NOTIF_MSG_BASE = 1000
    for seq in range(0x2000):
        nid = NOTIF_MSG_BASE + (seq & 0x0FFF)
        assert nid != NOTIF_SERVICE, f"collision at seq={seq}"
        assert nid >= NOTIF_MSG_BASE


def test_max_file_size_is_2gb():
    assert MAX_FILE_SIZE == 2 * 1024 * 1024 * 1024


def test_upload_done_driven_by_file_ack():
    """PC must not mark upload Done until ACK{file_id}; FILE_WRITE_FAILED fails the row."""
    c = PhoneClient()
    results = []
    c.on_file_done = lambda *a: results.append(a)
    c._pending_up[42] = {"name": "a.bin"}
    c._on_frame(MsgType.ACK, {"file_id": 42, "ok": True, "path": "/data/a.bin"}, b"")
    assert results == [(42, "a.bin", True, "up", "/data/a.bin")]
    assert 42 not in c._pending_up

    c._pending_up[43] = {"name": "b.bin"}
    c._on_frame(MsgType.ACK, {"file_id": 43, "ok": False, "path": ""}, b"")
    assert results[-1][0] == 43 and results[-1][2] is False

    c._pending_up[44] = {"name": "c.bin"}
    c._on_frame(MsgType.ERROR, {"code": "FILE_WRITE_FAILED", "id": 44, "message": "disk"}, b"")
    assert results[-1] == (44, "c.bin", False, "up", "disk")


if __name__ == "__main__":
    test_text_frame_roundtrip()
    test_encode_strips_none()
    test_message_notification_ids_never_collide_with_fgs()
    test_max_file_size_is_2gb()
    test_upload_done_driven_by_file_ack()
    print("ok")
