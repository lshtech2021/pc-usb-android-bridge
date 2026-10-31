# Terminal engine decision (proposal)

> **Decision update (2026-10-31):** Option A (xterm.js + QWebEngine) was
> evaluated and **not selected**. The project will keep pyte and fix the
> native Qt terminal instead; see
> [`terminal-fix-plan.md`](./terminal-fix-plan.md). This document is retained
> for the option analysis and the rationale for rejecting Windows Terminal and
> QTermWidget.

**Status:** Superseded — Option A not selected  
**Date:** 2026-10-31  
**Scope:** PC client (`pc/`), remote SSH terminal tab  
**Companion:** [`terminal-engine-parity-checklist.md`](./terminal-engine-parity-checklist.md)

## 0. TL;DR

Replacing the hand-written pyte terminal with a mature open-source terminal
emulator is **feasible and recommended**, but **not** by adopting Windows
Terminal or QTermWidget. The realistic candidate for this codebase is
**xterm.js** (MIT) inside a `QWebEngineView`, with a thin Python ↔ JavaScript
bridge.

- **Reuse from the third party:** VT parsing, normal/alternate screen buffers,
  application cursor mode, ANSI/256/truecolor rendering, scrollback,
  selection, mouse reporting, bracketed paste, Unicode/CJK/emoji and IME.
- **Keep in Python:** all bridge transport, protocol, attach/detach, backlog,
  status, theme, zoom and full-screen behaviour. `ui.py` keeps the same widget
  API; the protocol does not change.
- **Price:** `PyQtWebEngine` brings a Chromium runtime (reported +76–100 MB;
  the current packaged dist is 105 MB). Qt 5.15.2's WebEngine is Chromium 83,
  so xterm.js must be pinned to **5.5.0** (the 6.0.0 bundle uses `WeakRef`,
  which Chromium 83 does not implement).
- **De-risking:** a 2–3 day spike (P0) with explicit gates. If it fails, fall
  back to Option B (native Qt renderer on pyte/libvterm) rather than continuing
  to patch `QPlainTextEdit`.
- **Not viable here:** Windows Terminal/OpenConsole (C++/WinRT/XAML,
  Windows-only, no embeddable Qt/Python control) and QTermWidget (GPL, no
  official Windows support, designed to own a local PTY/process).

**Decision requested:** approve Option A + the P0 spike, and answer the open
questions in §9 (installer size, scrollback cap, Qt 6 timing).

## 1. Why the current terminal is not good enough

### 1.1 Reported symptoms

- Scrollback/paging is unreliable: the scrollbar jumps, older lines appear in
  500-line pages, selection is lost when new output arrives.
- Arrow keys often do nothing in `vim`, `less` or `htop`, or print `^[[A` /
  `^[[B` instead of moving the cursor.
- Remote ANSI colours never appear; the terminal is monochrome.
- Full-screen programs (`vim`, `htop`, `tmux`, `less`) corrupt the screen.
- Paste into `vim` misbehaves; mouse clicks/scroll in terminal apps do nothing.
- Chinese IME and wide/combining characters are not explicitly supported.
- Typing feels sluggish during large output because the whole visible text is
  rebuilt for every chunk.

### 1.2 Root causes (verified in the code)

