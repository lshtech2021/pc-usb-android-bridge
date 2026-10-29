"""Unlimited session-file scrollback for the PC terminal.

Every line that leaves the in-memory window is appended to a per-session file
under ~/.usbbridge/sessions/. The file is never trimmed; only a memory window
of recent / currently viewed lines is kept in RAM. Older pages are read back
by byte offset when the user scrolls up.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sessions_dir() -> Path:
    root = Path(os.environ.get("USBBRIDGE_HOME") or (Path.home() / ".usbbridge"))
    path = root / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_path(session_id: str) -> Path:
    safe = _SAFE.sub("_", (session_id or "default").strip())[:80] or "default"
    return sessions_dir() / f"{safe}.scrollback"


class ScrollbackCache:
    """Append-only session history on disk (oldest → newest), unlimited length."""

    def __init__(self, path: Path):
        self.path = path
        # Byte offset of each line start in the binary file.
        self._offsets: list[int] = []

    def __len__(self) -> int:
        return len(self._offsets)

    def reset(self):
        self._offsets = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")

    def extend(self, lines):
        if not lines:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as f:
            for line in lines:
                cleaned = str(line).replace("\r", "").replace("\n", "")
                self._offsets.append(f.tell())
                f.write(cleaned.encode("utf-8"))
                f.write(b"\n")

    def slice(self, start: int, end: int) -> list[str]:
        """Return lines[start:end] by seeking the session file."""
        n = len(self._offsets)
        start = max(0, min(start, n))
        end = max(start, min(end, n))
        if start == end:
            return []
        with self.path.open("rb") as f:
            f.seek(self._offsets[start])
            if end < n:
                data = f.read(self._offsets[end] - self._offsets[start])
            else:
                data = f.read()
        text = data.decode("utf-8", "replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines
