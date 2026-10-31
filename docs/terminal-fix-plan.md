# Terminal fix plan (keep pyte, replace the display layer)

**Status:** Approved — full cell-view fix; Qt 6 migration first  
**Date:** 2026-10-31  
**Scope:** PC client (`pc/`), remote SSH terminal tab  
**Supersedes:** the xterm.js option in
[`terminal-engine-decision.md`](./terminal-engine-decision.md) (not selected)  
**Related:** [`qt6-migration-feasibility.md`](./qt6-migration-feasibility.md)

## 0. Decision summary

**Approved decisions (2026-10-31):**

1. Take the **full cell-view fix** (not the interim QPlainTextEdit patch).
2. **Mouse reporting is in scope** for this iteration (not deferred).
3. A **hidden `QLineEdit` input proxy is an accepted fallback** if the custom
   widget's IME path fails the P0 spike.
4. **Migrate to Qt 6 first**, then build the new terminal view on Qt 6.

The plan below is the approved baseline. The Qt 6 phase (4–6 days) precedes
P0; the terminal estimates remain 22–34 person-days on top of it, so the
combined programme is roughly 26–40 person-days.

Keep **pyte 0.8.2** as the VT parser/model and keep every Python-side feature
(protocol, transport, attach/detach, backlog, status, theme, zoom, full
screen). Do **not** add a browser engine or a third-party terminal.

The root cause of the reported defects is the **display/interaction layer**:
`TerminalWidget` is a `QPlainTextEdit` whose document is a flat string. A
terminal is a grid of character cells with per-cell attributes, two screen
buffers, cursor modes and a scrollback window. The current code repeatedly
flattens that grid to text (`_row_text`), rebuilds the whole document
(`setPlainText`) and pages history by hand. No amount of key-map patching
fixes that abstraction mismatch.

**Target:** replace the view with a Qt-native character-cell renderer
(`QAbstractScrollArea` + `QPainter`) and add the missing model pieces:

1. `DualScreen` — normal + alternate screen, mode tracking (DECCKM, paste,
   mouse, focus).
2. `KeyEncoder` — Qt key event → correct VT/xterm bytes in normal and
   application modes.
3. `ScrollbackStore` — attributed, file-backed history with virtual scrolling.
4. `TerminalView` — paints only visible cells, with selection, cursor, IME,
   wheel/scrollbar, copy/paste and zoom.

The public `TerminalWidget` contract and `ui.py` call sites stay unchanged.
The protocol and the Android side do not change.

**Effort:** 22–34 person-days (roughly 4.5–6.5 weeks for one engineer),
including tests and packaging. An optional 8–13 day interim patch (model +
partial colours) can ship first. Qt 6 migration is a separate 4–6 day task and
is **not required** for these fixes; see the Qt 6 document.

**Why not patch `QPlainTextEdit` in place:** it can render character formats,
but it is a line/document widget, not a cell grid. Backgrounds, block cursors,
wide-character cell widths, alternate-screen swaps and a virtual scrollbar over
unlimited history all fight its document model; the current 500-line paging and
selection re-mapping are symptoms. A cell view is the smaller correct
abstraction, and it keeps the pyte model the project already has.

If the widget class must stay `QPlainTextEdit`, the interim quick wins in §4
are the practical maximum: they fix keys, alternate-screen apps and most
colours, but scrollback, cross-page selection and full-redraw cost remain
compromised.

## 1. Problem → fix map