| # | Location | Finding |
|---|---|---|
| 1 | `pc/terminal.py:21,49` | Fixed 120×30 `pyte.HistoryScreen`. pyte 0.8.2 records the `?1049` mode bit but does **not** implement an alternate screen; `vim`/`htop` therefore draw over the normal buffer. |
| 2 | `pc/terminal.py:144-146` | `_row_text()` concatenates `row[x].data` and **discards every `Char` attribute**. pyte does retain `fg`/`bg`/bold/etc.; the renderer throws them away. Verified: `ESC[31mRED` produces `fg='red'` in pyte, but `"RED"` (plain) in the widget. |
| 3 | `pc/terminal.py:427-461` | Hand-written key map always sends normal-mode CSI (`ESC[A`, …), never application-mode SS3 (`ESCOA`, …); it also lacks F1–F12, Insert, keypad, Alt and most modifier combinations. `Ctrl+letter` is a blanket `key - A + 1`. |
| 4 | `pc/terminal.py:235-288` | Every `feed()` calls `setPlainText()` over up to 10,000 history lines plus 30 live rows; selection is restored by character offset; the cursor is a text caret. |
| 5 | `pc/terminal.py:187-233` | Scrollback "paging" is a manual hack: load 500 more lines from a file when the scrollbar approaches an edge, then correct `QScrollBar` positions. |
| 6 | `pc/scrollback.py:46-74` | Only plain text is persisted, so colours, attributes and alternate-screen state cannot be restored. The session file is reset on every attach (`ui.py:840` → `terminal.py:115-123`): it is a within-session paging buffer, not durable history. |
| 7 | `pc/theme.py:78-91` | The terminal schemes define only `bg`, `fg`, `selection`, `selection_fg`; there is no 16-colour ANSI palette even if attributes were rendered. |
| 8 | `pc/terminal.py` | No mouse reporting, bracketed paste, focus events, OSC 8 hyperlinks, title changes or search. |
| 9 | `pc/usb_bridge.spec:24,29` | The build deliberately excludes QtWebEngine, QtWebChannel and QtWebSockets, so every browser-based option changes packaging and size. |

pyte is a good VT model for tests and simple line output, but it is not a
terminal emulator product: it has no alternate screen, application cursor
mode, mouse or paste protocols. Building those on top of `QPlainTextEdit` means
writing a terminal renderer ourselves.

## 2. Constraints

- Python 3.13.9, PyQt5 5.15.11, **Qt runtime 5.15.2** (`PyQt5-Qt5 5.15.2`),
  Windows 10 primary; README also claims Linux/macOS.
- Qt 5.15.2 WebEngine is based on **Chromium 83**; this limits available
  JavaScript APIs and is six years old.
- No protocol changes: the terminal consumes raw bytes from `REMOTE_OUTPUT`
  and writes raw bytes to `REMOTE_DATA` (`PROTOCOL.md` §6.5). The PTY is fixed
  at 120×30; window-size negotiation is deferred (PRD §8).
- Terminal output is untrusted; the terminal must not load remote content or
  execute HTML/JS originating from output.
- PyInstaller one-folder distribution; the current `pc/dist` is 105 MB and the
  spec deliberately trims unused Qt modules.
- PyQt5 and PyQtWebEngine are GPL v3 or commercial; component licences must
  fit either path (xterm.js is MIT and adds no constraint).

## 3. Options considered

### Option 0 — Patch pyte + QPlainTextEdit (status quo plus)

What it takes: render attributes with `QTextCharFormat` or a custom
`QPainter` widget; emulate the alternate screen by swapping `pyte.Screen`
buffers; track DECCKM; expand the key map; improve paging and selection.

- **Pros:** no new dependency or installer growth; Python-only.
- **Cons:** roughly 8–15 person-days for a partial fix. `QPlainTextEdit` is
  the wrong renderer for a character-cell grid; IME, mouse, bracketed paste,
  wide characters and performance remain bespoke. pyte still lacks terminal
  features, so this decision returns.
- **Verdict:** tactical only; not recommended as the target architecture.

### Option A — xterm.js in QWebEngineView (recommended)

- **xterm.js:** MIT, actively maintained, used by VS Code, Hyper and Theia.
  It explicitly supports `vim`, `tmux` and curses apps, mouse
  events, CJK/emoji/IMEs, truecolor, scrollback and selection. Addons exist for
  search, links, serialization and WebGL; none is required for v1.
- **Integration:** vendor the prebuilt UMD bundle
  (`lib/xterm.js` 290 KB + `css/xterm.css` 5.5 KB in 5.5.0), load a local HTML
  page in `QWebEngineView`, and bridge bytes/control over `QWebChannel`.
- **Python keeps:** transport, protocol, attach/detach, backlog, status,
  themes, zoom, full screen and session lifecycle. `ui.py` keeps the same
  widget contract.
