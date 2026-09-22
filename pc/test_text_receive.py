"""Regression: PC TEXT frames must decode; notification id must never collide with FGS id=1."""
import struct
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
from protocol import MsgType, encode_frame, decode_frame


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


def test_message_notification_ids_never_collide_with_fgs():
    """Mirrors BridgeService.nextMessageNotificationId: NOTIF_MSG_BASE + (seq & 0x0FFF)."""
    NOTIF_SERVICE = 1
    NOTIF_MSG_BASE = 1000
    for seq in range(0x2000):
        nid = NOTIF_MSG_BASE + (seq & 0x0FFF)
        assert nid != NOTIF_SERVICE, f"collision at seq={seq}"
        assert nid >= NOTIF_MSG_BASE


def test_old_id_scheme_could_collide():
    """Document why the old (uptime & 0xFFFF) scheme was unsafe."""
    NOTIF_SERVICE = 1
    # old: (System.currentTimeMillis() and 0xFFFF).toInt()
    assert (1 & 0xFFFF) == NOTIF_SERVICE


if __name__ == "__main__":
    test_text_frame_roundtrip()
    test_message_notification_ids_never_collide_with_fgs()
    test_old_id_scheme_could_collide()
    print("ok")
