"""Persistent PC identity (P-256 keypair). Private key is never shown in the UI."""
from __future__ import annotations

import json
import os
import socket
from base64 import b64decode, b64encode
from pathlib import Path

from link_crypto import (
    generate_keypair,
    load_private,
    pc_id_of,
    private_bytes,
    public_bytes,
    short_id,
)


def _default_path() -> Path:
    root = Path(os.environ.get("USBBRIDGE_HOME") or (Path.home() / ".usbbridge"))
    return root / "identity.json"


class PcIdentity:
    def __init__(self, pc_name: str, priv, pub_der: bytes, path: Path):
        self.pc_name = pc_name
        self._priv = priv
        self.pub_der = pub_der
        self.pc_id = pc_id_of(pub_der)
        self.path = path

    @property
    def short_id(self) -> str:
        return short_id(self.pc_id)

    def pub_b64(self) -> str:
        return b64encode(self.pub_der).decode("ascii")

    def set_name(self, name: str):
        self.pc_name = (name or self.pc_name).strip() or self.pc_name
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "pc_name": self.pc_name,
            "private_key_b64": b64encode(private_bytes(self._priv)).decode("ascii"),
            "public_key_b64": b64encode(self.pub_der).decode("ascii"),
            "pc_id": self.pc_id,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @classmethod
    def load_or_create(cls, path: Path | None = None) -> "PcIdentity":
        path = path or _default_path()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            priv = load_private(b64decode(data["private_key_b64"]))
            pub_der = b64decode(data["public_key_b64"])
            name = data.get("pc_name") or socket.gethostname() or "PC"
            return cls(name, priv, pub_der, path)
        priv, pub_der = generate_keypair()
        name = socket.gethostname() or "PC"
        ident = cls(name, priv, pub_der, path)
        ident.save()
        return ident
