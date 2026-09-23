"""P-256 ECDH + HKDF-SHA256 + AES-GCM for post-HELLO USB link sealing."""
from __future__ import annotations

import hashlib
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from protocol import MAGIC

CURVE = ec.SECP256R1()
INFO = b"usb-bridge-link-v1"
GCM_TAG_LEN = 16


def generate_keypair():
    priv = ec.generate_private_key(CURVE)
    return priv, public_bytes(priv.public_key())


def public_bytes(pub) -> bytes:
    return pub.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def private_bytes(priv) -> bytes:
    return priv.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_private(der: bytes):
    return serialization.load_der_private_key(der, password=None)


def load_public(der: bytes):
    return serialization.load_der_public_key(der)


def pc_id_of(pubkey_der: bytes) -> str:
    return hashlib.sha256(pubkey_der).hexdigest()


def short_id(pc_id: str) -> str:
    return pc_id[:16] if pc_id else "?"


def derive_session_key(shared: bytes, token: str, pc_id: str) -> bytes:
    salt = bytes.fromhex(pc_id) if len(pc_id) == 64 else pc_id.encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=INFO,
    ).derive(shared + token.encode("utf-8"))


def ecdh(priv, peer_pub_der: bytes) -> bytes:
    peer = load_public(peer_pub_der)
    shared = priv.exchange(ec.ECDH(), peer)
    # Left-pad / trim to 32 bytes so Android KeyAgreement and Python match
    if len(shared) >= 32:
        return shared[-32:]
    return shared.rjust(32, b"\x00")


class LinkSeal:
    """AES-GCM seal/unseal with per-direction counters (12-byte big-endian nonce)."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("session key must be 32 bytes")
        self._aes = AESGCM(key)
        self.send_ctr = 0
        self.recv_ctr = 0

    def seal_payload(self, msg_type: int, header_bytes: bytes, payload: bytes) -> bytes:
        inner = (struct.pack(">H", len(header_bytes)) + header_bytes
                 + struct.pack(">I", len(payload)) + payload)
        nonce = self.send_ctr.to_bytes(12, "big")
        self.send_ctr += 1
        aad = MAGIC + bytes([msg_type & 0xFF])
        return self._aes.encrypt(nonce, inner, aad)

    def unseal_payload(self, msg_type: int, ciphertext: bytes) -> tuple[bytes, bytes]:
        nonce = self.recv_ctr.to_bytes(12, "big")
        self.recv_ctr += 1
        aad = MAGIC + bytes([msg_type & 0xFF])
        inner = self._aes.decrypt(nonce, ciphertext, aad)
        if len(inner) < 6:
            raise IOError("sealed frame too short")
        hlen = struct.unpack(">H", inner[:2])[0]
        if len(inner) < 2 + hlen + 4:
            raise IOError("sealed frame truncated header")
        header_bytes = inner[2:2 + hlen]
        plen = struct.unpack(">I", inner[2 + hlen:6 + hlen])[0]
        if len(inner) < 6 + hlen + plen:
            raise IOError("sealed frame truncated payload")
        payload = inner[6 + hlen:6 + hlen + plen]
        return header_bytes, payload
