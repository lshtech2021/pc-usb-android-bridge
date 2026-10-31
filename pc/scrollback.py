"""Session scrollback storage for the PC terminal.

Two implementations live here:

* ``ScrollbackCache`` - the original plain-text cache used by the interim
  QPlainTextEdit widget while it is still in service.
* ``ScrollbackStore`` - the attributed, versioned JSONL store for the native
  cell view. Each line keeps its text runs plus foreground/background/attribute
  flags, so history renders in the same colours as the live screen.

Both are per-session files under ``~/.usbbridge/sessions/`` and are reset on
attach, matching the existing session lifecycle.
"""
from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from pathlib import Path

from terminal_model import (
    FLAG_BLINK,
    FLAG_BOLD,
    FLAG_ITALIC,
    FLAG_REVERSE,
    FLAG_STRIKE,
    FLAG_UNDERLINE,
    Run,
    Style,
)


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
    """Append-only plain-text history on disk (oldest → newest), unlimited length.

    Kept for the interim QPlainTextEdit widget; the native view uses
    :class:`ScrollbackStore`.
    """

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


class ScrollbackStore:
    """Attributed append-only scrollback with lazy, cached slicing.

    File layout: a version header line followed by one JSON object per line::

        {"v":1,"cols":120}
        {"r":[["user@host","green","default",0,9], ...]}

    Each run is ``[text, fg, bg, flags, cells]``. The byte-offset table means
    scrolling only reads the visible lines, not the whole history.
    """

    VERSION = 1
    CACHE_LIMIT = 1024

    def __init__(self, path: Path, columns: int = 120):
        self.path = path
        self.columns = columns
        self._offsets: list[int] = []
        self._cache: OrderedDict[int, list[Run]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._offsets)

    def reset(self, columns: int | None = None):
        if columns is not None:
            self.columns = columns
        self._offsets = []
        self._cache.clear()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = json.dumps(
            {"v": self.VERSION, "cols": self.columns},
            separators=(",", ":"),
        )
        self.path.write_text(header + "\n", encoding="utf-8")

    def append(self, runs: list[Run]):
        """Append one line represented by its styled runs."""
        self.append_many([runs])

    def append_many(self, lines: list[list[Run]]):
        """Append several attributed lines with one file open."""
        lines = [runs for runs in lines if runs]
        if not lines:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as f:
            for runs in lines:
                payload = [
                    [run.text, run.style.fg, run.style.bg,
                     run.style.flags(), run.cells]
                    for run in runs
                ]
                line = json.dumps(
                    {"r": payload}, ensure_ascii=False, separators=(",", ":")
                )
                self._offsets.append(f.tell())
                f.write(line.encode("utf-8"))
                f.write(b"\n")

    def slice(self, start: int, end: int) -> list[list[Run]]:
        """Return attributed lines ``[start:end]``."""
        n = len(self._offsets)
        start = max(0, min(start, n))
        end = max(start, min(end, n))
        if start == end:
            return []
        result: list[list[Run]] = []
        with self.path.open("rb") as f:
            for index in range(start, end):
                cached = self._cache.get(index)
                if cached is not None:
                    self._cache.move_to_end(index)
                    result.append(cached)
                    continue
                f.seek(self._offsets[index])
                if index + 1 < n:
                    raw = f.read(self._offsets[index + 1] - self._offsets[index])
                else:
                    raw = f.readline()
                line = self._parse(raw)
                self._remember(index, line)
                result.append(line)
        return result

    def _remember(self, index: int, runs: list[Run]):
        self._cache[index] = runs
        self._cache.move_to_end(index)
        while len(self._cache) > self.CACHE_LIMIT:
            self._cache.popitem(last=False)

    @staticmethod
    def _parse(raw: bytes) -> list[Run]:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        runs: list[Run] = []
        for item in obj.get("r", []):
            try:
                text, fg, bg, flags, cells = item
            except (TypeError, ValueError):
                continue
            runs.append(
                Run(
                    text=str(text),
                    cells=int(cells),
                    style=Style(
                        fg=str(fg),
                        bg=str(bg),
                        bold=bool(flags & FLAG_BOLD),
                        italic=bool(flags & FLAG_ITALIC),
                        underline=bool(flags & FLAG_UNDERLINE),
                        strike=bool(flags & FLAG_STRIKE),
                        reverse=bool(flags & FLAG_REVERSE),
                        blink=bool(flags & FLAG_BLINK),
                    ),
                )
            )
        return runs