| # | Symptom | Root cause | Fix | Main files |
|---|---|---|---|---|
| 1 | No colours | `_row_text()` only copies `Char.data`; attributes are discarded (`terminal.py:144-146`) | `row_runs()` extracts text + fg/bg/flags; view paints runs | `terminal.py`, `theme.py` |
| 2 | Arrow keys wrong in `vim`/`less` | No DECCKM tracking; fixed `ESC[A` mapping (`terminal.py:427-461`) | `KeyEncoder` reads private mode `?1`; sends `ESCOA` in application mode | `terminal.py` |
| 3 | `vim`/`htop`/`less` corrupt the screen | pyte records `?1049` but has no alternate screen | `DualScreen` proxy with normal/alt buffers | `terminal.py` |
| 4 | Scrollback/paging jumps, lost selection | Full `setPlainText` re-render; 500-line file paging; scrollbar correction (`terminal.py:187-288`) | `ScrollbackStore` + `QAbstractScrollArea` virtual scrollbar; absolute line indices | `terminal.py`, `scrollback.py` |
| 5 | Colours missing from history | Scrollback file stores plain text (`scrollback.py:46-74`) | Versioned attributed JSONL, lazy slicing | `scrollback.py` |
| 6 | Terminal schemes cannot show ANSI colours | Only bg/fg/selection in `theme.py:78-91` | Add 16 ANSI + 8 bright colours to both schemes | `theme.py` |
| 7 | Pasting into `vim` misbehaves | Raw clipboard bytes injected (`terminal.py:319-326`) | Bracketed-paste mode `?2004`; `paste()` wrapper | `terminal.py` |
| 8 | No mouse support | No mouse-mode tracking | SGR/X10 mouse encoding (in scope, P1) | `terminal.py` |
| 9 | Chinese IME untested | `QPlainTextEdit` provided it implicitly | `inputMethodEvent` / `inputMethodQuery` in the custom view | `terminal.py` |
| 10 | Sluggish during large output | Parse + full repaint on every USB frame | Batched feed (time budget) + paint only visible rows | `terminal.py` |
| 11 | Weird text caret instead of terminal cursor | `QTextCursor` is a text caret | Paint a block/underline cursor at the cell | `terminal.py` |
| 12 | Ctrl+C / copy / paste semantics fragile | Mixed widget/document handling | Cell selection + explicit clipboard actions | `terminal.py` |

## 2. Target architecture

```
TerminalWidget (QAbstractScrollArea, public API unchanged)
  ├─ DualScreen                 pyte normal HistoryScreen + alt Screen
  │    └─ mode helpers          ?1 DECCKM, ?2004 paste, ?1000/1002/1003 mouse,
  │                             ?1004 focus, ?1049/47/1047 alt screen
  ├─ ScrollbackStore            attributed JSONL + hot history spill
  ├─ LineProvider               file + hot pyte history + live screen
  ├─ KeyEncoder                 Qt key event -> VT bytes
  └─ TerminalView               paint, cursor, selection, IME, wheel, clipboard
```

**Buffer model.** Absolute line indices are stable while output grows:

```
file_count   = lines in the attributed scrollback file (oldest)
hot_count    = lines in pyte's HistoryScreen.top
total_history = file_count + hot_count
screen_base  = total_history
total        = total_history + screen.lines        # screen.lines is 30
line(i):
  i < file_count                 -> ScrollbackStore.slice(i, i+1)
  i < file_count + hot_count     -> pyte history row i - file_count
  else                           -> live screen row i - screen_base
```

When a screen line scrolls off, it is appended to `hot_count`; when a hot line
is evicted, it moves from `hot_count` to `file_count`. Both moves leave the
absolute index of every existing line unchanged, which makes selection,
follow-mode and scrollbar math simple.

**Proposed files**

```
pc/
  terminal.py          # public TerminalWidget; view + input + clipboard
  terminal_model.py    # DualScreen, mode helpers, row->runs (no Qt widgets)
  terminal_keys.py     # KeyEncoder (QtCore key constants only)
  scrollback.py        # attributed ScrollbackStore (replaces the text cache)
  theme.py             # terminal palettes: bg/fg/selection + 16 ANSI + bright
  ui.py                # unchanged API; only startup wiring if needed
```

Keep `TerminalWidget` as the exported name so `pc/ui.py:23` and the rest of
the UI do not change.

## 3. Detailed design

### 3.1 Alternate screen and modes (`DualScreen`)

pyte 0.8.2 records private-mode bits but implements only the normal buffer.
`Screen.set_mode(*modes, private=True)` stores `mode << 5` in `screen.mode`,
so `?1h` → `1 << 5 = 32` and `?1049h` → `1049 << 5 = 33568`. Use helpers:

```python
def private(screen, n: int) -> bool:
    return (n << 5) in screen.mode

app_cursor  = private(screen, 1)      # DECCKM
app_keypad  = private(screen, 66)     # DECNKM (optional)
bracketed   = private(screen, 2004)
mouse_1000  = private(screen, 1000)   # press/release
mouse_1002  = private(screen, 1002)   # button motion
mouse_1003  = private(screen, 1003)   # any motion
mouse_sgr   = private(screen, 1006)
focus_1004  = private(screen, 1004)
```

