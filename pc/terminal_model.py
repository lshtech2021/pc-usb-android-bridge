"""pyte-backed terminal model shared by the native terminal view.

This module is deliberately free of Qt imports so the alt-screen, mode and
attribute logic can be unit-tested headlessly. It provides:

* ``DualScreen`` - normal-history screen + alternate screen with mode-aware
  switching for ``?1049`` / ``?47`` / ``?1047``.
* ``private_mode`` - read a DEC private mode bit from ``screen.mode``.
* ``row_runs`` - turn a pyte row into text runs with per-cell style.
* ``normalize_color`` - map pyte colour names/hex to the app's theme keys.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable

import pyte
import pyte.modes as mo
from pyte.screens import Margins
from pyte.streams import Stream

# pyte keeps a bounded history window; older lines are spilled to the
# attributed scrollback store by the widget.
HOT_HISTORY = 2000

# Style flag bits used by the scrollback file format.
FLAG_BOLD = 1
FLAG_ITALIC = 2
FLAG_UNDERLINE = 4
FLAG_STRIKE = 8
FLAG_REVERSE = 16
FLAG_BLINK = 32

# pyte's ANSI names differ from the usual terminal names in two places.
_COLOR_ALIASES = {
    "brown": "yellow",
    "brightbrown": "brightyellow",
    "bfightmagenta": "brightmagenta",
}
_HEX = set("0123456789abcdefABCDEF")


def private_mode(screen, number: int) -> bool:
    """Return True when DEC private mode ``number`` is set on ``screen``.

    pyte stores private mode ``n`` as ``n << 5`` in ``Screen.mode``.
    """
    return (number << 5) in screen.mode


def normalize_color(value: str) -> str:
    """Normalise a pyte fg/bg value for theme lookup or the scrollback file.

    ``default`` stays ``default``; ``brown`` becomes ``yellow`` (pyte's name
    for ANSI yellow); ``bfightmagenta`` is pyte's typo for bright magenta;
    six-digit 256/truecolor hex strings gain a leading ``#``.
    """
    if not value or value == "default":
        return "default"
    value = _COLOR_ALIASES.get(value, value)
    if len(value) == 6 and all(ch in _HEX for ch in value):
        return "#" + value
    return value


@dataclass(frozen=True)
class Style:
    """Resolved cell attributes (colours are still pyte values)."""

    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    reverse: bool = False
    blink: bool = False

    def flags(self) -> int:
        return (
            (FLAG_BOLD if self.bold else 0)
            | (FLAG_ITALIC if self.italic else 0)
            | (FLAG_UNDERLINE if self.underline else 0)
            | (FLAG_STRIKE if self.strike else 0)
            | (FLAG_REVERSE if self.reverse else 0)
            | (FLAG_BLINK if self.blink else 0)
        )


@dataclass(frozen=True)
class Run:
    """A run of consecutive cells with the same style.

    ``cells`` is the number of terminal cells covered by the run (wide
    characters and their stub cells are counted individually). ``text`` never
    contains the empty stub data of a wide character.
    """

    text: str
    cells: int
    style: Style


EMPTY_STYLE = Style()


def row_runs(row, columns: int) -> list[Run]:
    """Convert one pyte row (a mapping column -> Char) to styled runs.

    pyte stores a wide character in one cell and an empty ``data`` stub in the
    next. Both cells are counted, but the stub contributes no text. Trailing
    default-style spaces are omitted: the view fills the default background
    and copy trims line ends anyway, so they would only bloat the scrollback.
    """
    # Trim the default-space tail before building runs. Most terminal lines
    # are short relative to the 120-column grid.
    last = columns - 1
    while last >= 0:
        ch = row[last]
        if (ch.data == " " and ch.fg == "default" and ch.bg == "default"
                and not (ch.bold or ch.italics or ch.underscore
                         or ch.strikethrough or ch.reverse or ch.blink)):
            last -= 1
        else:
            break
    if last < 0:
        return []

    runs: list[Run] = []
    text: list[str] = []
    cells = 0
    style_key = None
    current_style = EMPTY_STYLE

    def flush() -> None:
        nonlocal text, cells, current_style
        if style_key is not None and cells:
            runs.append(Run("".join(text), cells, current_style))
        text = []
        cells = 0
        current_style = EMPTY_STYLE

    for x in range(last + 1):
        ch = row[x]
        key = (ch.fg, ch.bg, bool(ch.bold), bool(ch.italics),
               bool(ch.underscore), bool(ch.strikethrough),
               bool(ch.reverse), bool(ch.blink))
        if key != style_key:
            flush()
            style_key = key
            current_style = Style(
                fg=key[0], bg=key[1], bold=key[2], italic=key[3],
                underline=key[4], strike=key[5], reverse=key[6],
                blink=key[7])
        if ch.data:
            # A wide glyph's second cell is an empty stub; counting per cell
            # keeps the run width exact without double-counting the glyph.
            text.append(ch.data)
        cells += 1

    flush()
    return runs


class _History:
    """Minimal history view exposed to the widget."""

    def __init__(self, top):
        self.top = top
        self.bottom = deque()
        self.size = len(top)
        self.position = self.size


class FastScreen(pyte.Screen):
    """A plain pyte Screen with a bounded history deque.

    ``pyte.HistoryScreen`` wraps every stream event in
    ``__getattribute__`` bookkeeping, which makes large feeds noticeably
    slower. The native view only needs "append the row leaving the top of the
    screen", so this subclass keeps the normal ``Screen`` dispatch and stores
    that row directly. ``on_evict`` fires for a row dropped from the history
    deque so a file-backed scrollback never misses lines.
    """

    def __init__(self, columns: int, lines: int, history: int,
                 on_evict: Callable[[object], None] | None = None):
        self.history_top = deque(maxlen=history)
        self._on_evict = on_evict
        self._char_cache: dict[tuple, object] = {}
        super().__init__(columns, lines)
        self.history = _History(self.history_top)

    def draw(self, data: str) -> None:
        """Fast ASCII path; pyte's generic path for everything else.

        pyte builds a new ``Char`` namedtuple for every character via
        ``_replace``. That dominates large feeds. Plain ASCII text is by far
        the common case, so cache one Char per (attributes, character) and
        assign it directly. Non-ASCII, control characters and insert mode use
        the reference implementation.
        """
        if mo.IRM in self.mode:
            super().draw(data)
            return
        translated = data.translate(
            self.g1_charset if self.charset else self.g0_charset)
        if not translated.isascii() or not translated.isprintable():
            super().draw(data)
            return
        data = translated

        columns = self.columns
        buffer = self.buffer
        cursor = self.cursor
        attrs = cursor.attrs
        cache = self._char_cache
        dirty = self.dirty
        awm = mo.DECAWM in self.mode
        for char in data:
            if cursor.x == columns:
                if awm:
                    dirty.add(cursor.y)
                    self.carriage_return()
                    self.linefeed()
                else:
                    cursor.x -= 1
            key = (attrs, char)
            cell = cache.get(key)
            if cell is None:
                cell = attrs._replace(data=char)
                if len(cache) < 8192:
                    cache[key] = cell
            buffer[cursor.y][cursor.x] = cell
            cursor.x += 1
            dirty.add(cursor.y)
        if cursor.x == columns:
            if awm:
                dirty.add(cursor.y)
                self.carriage_return()
                self.linefeed()
            else:
                cursor.x -= 1

    def index(self) -> None:
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom:
            row = self.buffer[top]
            if (self.history_top.maxlen is not None
                    and len(self.history_top) >= self.history_top.maxlen
                    and self.history_top and self._on_evict is not None):
                self._on_evict(self.history_top[0])
            self.history_top.append(row)
        super().index()

    def reset(self) -> None:
        super().reset()
        if hasattr(self, "history_top"):
            self.history_top.clear()
            self.history = _History(self.history_top)


class DualScreen:
    """A pyte screen proxy with a normal history buffer and an alt screen.

    pyte's ``Stream`` binds listener methods once when it is attached, so the
    forwarding methods must exist on the class *before* ``ByteStream(dual)``
    is constructed. The simple event forwarders are generated below; the
    buffer/mode methods that need special handling are explicit.

    ``on_evict`` is called with each row that drops out of the normal hot
    history window, so the attributed scrollback store cannot miss lines when
    a single feed produces more lines than the window holds.
    """

    def __init__(self, columns: int, lines: int, history: int = HOT_HISTORY,
                 on_evict: Callable[[object], None] | None = None):
        self.normal = FastScreen(columns, lines, history, on_evict)
        self.alt = pyte.Screen(columns, lines)
        self.active = self.normal
        self.alt_active = False

    # ---- state exposed to the view -------------------------------------
    @property
    def buffer(self):
        return self.active.buffer

    @property
    def cursor(self):
        return self.active.cursor

    @property
    def mode(self):
        return self.active.mode

    @property
    def dirty(self):
        return self.active.dirty

    @property
    def margins(self):
        return self.active.margins

    @property
    def columns(self) -> int:
        return self.active.columns

    @property
    def lines(self) -> int:
        return self.active.lines

    @property
    def history(self):
        """The normal-screen history; ignore while ``alt_active`` is true."""
        return self.normal.history

    @property
    def title(self) -> str:
        return self.active.title

    @property
    def icon_name(self) -> str:
        return self.active.icon_name

    # ---- mode handling -------------------------------------------------
    def set_mode(self, *modes, **kwargs):
        private = bool(kwargs.get("private"))
        if private and 3 in modes:          # DECCOLM: keep the fixed 120-col grid
            modes = tuple(m for m in modes if m != 3)
        if private and ({47, 1047, 1049} & set(modes)):
            if not self.alt_active:
                self._enter_alt(clear=1049 in modes)
            rest = [m for m in modes if m not in (47, 1047, 1049)]
            if rest:
                self.active.set_mode(*rest, private=True)
            return
        return self.active.set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        private = bool(kwargs.get("private"))
        if private and 3 in modes:
            modes = tuple(m for m in modes if m != 3)
        if private and ({47, 1047, 1049} & set(modes)):
            if self.alt_active:
                self._leave_alt(clear=bool({1047, 1049} & set(modes)))
            rest = [m for m in modes if m not in (47, 1047, 1049)]
            if rest:
                self.active.reset_mode(*rest, private=True)
            return
        return self.active.reset_mode(*modes, **kwargs)

    def reset(self):
        self.normal.reset()
        self.alt.reset()
        self.active = self.normal
        self.alt_active = False

    def resize(self, lines=None, columns=None):
        # The phone PTY is fixed at 120x30; keep both buffers in lockstep if a
        # caller reaches this anyway.
        self.normal.resize(lines=lines, columns=columns)
        self.alt.resize(lines=lines, columns=columns)

    # ---- alt-screen transitions ---------------------------------------
    def _enter_alt(self, clear: bool) -> None:
        if clear:
            self.alt.reset()
        # Shared modes (DECCKM, DECAWM, paste/mouse/focus) follow the app.
        self.alt.mode = set(self.normal.mode)
        self.active = self.alt
        self.alt_active = True

    def _leave_alt(self, clear: bool) -> None:
        self.active = self.normal
        self.alt_active = False
        if clear:
            self.alt.reset()


def _forward(name: str):
    def method(self, *args, **kwargs):
        return getattr(self.active, name)(*args, **kwargs)

    method.__name__ = name
    return method


for _event in Stream.events:
    if not hasattr(DualScreen, _event):
        setattr(DualScreen, _event, _forward(_event))
del _event


def screen_from_bytes(data: bytes) -> DualScreen:
    """Small helper for tests: feed bytes into a fresh 120x30 dual screen."""
    screen = DualScreen(120, 30)
    pyte.ByteStream(screen).feed(data)
    return screen


def iter_history(screen: DualScreen) -> Iterable:
    """Yield the normal hot-history rows, oldest first."""
    return iter(screen.normal.history.top)