- **Costs/risks:** `PyQtWebEngine`/Chromium size and memory; Chromium 83 age;
  a JavaScript bridge to maintain; clipboard/context-menu integration; extra
  packaging work.
- **Version decision:** pin **xterm.js 5.5.0** on Qt 5.15.2. The published
  6.0.0 bundle uses `WeakRef` in its window-chain lookup; Chromium 83 predates
  WeakRef (Chrome 84+), so v6 is not safe without a shim or Qt 6. v5.5.0's
  bundle uses syntax and APIs available in Chromium 83 (arrow functions,
  classes, `??`, optional chaining; no WeakRef); the spike must still confirm
  this at runtime. A strong-reference `WeakRef` shim could make v6 load, but
  that adds an unsupported build step; prefer v5.5.0 or Qt 6.
- **Verdict:** best reuse-to-effort ratio; recommended.

### Option B — Native Qt terminal widget on pyte or libvterm

- Keep Python-only, no WebEngine, no installer growth.
- pyte would need to be replaced or extended (libvterm via `ctypes` is the
  credible VT core), but the cell renderer, selection, scrollbar, IME, mouse,
  paste and alternate-screen UI must still be built.
- **Effort:** 20–30 person-days; lower feature ceiling; ongoing VT maintenance.
- **Verdict:** fallback only if installer size or the old Chromium engine is a
  hard blocker.

### Option C — QTermWidget (LXQt / Konsole widget)

- Licensed GPL-2+ with GPL3 PyQt bindings; no official Windows support; the
  PyQt bindings are out-of-tree and stale.
- Designed to own a local PTY/child process, not to display bytes arriving
  from an external protocol, so integrating our remote stream is a workaround.
- **Verdict:** reject for this product. Plausible only for a Linux-only GPL
  build.

### Option D — Microsoft Windows Terminal / OpenConsole

During the evaluation the workspace contained the full Windows Terminal
source (v1.25, 83 MB, 3,760 files); it has since been removed.

- MIT and best-in-class, but it is a C++/WinRT + XAML (WinUI) application with
  DirectWrite/D3D and Microsoft-internal build dependencies, Windows-only.
  There is no embeddable Qt/Python control.
- Running Windows Terminal as a separate process through ConPTY would produce
  a second top-level window with no app integration, Windows-only, extra VT
  translation/latency, and still need a helper to feed the remote byte stream.
  Reusing only the parser/buffer means adopting the C++ dependency stack and
  writing the renderer.
- **Verdict:** reject. Remove the unused tree from the workspace or ignore it
  (see P4).

### Option E — Other terminal cores (libvterm, alacritty_terminal, wezterm-term)

- Good VT cores, but not complete widgets; C/Rust build and FFI/distribution
  burden; renderer and terminal UI are still custom.
- **Verdict:** not primary. libvterm is a possible pyte replacement inside
  Option B.

### Option F — Migrate to Qt 6 + xterm.js 6

- Qt 6.4+ WebEngine is Chromium 102+, supports xterm.js 6, and is the
  long-term supported path.
- The PyQt5 → PyQt6 migration touches enums, `QAction` imports, event APIs and
  packaging; it is a separate project.
- **Verdict:** follow-up, not a prerequisite. Do it when the product needs a
  modern engine or xterm.js 6 features. Not included in the terminal parity
  estimate.

### Comparison

| Option | Reuse | Effort | Size delta | Windows | Licence | Risk | Verdict |
|---|---|---|---|---|---|---|---|
| 0 — patch pyte | None | 8–15 d | 0 | Yes | — | Medium | Tactical only |
| **A — xterm.js + QWebEngine** | **Full VT + renderer** | **17–26 d** | **+~100 MB** | Yes | MIT | Medium | **Recommended** |
| B — native renderer | VT core only | 20–30 d | 0 | Yes | MIT/LGPL | Medium-high | Fallback |
| C — QTermWidget | Full widget | 10–15 d | +~5 MB | Poor | GPL | High | Reject |
| D — Windows Terminal | Full app | Months | n/a | Only | MIT | Very high | Reject |
| E — libvterm etc. | VT core only | 20–30 d | ~1 MB | Yes | MIT | Medium | Reject as primary |
| F — Qt 6 + xterm 6 | Full VT + modern engine | +10–15 d | +~100 MB | Yes | MIT | Medium | Later |

