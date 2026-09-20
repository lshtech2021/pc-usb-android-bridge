"""自定义应用层协议：帧编解码与消息/错误码常量。

帧格式（大端）：
    magic(2) | type(1) | headerLen(2) | header(JSON,UTF-8) | payloadLen(4) | payload
总长度 = 9 + headerLen + payloadLen，字段顺序必须与 Android 端 FrameIO.kt 严格一致。
"""
import json
import struct

MAGIC = b"\xAB\xCD"


class MsgType:
    HELLO, TEXT = 0x00, 0x01
    FILE_META, FILE_CHUNK, FILE_END = 0x02, 0x03, 0x04
    REMOTE_OPEN, REMOTE_DATA, REMOTE_OUTPUT, REMOTE_CLOSE = 0x10, 0x11, 0x12, 0x13
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


def encode_frame(msg_type: int, header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, ensure_ascii=False).encode("utf-8")
    return (MAGIC + struct.pack(">BH", msg_type, len(h))
            + h + struct.pack(">I", len(payload)) + payload)


def decode_frame(read):
    """read(n) 必须返回恰好 n 字节，否则抛异常。"""
    if read(2) != MAGIC:
        raise IOError("协议 magic 错误")
    msg_type, hlen = struct.unpack(">BH", read(3))   # type 1B + headerLen 2B
    header = json.loads(read(hlen).decode("utf-8")) if hlen else {}
    (plen,) = struct.unpack(">I", read(4))
    return msg_type, header, (read(plen) if plen else b"")