`DualScreen` owns two pyte screens and forwards every `Stream` event to the
active one. **Important:** `pyte.Stream` binds listener methods once at
attach time (`Stream._parser_fsm` calls `getattr(listener, attr)` while
building its dispatch tables), so `DualScreen` must define real forwarding
methods for every event **before** `pyte.ByteStream(dual)` is created. Generate
the simple forwarders at class creation; keep `set_mode`/`reset_mode` explicit.

```python
class DualScreen:
    def __init__(self, cols, rows, hot):
        self.normal = pyte.HistoryScreen(cols, rows, history=hot)
        self.alt = pyte.Screen(cols, rows)
        self.active = self.normal
        self.alt_active = False

    @property
    def buffer(self): return self.active.buffer
    @property
    def cursor(self): return self.active.cursor
    @property
    def mode(self): return self.active.mode
    @property
    def history(self): return self.normal.history
    # columns, lines, dirty, margins, title, ... forward similarly

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

    def _enter_alt(self, clear):
        if clear:
            self.alt.reset()
        self.alt.mode = set(self.normal.mode)   # inherit shared modes
        self.active = self.alt
        self.alt_active = True

    def _leave_alt(self, clear):
        self.active = self.normal
        self.alt_active = False
        if clear:
            self.alt.reset()

# Generate forwarders for every pyte Stream event that is not explicit.
def _forward(name):
    def method(self, *args, **kwargs):
        return getattr(self.active, name)(*args, **kwargs)
    method.__name__ = name
    return method

for _event in pyte.streams.Stream.events:
    if not hasattr(DualScreen, _event):
        setattr(DualScreen, _event, _forward(_event))
```

`?1049` details:

- `?1049h`: switch to the alt buffer, clear it, home the cursor. The normal
  buffer, scrollback and cursor are untouched, so `?1049l` restores them.
- `?47h/?47l`: switch buffers without clearing; alt contents are retained.
- `?1047h/?1047l`: switch without clearing on enter, clear the alt buffer on
  exit.
- Ignore DECCOLM (`?3h/?3l`): pyte resizes the active screen to 132 columns,
  but the phone PTY is fixed at 120. The view and the remote would disagree.
  Keep the grid at 120 columns (and do not resize the two buffers).
- While alt is active, do **not** spill alt lines into the normal scrollback
  and do not show scrollback; the scrollbar range collapses to the alt screen.
  `DualScreen.history` keeps returning the normal history, so the view must
  check `alt_active` before reading it.
- On exit, restore the normal scroll position and follow state.

Validate with a `vim`/`less`/`htop` session and a synthetic stream:
`ESC[?1049h` + text + `ESC[?1049l`.

Prototype status: a local probe against pyte 0.8.2 verified that this proxy
switches to the alt buffer on `?1049h`, returns to the normal buffer/history on
`?1049l`, and exposes the `?1h` bit as expected. The remaining risk is the Qt
view, not the screen model.

### 3.2 Key encoding (`KeyEncoder`)

`keyPressEvent` should delegate to a table, not a 20-line dictionary. The
encoder takes `(key, modifiers, text, repeat)` and returns `bytes | None`.

| Key | Normal mode | Application cursor mode (`?1h`) |
|---|---|---|
| Up/Down/Right/Left | `ESC[A/B/C/D` | `ESCOA/B/C/D` |
| Home / End | `ESC[H` / `ESC[F` | same (DECCKM affects cursor keys only) |
| Insert / Delete | `ESC[2~` / `ESC[3~` | same |
| PageUp / PageDown | `ESC[5~` / `ESC[6~` | same |
| F1–F4 | `ESCOP..S` | same |
| F5–F12 | `ESC[15~ 17~ 18~ 19~ 20~ 21~ 23~ 24~` | same |
| Tab / Shift+Tab | `\t` / `ESC[Z` | same |
| Backspace / Enter / Escape | `0x7f` / `\r` / `ESC` | same |
| Ctrl+letter | `0x01..0x1a` | same |
| Ctrl+Space / Ctrl+@ | `0x00` | same |
| Alt+key | `ESC` + key bytes | same |

Modifier form (xterm): `m = 1 + Shift(1) + Alt(2) + Ctrl(4)`.

- Arrows with modifiers: `ESC[1;{m}A/B/C/D`.
- Home/End with modifiers: `ESC[1;{m}H/F`.
- Insert/Delete/Page with modifiers: `ESC[2;{m}~`, `ESC[3;{m}~`,
  `ESC[5;{m}~`, `ESC[6;{m}~`.
- F1–F4 with modifiers: `ESC[1;{m}P..S`.
- F5–F12 with modifiers: `ESC[{code};{m}~`.
- Alt+arrow/Alt+Home use the modifier form, not `ESC` + `ESC[...]`.
- Application keypad (`?66h`) is optional; if implemented, use `ESCOp..y`
  for keypad digits/operators. Qt does not reliably distinguish keypad digits
  from the main row in `keyPressEvent`; treat it as P2.
- Printable text: `event.text().encode("utf-8")`, except when the IME is
  composing (see §3.5).
- AltGr (`Qt.GroupSwitchModifier`) is not Alt: if `event.text()` is non-empty,
  send the text instead of an `ESC` prefix. Otherwise European layouts such as
  German/Polish break.
- `Ctrl+Shift+letter`: send the control byte unless the combination is a
  widget shortcut (Ctrl+Shift+C/V copy/paste).
- `Ctrl+C`: copy if a selection exists, otherwise send `0x03` (preserve
  current behaviour).
- `Ctrl+V`: paste (preserve current behaviour); `Ctrl+Shift+V` and
  `Shift+Insert` also paste.
- `Shift+PageUp/PageDown`: scroll the local viewport; plain PageUp/PageDown
  go to the remote.
- `Tab`/`Backtab`: keep the existing `event()` interception so Qt focus
  traversal does not eat them while attached (`terminal.py:328-336`).

**Mouse reporting (in scope).** Track `?1000` (press/release), `?1002`
(button motion), `?1003` (any motion) and `?1006` (SGR). `?1015` (urxvt) is
not implemented in v1. When any mouse mode is active and Shift is **not**
held, encode the event and send it; Shift forces local selection (xterm
behaviour). Otherwise the mouse continues to select/copy locally.

- Buttons: 0/1/2 = left/middle/right; wheel up/down = 64/65 (SGR); release
  is `m` in SGR, button 3 in X10.
- Modifier bits: Shift +4, Alt +8, Ctrl +16.
- SGR: `ESC[<b;x;yM` press / `ESC[<b;x;ym` release, coordinates 1-based.
- X10: `ESC[M` + `b+32` + `x+33` + `y+33`; clamp coordinates to 223.
- `?1002` sends motion only while a button is held; `?1003` sends all motion.
- Wheel over the viewport scrolls locally unless a mouse mode is active.

Unit-test the table directly: feed `?1h`, press Up, expect `ESCOA`; press
Ctrl+Up, expect `ESC[1;5A`; press Alt+Up, expect `ESC[1;3A`.

### 3.3 Attributes, colours and theme

pyte keeps per-cell attributes in `Char`:

```
data, fg, bg, bold, italics, underscore, strikethrough, reverse, blink
```

`row_runs(row, columns)` converts a row into `Run(text, cells, fg, bg, flags)`
runs. Rules:

- `data == ""` is the stub of a preceding wide character: it has cell width 1
  for background but contributes no text.
- A character with `wcwidth(ch) == 2` occupies two cells; the next cell is a
  stub. Combining characters are already merged into the previous `data` by
  pyte. (`wcwidth` is already a pyte dependency; no new package is needed.)
- Group consecutive cells with the same style; track `cells` so painting does
  not depend on Python string length.
- `reverse` swaps fg/bg after resolving defaults. If both are `default`, use
  the theme fg/bg swapped.
- `bold` uses a bold font; for the eight base ANSI foregrounds, optionally
  promote to the bright variant (xterm convention).
- `blink` may be ignored initially (or implement a 500 ms timer later).

**pyte colour names** (verified against `pyte.graphics`):

| pyte value | Theme key |
|---|---|
| `default` | `fg` / `bg` |
| `black red green brown blue magenta cyan white` | same, with `brown` → `yellow` |
| `brightblack … brightwhite` | same |
| `#rrggbb` or `rrggbb` | exact colour (256-colour and 24-bit SGR) |

Two pyte quirks to handle explicitly:

- ANSI "yellow" is called **`brown`** in pyte's `FG_ANSI`/`BG_ANSI`.
- `pyte.graphics.BG_AIXTERM` contains the typo **`bfightmagenta`**; map it to
  `brightmagenta`.

Extend `pc/theme.py:78-91` with a full palette per scheme, for example:

```python
TERMINAL_THEMES = {
    "dark": {
        "bg": "#1E1E1E", "fg": "#D4D4D4",
        "selection": "#264F78", "selection_fg": "#FFFFFF",
        "cursor": "#D4D4D4",
        "black": "#000000", "red": "#CD3131", "green": "#0DBC79",
        "yellow": "#E5E510", "blue": "#2472C8", "magenta": "#BC3FBC",
        "cyan": "#11A8CD", "white": "#E5E5E5",
        "brightblack": "#666666", "brightred": "#F14C4C",
        "brightgreen": "#23D18B", "brightyellow": "#F5F543",
        "brightblue": "#3B8EEA", "brightmagenta": "#D670D6",
        "brightcyan": "#29B8DB", "brightwhite": "#FFFFFF",
    },
    "light": { ... readable values on white ... },
}
```

Keep `terminal_color(key)` working; add the new keys. The view maps a pyte
value to a `QColor` once and caches it.

### 3.4 Scrollback store and paging

Replace the text-only `ScrollbackCache` with an attributed, append-only store.

**File format** (`~/.usbbridge/sessions/<id>.scrollback`, reset on attach):

```
{"v":1,"cols":120}
{"r":[["user@host", "green", "default", 0, 9], [" $ ", "default", "default", 0, 3]]}
{"r":[["中文", "#d4d4d4", "#1e1e1e", 0, 4]]}
```

Each `r` entry is `[text, fg, bg, flags, cells]`; `flags` is a bit mask
(bold=1, italic=2, underline=4, strike=8, reverse=16, blink=32). A line is one
JSON object; a byte-offset table gives `slice(start, end)` without scanning
the whole file. Keep a small parsed-line cache (256–1024 lines) around the
viewport.

**Spill.** Keep `HistoryScreen(history=HOT_HISTORY)` with a small hot window
(e.g., 2,000 lines, roughly 24 MB of pyte `Char` objects); when pyte evicts a
line, convert it to runs and append to the file. Do this only on the normal
screen, never in the alt buffer. Absolute indices stay stable (§2), so the
current id-diff spill logic (`terminal.py:148-159`) can be reused with a
different serializer.

**Scrollbar and follow mode.**

```
viewport_rows = max(1, viewport_height // cell_height)
total         = total_history + screen.lines
scroll_max    = max(0, total - viewport_rows)
```

- `verticalScrollBar.setRange(0, scroll_max)`, `pageStep=viewport_rows`,
  `singleStep=1`.
- `value` is the absolute index of the top visible line.
- Follow if `value == scroll_max`; on new output set `value = scroll_max`.
- If the user scrolled up, keep `value` (the same absolute line stays at the
  top) while output grows.
- Wheel scrolls 3 lines; Shift+PageUp/PageDown scroll `pageStep`; the native
  scrollbar works at any position.
- This removes the 500-line page hack and the `QScrollBar` correction entirely.
- If the viewport is taller than 30 rows, the extra rows show history above
  the live screen. If it is shorter, the viewport shows part of the screen
  and the scrollbar can reach the rest; alternatively shrink the font to fit
  30 rows (choose in P0).

**Selection.** Store anchor and cursor as `(absolute_line, column)`. Absolute
line indices are stable, so selection survives page loads, spills and new
output. Paint the selected cell range; copy extracts text from the provider,
skips wide-character stubs, trims trailing blanks per line, and joins lines
with `\n`. Selection across file/hot/screen works because the provider hides
the storage boundary.

### 3.5 Rendering, cursor, IME

`TerminalView` is a `QAbstractScrollArea`; paint in `paintEvent` on the
viewport:

1. Fill with the theme background.
2. Resolve `start = scroll_value`, `rows = viewport_rows`.
3. For each visible line, get runs from `LineProvider`.
4. Fill run backgrounds and the selection background.
5. Draw text with
   `QPainter.drawText(QRectF(x, y, cells*cell_w, cell_h),
   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, run.text)`.
6. Draw the cursor block (or underline) at `screen_base + cursor.y`; for a
   block cursor, redraw the character under it in the background colour.
7. Draw the IME pre-edit text at the cursor with an underline.

Details:

- Cell metrics: `QFontMetricsF(font)`, `cell_w = horizontalAdvance("M")`,
  `cell_h = height()`; recompute on font/DPI change. Use a font stack that
  falls back for CJK (`Consolas, "Microsoft YaHei Mono", monospace`).
- Wide characters: advance by `2 * cell_w`; do not draw the stub cell's empty
  text. If a fallback font's glyph is wider than two cells, clip/scale only as
  a last resort; test CJK/emoji.
- Cache `QFont` variants (bold, italic, bold-italic) and `QColor` values.
- Paint only visible rows; 120 × ~60 cells is tiny compared with a document
  layout.
- Horizontal scrolling: the grid is fixed at 120 columns, so set the
  horizontal scrollbar range to `max(0, 120*cell_w - viewport_width)` and
  translate the painter by `-hscroll`. Convert mouse positions with
  `+hscroll`. Do not reflow or wrap.
- `focusInEvent`/`focusOutEvent`: repaint the cursor; send `ESC[I`/`ESC[O`
  if `?1004` is set.
- IME:
  - `setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)`.
  - `inputMethodEvent`: send `event.commitString()` as UTF-8; keep
    `event.preeditString()` for painting; clear on commit.
  - `inputMethodQuery`: return the cursor rectangle for
    `Qt.InputMethodQuery.ImCursorRectangle`, `ImEnabled=True`, and
    `ImHints=Qt.InputMethodHint.ImhNoPredictiveText |
    Qt.InputMethodHint.ImhNoAutoUppercase`.
  - In `keyPressEvent`, handle special keys first; send printable
    `event.text()` only when no IME composition is active. Verify Chinese
    input in P0; if Qt delivers duplicate text, use a composing flag or a
    hidden `QLineEdit` input proxy as a fallback (last resort).
- Clipboard: `QApplication.clipboard()`; never use a web/browser clipboard.
- Context menu: native `QMenu` with Copy/Paste/Select all/Zoom actions;
  enable Copy only when a selection exists.

### 3.6 Input, paste, zoom, theme, full screen

- `paste`: read the Qt clipboard, normalize `\r\n`/`\r` to `\n`; if `?2004`
  is set, wrap with `ESC[200~` … `ESC[201~`; otherwise send raw UTF-8.
- `zoom(delta)` / `reset_zoom()`: change the font point size (7–26), recompute
  metrics, clamp the scroll value, repaint, emit `zoom_changed`. Preserve the
  same shortcuts: Ctrl+wheel, Ctrl+=/−, Ctrl+0.
- `apply_theme()`: reload bg/fg/selection/ANSI colours, repaint.
- `begin_session(id)`: reset `DualScreen`, `ScrollbackStore`, selection,
  scroll and pending queue; repaint.
- `set_attached(bool)`: gate input and focus; Escape emits `escape_out` only
  when detached (same as today).
- Full screen is still handled by `ui.py`; the view only needs to fill its
  parent and repaint on resize.
- The public API stays exactly as today: `bytes_out`, `zoom_changed`,
  `escape_out`, `feed`, `begin_session`, `clear_screen`, `set_attached`,
  `session_file`, `font_size`, `zoom`, `reset_zoom`, `apply_theme`.

### 3.7 Performance and batching

- `feed(data)` appends to a bounded byte queue; a 16 ms `QTimer` drains up to
  64 KiB (or a 5 ms budget) per tick. This keeps the UI responsive when the
  phone sends a 10 MB burst and coalesces repaints.
- Parse through `pyte.ByteStream`, which already handles UTF-8 split across
  frames.
- Repaint only the viewport after each drain. pyte's `dirty` set can narrow
  the repaint, but a full viewport repaint of ~7,200 cells is already cheap;
  start simple, optimize only if measurements demand it.
- Keep the queue cap explicit (e.g., 16–32 MiB). If exceeded, surface a
  visible notice rather than silently dropping output; the protocol has no
  flow control, so this is a product decision.
- Memory: hot pyte history 2,000 lines (~24 MB), scrollback parsed cache
  256–1,024 lines, queue cap, plus the view. Measure RSS.

## 4. Interim quick wins (optional)

If a release is needed before the full view lands, ship these in order; the
code is reused by the full fix:

| Step | Work | Estimate |
|---|---|---|
| QW1 | `DualScreen` + mode helpers + `KeyEncoder`; wire into the current `QPlainTextEdit` | 3–5 d |
| QW2 | Full ANSI palettes in `theme.py`; render formatted runs in `_render` (cap the rendered window if too slow) | 3–5 d |
| QW3 | Improve current paging: block-height scroll correction, preserve selection/follow | 2–3 d |

This fixes arrows, alternate-screen apps and (mostly) colours, but does **not**
fix virtual scrollback, selection across history or the full-document
re-render cost. Gate: if QW2 cannot keep up with 10 MB output, stop and go
straight to the full view.

## 5. Delivery plan

| Phase | Deliverable | Estimate |
|---|---|---|
| **Qt 6** | Mechanical PyQt5 → PyQt6 migration (imports, scoped enums, removed APIs, spec/requirements); clean-machine build. See the Qt 6 document. | 4–6 d |
| **P0 spike** | `TerminalView` prototype: paint 120×30 with colours, CJK/wide chars, cursor, selection, block scroll; Chinese IME commit/pre-edit; mouse encoding; measure paint time. Decide font stack and viewport/font shrink policy. | 2–3 d |
| **P1 model** | `DualScreen`, mode helpers, `row_runs`, `KeyEncoder`, mouse encoder; unit tests for alt screen, DECCKM, modifiers, wide chars, mouse modes. | 4–7 d |
| **P2 scrollback** | `ScrollbackStore` v1 (attributed JSONL), hot spill, lazy slice, absolute-index provider; tests with 50k lines. | 3–5 d |
| **P3 view** | `QAbstractScrollArea` painting; colours/attrs; cursor; selection/copy; wheel/scrollbar/follow; zoom/theme; IME; focus; mouse routing; context menu. | 8–11 d |
| **P4 integration** | Wire into `ui.py`; attach/detach/backlog/full screen; offscreen tests; 10 MB/50k-line performance; clean-machine packaged build. | 3–5 d |
| **Buffer** | Review, manual matrix, regressions. | 2–3 d |

**Total:** 26–40 person-days including the Qt 6 phase (roughly 5–8 weeks for
one engineer). The terminal work alone is 22–34 person-days. The interim path
is 8–13 days but is not a full fix.

### Acceptance criteria

- **Colours:** 16-colour, 256-colour and 24-bit sequences render exactly;
  bold/italic/underline/reverse/strike work; history keeps its colours.
- **Keys:** arrows work in normal and application cursor modes; Home/End/
  Insert/Delete/Page/F1–F12/modifiers/Alt work; Tab/Shift+Tab, Ctrl+C
  copy-vs-interrupt, paste and bracketed paste behave.
- **Full-screen apps:** `vim`, `less`, `htop`, `tmux`, `top` render and restore
  the normal screen/history correctly.
- **Scrollback:** at least 50,000 lines; wheel, scrollbar and
  Shift+PageUp/PageDown reach any position; follow mode; no 500-line jumps;
  selection and copy across file/hot/screen.
- **Performance:** 10 MB `cat` and 100k-line flood complete without freezing
  the UI; feed is sliced at 8 KB so input/paint still run between slices. On
  the current host this is ~32 s (1 MB ≈2.7 s); pyte parsing and run
  extraction are the bottleneck. Paint of the visible viewport should be
  ≤16 ms on a real display (the offscreen grab is not representative).
- **IME/Unicode:** Chinese composition and commit work; CJK/emoji widths align
  in the grid.
- **Parity:** attach/detach/backlog, status, theme, zoom persistence, full
  screen (Escape only when detached), fixed 120×30 and horizontal scrolling
  for narrow windows.
- **Packaging:** PyInstaller build runs on a clean Windows machine; no new
  dependency for the native fix (Qt 6 aside).

### Test plan

- `pc/test_terminal_model.py`: alt-screen enter/exit, normal buffer/history
  restored, modes (`?1`, `?2004`, `?1000`, `?1004`), wide chars, colour names.
- `pc/test_terminal_keys.py`: every row of the key table; modifier math;
  DECCKM on/off; Ctrl/Alt; Shift+Tab; plain Page vs Shift+Page.
- `pc/test_scrollback.py`: append/spill/slice, absolute-index stability,
  50k lines, selection extraction across boundaries.
- `pc/test_terminal_view.py`: offscreen `QWidget.render()` to `QImage`; sample
  pixels for fg/bg/reverse/selection/cursor; feed a scripted stream; simulate
  key events and `QInputMethodEvent`; scroll/follow assertions.