## 4. Decision

> **Not taken.** On 2026-10-31 the project chose to keep pyte and implement a
> native Qt cell renderer instead; see
> [`terminal-fix-plan.md`](./terminal-fix-plan.md). The text below records the
> proposal as it stood before that decision.

1. **Adopt Option A** (`xterm.js` in `QWebEngineView`) with a thin
   `WebTerminalWidget` adapter that preserves the existing `TerminalWidget`
   signals and methods.
2. **Run the P0 spike first.** Do not merge production code until gates G1–G4
   pass; the spike is the cheapest way to disprove the Chromium 83 assumption.
3. **Keep Option B as the fallback.** Do not invest further in the current
   `QPlainTextEdit` renderer except for critical bug fixes.
4. **Pin xterm.js 5.5.0** and verify its npm tarball SHA-256
   (`bd954fa721872170188cc5d7e83e88db3c83c9a18a4e8d24c2783d26491f59d2`)
   in the proposed `tools/vendor_xterm.py` until the app moves to Qt 6.
5. **No protocol changes.** The phone and `PROTOCOL.md` are unaffected.

## 5. Target architecture

### 5.1 Components

```
ui.py (unchanged call sites)
  └─ TerminalBackend contract (bytes_out, zoom_changed, escape_out;
     feed, begin_session, set_attached, zoom, font_size, …)
       ├─ PyteTerminal (current implementation, fallback)
       └─ WebTerminal (QWidget)
            ├─ QWebEngineView (local page, no remote content)
            └─ QWebChannel bridge (QObject slots/signals)
                 └─ terminal.html + terminal.js + xterm.js 5.5.0
                      └─ Terminal(cols=120, rows=30, scrollback=20_000)
```

### 5.2 Data flow

**Remote → screen**

`REMOTE_OUTPUT` → `PhoneClient` network thread → existing queued Qt signal →
`WebTerminal.feed(bytes)` → base64 → `QWebChannel` signal → JS decodes to
`Uint8Array` → `term.write(data, callback)` → JS acknowledges → next chunk.

- Keep at most ~64 KB in flight; hold the rest in a bounded Python queue.
  This prevents a `cat bigfile` burst from ballooning the WebEngine process.
- Base64 is required because QWebChannel transports JSON strings, not binary
  `QByteArray` (see §10 references). Overhead is ~33%; chunking keeps each
  message small.

**Keyboard → remote**

`term.onData(str)` → bridge slot → UTF-8 encode → `bytes_out.emit` →
`PhoneClient.remote_input` → `REMOTE_DATA`. `onBinary` (mouse) is forwarded
as base64 → raw bytes.

**Control**

- Zoom: JS intercepts Ctrl+wheel/Ctrl+=/−/0 and asks Python; Python is the
  single source of truth, updates JS `fontSize`, and persists
  `KEY_FONT_SIZE`.
- Theme: Python serialises the terminal palette (bg/fg/cursor/selection +
  16 ANSI + 8 bright colours) and pushes it to JS as JSON.
- Clipboard: JS sends the selected text to Python; Python writes
  `QApplication.clipboard()`. Paste reads the Qt clipboard and calls
  `term.paste(text)`, which respects bracketed-paste mode (unlike the current
  raw byte injection).
- Full screen: unchanged in Python. JS emits `escape` only while detached;
  Python exits full screen.

Bridge sketch (names only; the implementation stays small):

```js
const term = new Terminal({ cols: 120, rows: 30, scrollback: 20000, theme });
term.onData(d => bridge.input(d));                       // JS -> Python
term.onBinary(d => bridge.binary(btoa(d)));              // mouse/legacy bytes
bridge.output.connect(b64 => {                           // Python -> JS
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  term.write(bytes, () => bridge.ack());
});
```

```python
class Bridge(QObject):
    output = pyqtSignal(str)          # base64 terminal output

    @pyqtSlot(str)
    def input(self, text: str):
        self.bytes_out.emit(text.encode("utf-8"))

    @pyqtSlot(str)
    def binary(self, b64: str):
        self.bytes_out.emit(base64.b64decode(b64))
```

### 5.3 Widget contract (kept from `pc/terminal.py`)

```python
bytes_out     = pyqtSignal(object)   # bytes to remote
zoom_changed  = pyqtSignal(int)
escape_out    = pyqtSignal()

feed(data: bytes) -> None
begin_session(session_id: str) -> None
clear_screen() -> None
set_attached(attached: bool) -> None
session_file() -> str                # may return "" for the web backend
font_size() -> int
zoom(delta: int) -> None
reset_zoom() -> None
apply_theme() -> None
```

`zoom()` must be safe before the page has loaded (`ui.py:361` restores the
font size during startup): queue the requested size and apply it when JS emits
`ready`.

### 5.4 Proposed file layout

```
pc/
  terminal.py            # backend contract + create_terminal() factory
  terminal_pyte.py       # current implementation moved here
  terminal_web.py        # QWebEngineView + QWebChannel wrapper
  terminal_assets/
    terminal_assets.qrc     # compiled with pyrcc5 into terminal_assets_rc.py
    terminal.html
    terminal.js
    xterm.css
    xterm.js             # vendored 5.5.0, checksum-verified
    LICENSE.xterm.js
  usb_bridge.spec        # updated: drop WebEngine/WebChannel excludes
tools/
  vendor_xterm.py        # reproducible download + SHA-256 check (no Node needed)
```

`terminal.py` keeps exporting `TerminalWidget` for `ui.py` compatibility; the
factory chooses the backend from `USBBRIDGE_TERMINAL_ENGINE` (default `pyte`
during development, `web` after G7). Keeping both backends for one release
makes rollback a setting, not a revert.

`requirements.txt` gains `PyQtWebEngine==5.15.7` (on Windows this pulls
`PyQtWebEngine-Qt5 5.15.2`, i.e. Chromium 83). No Node.js is needed at build
time: the xterm.js bundle is vendored and checksum-verified by
`tools/vendor_xterm.py`.

Qt integration notes:

- Import `QtWebEngineWidgets` before `QApplication` is constructed (Qt 5
  requirement) and set `Qt.AA_ShareOpenGLContexts` if the probe shows it is
  needed. Keep the import at app startup even though the view itself is
  created lazily on first terminal use.
- Create the `QWebChannel`, register the bridge object and set it on the page
  **before** loading `terminal.html`; the page loads
  `qrc:///qtwebchannel/qwebchannel.js` first and then `terminal.js`.
- Prefer Qt resources for the page and assets: list them in
  `terminal_assets.qrc`, compile with `pyrcc5`, and load
  `qrc:/terminal/terminal.html`. This gives a stable origin (so
  `default-src 'self'` works), avoids `file://` path/origin quirks, and needs
  no PyInstaller data-file wiring. If `file://` is used instead, enable
  `LocalContentCanAccessFileUrls` and verify the CSP against that origin.
- If a machine has GPU/driver problems, fall back with
  `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`; the default DOM renderer does
  not need WebGL.
- Give the web view an object name and a focus proxy so tab order and the
  existing full-screen focus logic keep working.
- Disable the web view's built-in context menu (Back/Reload/Save page/View
  source); provide the existing native `QMenu` from the wrapper instead.
- Style the xterm.js scrollbar with CSS (`::-webkit-scrollbar` is available in
  Chromium 83) so it does not look like a browser scrollbar next to the Qt
  widgets.

### 5.5 Geometry and scrollback decisions

- **Fixed 120×30:** set `cols: 120, rows: 30`; never use the FitAddon to
  resize, because the phone PTY cannot follow. Font zoom changes cell size,
  not the grid. If the window is narrower than 120 cells, the outer shell
  scrolls horizontally (spike must confirm this works inside xterm.js's
  viewport; fallback is a minimum-font auto-shrink).
