"""Custom application-layer protocol: frame encode/decode and message/error code constants.

Frame format (big-endian):
    magic(2) | type(1) | headerLen(2) | header(JSON,UTF-8) | payloadLen(4) | payload
Total length = 9 + headerLen + payloadLen; field order must match Android-side FrameIO.kt exactly.
"""
import json
import struct

MAGIC = b"\xAB\xCD"


class MsgType:
    HELLO, TEXT = 0x00, 0x01
    FILE_META, FILE_CHUNK, FILE_END = 0x02, 0x03, 0x04
    REMOTE_OPEN, REMOTE_DATA, REMOTE_OUTPUT, REMOTE_CLOSE = 0x10, 0x11, 0x12, 0x13
    CONN_LIST, CONN_LIST_RESULT = 0x14, 0x15
    CONN_ATTACH, CONN_DETACH = 0x16, 0x17
    ACK, ERROR = 0x20, 0x21
    PING, PONG = 0x30, 0x31


class ErrCode:
    BAD_TOKEN = "BAD_TOKEN"
    BAD_FRAME = "BAD_FRAME"
    UNKNOWN_HOST = "UNKNOWN_HOST"
    HOST_KEY_CHANGED = "HOST_KEY_CHANGED"
    AUTH_FAILED = "AUTH_FAILED"
    HOST_UNREACHABLE = "HOST_UNREACHABLE"
    CHANNEL_OPEN_FAILED = "CHANNEL_OPEN_FAILED"
    FILE_WRITE_FAILED = "FILE_WRITE_FAILED"
    CONN_NOT_RUNNING = "CONN_NOT_RUNNING"
    CONN_BUSY = "CONN_BUSY"
    CONN_NOT_FOUND = "CONN_NOT_FOUND"


def encode_frame(msg_type: int, header: dict, payload: bytes = b"") -> bytes:
    # Omit None values — Android JSONObject.optString turns JSON null into the string "null"
    clean = {k: v for k, v in header.items() if v is not None}
    h = json.dumps(clean, ensure_ascii=False).encode("utf-8")
    return (MAGIC + struct.pack(">BH", msg_type, len(h))
            + h + struct.pack(">I", len(payload)) + payload)


def decode_frame(read):
    """read(n) must return exactly n bytes, otherwise it raises an exception."""
    if read(2) != MAGIC:
        raise IOError("protocol magic mismatch")
    msg_type, hlen = struct.unpack(">BH", read(3))   # type 1B + headerLen 2B
    header = json.loads(read(hlen).decode("utf-8")) if hlen else {}
    (plen,) = struct.unpack(">I", read(4))
    return msg_type, header, (read(plen) if plen else b"")