- Manual matrix: `vim`, `less`, `htop`, `tmux`, `top`, coloured `ls`,
  truecolor swatches, CJK/emoji, Chinese IME, 50k `seq`, 10 MB `cat`,
  attach/detach/reattach, full screen, 125%/150% DPI, packaged build.

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| Custom-widget IME is awkward | P0 proves commit/pre-edit; fallback to a hidden `QLineEdit` input proxy if needed |
| Font fallback breaks CJK cell widths | Choose the font stack in P0; test 100/125/150% DPI; clip only as last resort |
| pyte lacks a specific sequence | Implement the common set here (alt screen, DECCKM, paste, mouse, focus); document remaining gaps; libvterm remains a future model swap |
| Scrollback JSONL is too slow/large | Cache visible lines; measure 50k lines; switch to a compact binary format if needed |
| pyte parsing blocks the UI on huge output | Time-budgeted batch feed; if still too slow, parse on a worker thread and hand runs to the view |
| Selection/scroll edge cases | Absolute-index model + focused unit tests + manual matrix |
| Qt 6 migration overlaps | Write new code with scoped enums (works on PyQt5 5.15.11) and Qt6-compatible APIs; see the Qt 6 document |
| Regression in `ui.py` | Keep the public widget API; offscreen smoke tests; one-release rollback tag |

## 6. Qt 6

**Approved order: Qt 6 first.** The migration is feasible and mostly
mechanical; see [`qt6-migration-feasibility.md`](./qt6-migration-feasibility.md).
Complete the Qt 6 branch and clean-machine build before starting P0, so the
new renderer is written and tested once.

## 7. Open questions

Resolved (2026-10-31): full cell-view fix approved; mouse reporting in scope;
hidden `QLineEdit` IME fallback accepted; Qt 6 first.

Still to decide during P0/P2:

1. Scrollback retention: hot window size (2,000 lines) and any overall cap?
2. Viewport policy when the window is shorter than 30 rows: shrink the font
   to fit, or scroll within the screen?
3. Large-output throughput: pyte takes ~32 s for 10 MB / 100k lines on this
   host. Is that acceptable, or should a future iteration evaluate a C parser
   (libvterm) behind the same view/model interface?

## 8. Progress log (2026-10-31)

- **Qt 6 phase complete** in the working tree; see
  [`qt6-migration-feasibility.md`](./qt6-migration-feasibility.md) §9. The
  packaged build starts.
- Added `pc/terminal_model.py`: `DualScreen` (normal/alt buffers, `?1049` /
  `?47` / `?1047`, DECCOLM guard), private-mode helper, `row_runs` with
  per-cell style, colour normalisation, `HOT_HISTORY`.
- Added `pc/terminal_keys.py`: `KeyEncoder` for DECCKM, xterm modifier
  encoding, Home/End/Insert/Delete/Page, F1–F12, Ctrl/Alt/AltGr, Tab/Backtab.
- Added headless tests `pc/test_terminal_model.py` and
  `pc/test_terminal_keys.py`; both pass.
- Added `ScrollbackStore` to `pc/scrollback.py`: versioned attributed JSONL,
  byte-offset slicing, parsed-line cache; `pc/test_scrollback.py` passes. The
  interim widget still uses `ScrollbackCache`; the native view uses
  `ScrollbackStore`.
- **Native view implemented** in `pc/terminal_view.py`
  (`QAbstractScrollArea` + `QPainter`): colours/attributes, block cursor,
  selection/copy, wheel/scrollbar/follow, alt screen, IME commit/pre-edit,
  SGR/X10 mouse reporting, context menu and zoom/theme. `pc/terminal.py` is
  now the public shim; `pc/terminal_qplain.py` keeps the previous
  implementation for rollback.
- Added `pc/test_terminal_view.py`: alt screen, colours, selection/clipboard,
  Ctrl+C copy-vs-interrupt, bracketed paste, queued DECCKM before a key,
  mouse reporting, scroll/follow, 2.5k-line spill, IME commit, zoom/theme.
- Full test sweep passes: model, keys, scrollback, view, text receive, FS
  explorer, proxy tether.
- Performance measured on this host: 1 MB / 10k lines ≈2.7 s; 10 MB /
  100k lines ≈32 s with 8 KB feed slices (UI remains responsive; pyte
  parsing and run extraction dominate). One optimisation already applied:
  a fast ASCII `draw` path and tighter `row_runs` reduced 1 MB parsing from
  ~21 s to ~2.7 s.
- PyInstaller rebuild succeeds (97 MB) and the packaged executable starts.
- **Still pending:** real-display visual/DPI QA, clean-machine test,
  Linux/macOS smoke, and a decision on whether pyte's large-output throughput
  (≈32 s for 10 MB) is acceptable or a C parser (libvterm) should replace it
  later.