- **Scrollback:** set `scrollback: 20000` (configurable; ~30 MB of buffer
  memory at 120 columns). The current on-disk buffer is reset on every attach,
  so there is **no cross-attach history regression**; the only change is that a
  single very long session caps at 20k browsable lines instead of unlimited
  file paging. If unlimited browsing is required later, add the serialization
  addon or a "load older" path — not part of v1 parity.
- **Backlog:** feed the phone's 64 KiB backlog after `term.reset()`; partial
  escape sequences at the ring-buffer boundary are tolerated by xterm.js.
- **Alternate screen:** xterm.js preserves the normal buffer and its
  scrollback across `?1049h/l`, so `vim`/`htop`/`less` behave correctly.

### 5.6 Security lockdown

- Load only local assets; deny every navigation in
  `QWebEnginePage.acceptNavigationRequest` and disable
  `LocalContentCanAccessRemoteUrls`, `JavascriptCanOpenWindows` and plugins.
- Use an off-the-record profile; no persistent cookies/cache.
- CSP `default-src 'self'; script-src 'self'`; no remote fonts/scripts.
- Do not ship the web-links addon in v1 (OSC 8 links are rendered as text).
- The Chromium 83 engine is old; restricting it to local, static content and
  user-controlled terminal bytes keeps the attack surface small. This is a
  reason to schedule the Qt 6 migration, not a reason to keep pyte.

## 6. PC feature parity

The detailed, testable matrix is in
[`terminal-engine-parity-checklist.md`](./terminal-engine-parity-checklist.md).
The main mappings:

| PC feature | Where it moves |
|---|---|
| Attach/detach by connection ID, backlog replay, status pill | Python unchanged |
| Raw key input, Tab/Shift+Tab, arrows/Home/End/Delete/Insert/F1–F12, Alt, keypad | xterm.js |
| Application cursor mode (DECCKM), alternate screen, mouse, bracketed paste | xterm.js |
| ANSI 16/256/truecolor, bold/italic/underline/reverse/strikethrough | xterm.js + full `theme.py` palettes |
| Scrollback wheel/scrollbar/Shift+PgUp/PgDn, follow output | xterm.js |
| Selection + copy | xterm.js selection + Qt clipboard bridge |
| Ctrl+C copy-if-selection-else-interrupt, Ctrl+Shift+C/V, Ctrl+V, Shift+Insert, context menu | JS key handler + Python clipboard/menu |
| Font zoom Ctrl+wheel/Ctrl+=/Ctrl+−/Ctrl+0, persistence | JS shortcut → Python → JS |
| Terminal light/dark schemes | Python palette → JS theme |
| Full screen F11/Escape-only-when-detached | Python unchanged |
| 120×30 fixed PTY | xterm.js options; no fit/resize |
| Chinese IME, CJK/emoji widths | xterm.js + QtWebEngine IME |
| Session reset on attach | JS `term.reset()` |
| Large-output backpressure | Python queue + JS write callback |
| Packaging, startup, memory | P4 measurements and gates |

**Deliberate changes (need product sign-off):**

1. Installer grows by roughly 100 MB.
2. Browsable scrollback within one session is capped at 20k lines (the old
   file paging is removed); cross-attach behaviour is unchanged because it was
   already reset on attach.
3. The terminal viewport is now a real 30-row viewport; scrollback is above it
   rather than filling the whole widget. This is standard terminal behaviour
   but visually different from today's 10k-line text pad.

## 7. Delivery plan

| Phase | Deliverable | Estimate |
|---|---|---|
| **P0 spike (gate G1)** | Install `PyQtWebEngine`, load xterm.js 5.5.0 and 6.0.0 in a probe page, verify JS runtime on Chromium 83, truecolor, alternate screen, DECCKM, scrollback, IME, 10 MB throughput, QWebChannel base64 cost. Record results in this doc. | 2–3 days |
| **P1 skeleton** | Backend contract + factory; move current code to `terminal_pyte.py`; `WebTerminal` with QWebChannel; feed/input/reset/attach; feature flag. | 3–5 days |
| **P2 parity** | Clipboard and context menu; Ctrl+C semantics; zoom/persistence; full ANSI palettes; full screen; fixed geometry/horizontal scroll; focus. | 5–7 days |
| **P3 robustness** | Backpressure; selection during output; reattach/backlog; Unicode/IME edge cases; security lockdown; offscreen smoke tests. | 3–5 days |
| **P4 packaging/docs** | Spec + requirements; clean-Windows test; measure size/startup/memory; update README/PRD; the `terminal/` tree has already been removed. | 2–3 days |
| **Buffer/review** | Code review, cross-platform smoke, manual matrix. | 2–3 days |

**Total:** roughly 17–26 person-days (3–5 weeks for one engineer), including
the spike. Option B is 20–30 person-days and has a lower ceiling; Option 0 is
8–15 person-days and does not solve the architecture.

### Gates

- **G1 — Engine viability (after P0).** xterm.js 5.5.0 runs on the actual Qt
  5.15.2 WebEngine; all of truecolor, alternate screen, DECCKM, scrollback,
  bracketed paste, mouse and Chinese IME pass; 10 MB output remains
  responsive. If v5.5.0 fails, stop and evaluate Qt 6 (Option F) or Option B.
- **G2 — Size/startup.** Measured dist delta and cold-start delta are within
  the agreed budget. Proposed budget: **+120 MB** dist (hard cap +150 MB
  unless product approves), **+1.0 s** cold start, **+250 MB** RSS with the
  terminal open. Lazy-create the web view on first terminal-tab use.
- **G3 — Throughput/latency.** `cat` of 10 MB and 100k-line flood complete
  without UI freeze; keystroke echo latency < 100 ms over USB; the Python
  queue stays ≤4 MB and the in-flight JS chunk ≤64 KB.
- **G4 — Security.** No navigation, no remote requests, no clipboard/tab
  access from the page; CSP and profile lockdown verified.
- **G5 — Parity.** All P0/P1 items in the checklist pass, including
  attach/detach and the Ctrl+C/Ctrl+V semantics.
- **G6 — Packaging.** Packaged app runs on a clean Windows machine without
  Python; Linux/macOS smoke (at least import + page load) passes.
- **G7 — Rollout.** Feature flag flipped to `web` by default; pyte backend
  remains selectable for one release.

## 8. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Chromium 83 cannot run the vendored xterm.js | Blocks Option A | P0 spike; pin 5.5.0; if it still fails, Qt 6 migration or Option B |
| Installer grows ~100 MB | Product/distribution concern | Measure; trim locales/translations; lazy web view; escalate the G2 decision early |
| Old Chromium security surface | Local exploit risk | Local content only; deny navigation/remote URLs; CSP; no links addon; plan Qt 6 |
| QWebChannel base64 throughput | Input echo lag on large output | Chunk 16–64 KB; write callback + ack; fallback to loopback WebSocket (`QtWebSockets`) if G3 fails |
| Clipboard permissions | Copy/paste unreliable | Do clipboard in Qt, not `navigator.clipboard`; JS only reports selection/requests paste |
| IME/DPI/font differences | Chinese input or rendering issues | Spike on Windows 10 at 100%/125%/150%; explicit font stack; test CJK/emoji |
| Fixed 120×30 vs window size | Clipping or odd empty space | Outer horizontal scroll or minimum-font shrink; center the grid; no PTY resize |
| Selection lost on output | UX regression | xterm.js standard behaviour; test; document; do not force-clear |
| Reattach/history expectations | Perceived regression | Checklist item; explain the existing reset-on-attach behaviour; consider serialize addon later |
| JS bridge maintenance | Long-term cost | Keep the bridge <300 lines, no framework/build step; vendor pinned JS; add a codec/queue unit test |
| Packaging on macOS/Linux | Platform regressions | CI smoke builds; PyInstaller QtWebEngine hooks; document Linux system libs |

## 9. Open questions (decision needed)

1. **Installer size:** is a ~100 MB increase (105 MB → ~205 MB) acceptable?
   This is the largest tradeoff and the main reason to choose Option B.
2. **Scrollback cap:** is 20k lines per session acceptable, or must unlimited
   on-disk paging be preserved? (The current file is reset on every attach, so
   only very long single sessions are affected.)
3. **Qt 6 timing:** do we budget a PyQt6/Qt 6 migration now (modern Chromium,
   xterm.js 6), or accept Chromium 83 for this iteration and schedule it later?
4. **Platform priority:** is Windows the only shipping target? If Linux-only
   and GPL is acceptable, QTermWidget could be reconsidered; xterm.js remains
   the recommendation regardless.
5. **Licensing path:** is the PC client distributed under GPL (PyQt5 GPL) or
   with a commercial PyQt/PyQtWebEngine licence? xterm.js (MIT) is compatible
   with both; QTermWidget would force GPL.
6. **Geometry:** keep 120×30 fixed indefinitely, or plan SIGWINCH/resize
   negotiation? If resize is planned, the xterm.js FitAddon becomes relevant.
7. **Build tooling:** is a vendored, prebuilt JS bundle acceptable (no Node at
   build time), or must the build remain pure Python? The proposal vendors the
   bundle and uses a checksum-verifying Python script.
8. **Extras:** are search, clickable links, accessibility/screen-reader mode
   or session serialization required in this iteration? They are all optional
   addons; v1 excludes them.

## 10. References

- xterm.js repository and browser/feature notes: <https://github.com/xtermjs/xterm.js>
- xterm.js npm package (`@xterm/xterm` 6.0.0 latest; 5.5.0 latest v5):
  <https://www.npmjs.com/package/@xterm/xterm>
- Qt WebEngine / Chromium version map (Qt 5.15.2 → Chromium 83):
  <https://wiki.qt.io/QtWebEngine/ChromiumVersions>
- QWebChannel transports JSON strings; `QByteArray` arrives as a string:
  <https://stackoverflow.com/questions/36934402>
- QWebChannel binary limitation discussion:
  <https://stackoverflow.com/questions/39709728>
- PyInstaller + QWebEngine size reports:
  <https://stackoverflow.com/questions/66501589>,
  <https://stackoverflow.com/questions/49045142>
- QTermWidget (GPL, platforms, PyQt bindings):
  <https://github.com/lxqt/qtermwidget>
- Microsoft Windows Terminal source (MIT):
  <https://github.com/microsoft/terminal>
- `WeakRef` availability (Chrome 84+):
  <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/WeakRef>

## Appendix A — P0 spike script (outline)

1. `pip install PyQtWebEngine==5.15.7` in a scratch venv (adds
   `PyQtWebEngine-Qt5 5.15.2`).
2. Extract the xterm.js 5.5.0 and 6.0.0 tarballs into a scratch directory
   (copy only the chosen version into `pc/terminal_assets/` afterwards); run
   `navigator.userAgent` and `typeof Terminal` through
   `QWebEnginePage.runJavaScript`.
3. Load a 120×30 terminal; feed, in order:
   - `ESC[31m` / `ESC[38;5;208m` / `ESC[38;2;12;200;99m` colour tests;
   - `ESC[?1049h` alternate-screen test, then `ESC[?1049l`;
   - a `vim`-like stream and a real `vim` session;
   - `ESC[?1h` then arrow keys, capturing `onData` (expect `ESCOA`…);
   - 10 MB of `yes`-style lines; record time, memory and input echo;
   - Chinese text and IME composition.
4. Measure `QWebChannel` base64 round-trip for 16/64/256 KB chunks. Record
   whether G3 needs the WebSocket fallback.
5. Save the results (versions, user agent, pass/fail) in this document before
   P1 starts.

## Appendix B — Why the Windows Terminal tree was not reused

`terminal/` was the full Windows Terminal v1.25 source (83 MB, 3,760 files)
during the evaluation; it has since been removed from the workspace.
It is an application, not a library: its terminal control is C++/WinRT + XAML,
its renderer targets DirectWrite/D3D, its build depends on the Windows Terminal
toolchain, and it is Windows-only. There is no supported way to host it in a
PyQt5 widget or to feed it a custom USB byte stream without running a separate
ConPTY helper process and a second window. Keeping the tree in the repository
added 83 MB and licensing/build noise with no integration path; it has been
removed.
