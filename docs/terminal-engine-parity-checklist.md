# Terminal engine parity checklist

> **Superseded (2026-10-31):** the xterm.js / QWebEngine path was not
> selected. This checklist is retained as analysis only; the active plan is
> [`terminal-fix-plan.md`](./terminal-fix-plan.md).

Companion to [`terminal-engine-decision.md`](./terminal-engine-decision.md).
Use this file to plan, review and sign off the engine replacement (P0–P4).

**Legend:** `P0` must pass before the `web` backend becomes the default;
`P1` must pass before release; `P2` may follow. Status is one of
`TODO`, `WIP`, `PASS`, `FAIL`, `N/A`.

## 0. P0 spike results (fill in before P1)

| Item | Result / evidence |
|---|---|
| Qt / PyQt5 versions | |
| QtWebEngine version and Chromium version (`navigator.userAgent`) | |
| xterm.js 5.5.0 loads (`typeof Terminal`) | |
| xterm.js 6.0.0 loads (expected: fails on `WeakRef`; record the error) | |
| Truecolor / 256 / 16 colours | |
| Alternate screen (`vim`, `htop`, `less`) | |
| Application cursor mode (arrows in `vim`) | |
| Scrollback 20k; wheel, scrollbar, Shift+PgUp/PgDn | |
| Bracketed paste | |
| Mouse reporting | |
| Chinese IME and CJK widths | |
| 10 MB output throughput / UI responsiveness | |
| QWebChannel 16/64/256 KB round-trip cost | |
| Decision for G3: QWebChannel or WebSocket fallback | |

## 1. Attachment and session

| ID | Capability | Target behaviour | How to verify | Pri | Status |
|---|---|---|---|---|---|
| T01 | Attach by connection ID | `CONN_ATTACH` flow unchanged; state pill goes `attaching` → `attached`; terminal gets focus | Select a running phone profile, click Attach | P0 | TODO |
| T02 | Backlog replay | 64 KiB backlog is fed after `term.reset()`; screen shows current remote state | Reattach after output was produced while detached | P0 | TODO |
| T03 | Detach | `CONN_DETACH` sent; widget read-only/ignores input; pill `detached` | Click Detach, type; phone session keeps running | P0 | TODO |
| T04 | Remote close | `REMOTE_CLOSE` shows the closed state; input stops | Stop the SSH profile on the phone while attached | P1 | TODO |
| T05 | Reattach | A new attach resets the screen and scrollback and replays backlog | Attach, detach, attach again | P0 | TODO |
| T06 | Clear screen | `begin_session`/`clear_screen` resets the terminal and scrollback | Call the existing clear action/path | P1 | TODO |
| T07 | Status/error messages | `REMOTE_ERROR` codes still surface in the pill/log | Simulate `CONN_BUSY` or stop the profile | P1 | TODO |
| T08 | Input gating | Keys/clicks do nothing while detached | Detach and press keys | P0 | TODO |

## 2. Keyboard and terminal protocols

| ID | Capability | Target behaviour | How to verify | Pri | Status |
|---|---|---|---|---|---|
| T10 | Printable text / UTF-8 | Text, CJK and emoji are sent byte-exactly; split UTF-8 frames are reassembled | Type Chinese/emoji at a shell prompt | P0 | TODO |
| T11 | Enter / Backspace / Tab / Shift+Tab | `\r`, `0x7f`, `\t`, `ESC[Z` reach the shell | Shell completion and `cat -v` | P0 | TODO |
| T12 | Arrow keys, normal mode | `ESC[A`/`B`/`C`/`D` | `cat -v`, press arrows | P0 | TODO |
| T13 | Arrow keys, application mode (DECCKM) | `ESCOA`/`B`/`C`/`D` after `ESC[?1h` | Open `vim`/`less`, move the cursor | P0 | TODO |
| T14 | Home/End/Delete/Insert/PageUp/PageDown | Correct CSI sequences; PageUp/PageDown reach full-screen apps | `cat -v`, press each key | P0 | TODO |
| T15 | F1–F12 and modifiers | xterm-compatible sequences (with modifier parameters) | `cat -v` / `showkey` if available | P1 | TODO |
| T16 | Alt/Meta and Ctrl combinations | Alt sends ESC-prefix; Ctrl+letter sends control bytes; Ctrl+Space sends NUL | `cat -v` | P1 | TODO |
| T17 | Ctrl+C / Ctrl+D / Ctrl+Z | Control bytes reach the remote; Ctrl+C still copies when a selection exists (T34) | Run `sleep 100`, send Ctrl+C; test `logout` with Ctrl+D | P0 | TODO |
| T18 | Keypad / application keypad | Numeric keypad works in `vim`/`mc` | Test if the target uses it | P2 | TODO |

## 3. Rendering, colours and Unicode

| ID | Capability | Target behaviour | How to verify | Pri | Status |
|---|---|---|---|---|---|
| T20 | 16-colour ANSI | All 8 normal + 8 bright colours match the selected scheme | `printf` colour swatch (§4) | P0 | TODO |
| T21 | 256-colour | `ESC[38;5;n m` / `48;5;n` render | `printf` swatch | P0 | TODO |
| T22 | 24-bit truecolor | `ESC[38;2;r;g;b m` renders exact RGB | `printf` swatch | P0 | TODO |
| T23 | Bold / dim / italic / underline / reverse / strike | Attributes render and reset correctly | `printf` attribute line | P1 | TODO |
| T24 | Wide CJK / emoji / combining marks | Correct cell widths; no overlap or dropped glyphs | `printf '中文 😀 é\n'`; `ls` in a CJK directory | P0 | TODO |
| T25 | Cursor shape/position | Cursor tracks the remote cursor; no text-caret artefacts | Run `vim`/`htop`; type at a prompt | P0 | TODO |
| T26 | Alternate screen | Enter/exit restores the normal screen and scrollback | `vim`, `less`, `htop`; `ESC[?1049h/l` | P0 | TODO |
| T27 | Full-screen redraw correctness | `vim`, `htop`, `tmux`, `less`, `top` render without corruption | Run each program, resize/scroll | P0 | TODO |
| T28 | Scroll regions / insert-delete lines | Full-screen apps do not leave artefacts | Use `tmux` split panes and `vim` splits | P1 | TODO |
| T29 | Title / bell (optional) | OSC title/bell are ignored or surfaced safely | `printf '\033]0;title\a'`; bell | P2 | TODO |

## 4. Scrollback, selection and clipboard

| ID | Capability | Target behaviour | How to verify | Pri | Status |
|---|---|---|---|---|---|
| T30 | Scrollback capacity | 20,000 lines (configurable) retained; old lines drop cleanly | `seq 1 50000`, scroll to top | P0 | TODO |
| T31 | Wheel scrolling | Wheel scrolls history; follow mode stops while scrolled up | `seq 1 10000`, wheel up/down | P0 | TODO |
| T32 | Scrollbar | Drag/scrollbar reflects history and viewport | Same as T31 | P0 | TODO |
| T33 | Shift+PageUp/PageDown | Scroll history locally; plain PageUp/PageDown go remote | Test in shell and in `less` | P0 | TODO |
| T34 | Follow-on-output | New output appends without jumping when scrolled up; returning to bottom follows again | Scroll up while `yes` runs, then return | P0 | TODO |
| T35 | Mouse selection | Drag selects; double-click selects a word; selection can span wrapped lines | Select and copy text | P0 | TODO |
| T36 | Copy | Ctrl+Shift+C, Ctrl+Insert, context menu; selection goes to the Qt clipboard | Paste into Notepad | P0 | TODO |
| T37 | Ctrl+C semantics | With a selection: copy and do not interrupt; without: send `0x03` | Run `sleep 100` with and without a selection | P0 | TODO |
| T38 | Paste | Ctrl+Shift+V, Ctrl+V, Shift+Insert, context menu; bracketed paste when enabled | Paste multi-line text into `cat` and `vim` | P0 | TODO |
| T39 | Select all | Selects the visible buffer/history per xterm.js semantics | Context menu → Select all | P1 | TODO |

## 5. PC UI behaviour

| ID | Capability | Target behaviour | How to verify | Pri | Status |
|---|---|---|---|---|---|
| T40 | Font size persistence | Ctrl+wheel / Ctrl+= / Ctrl+- / Ctrl+0; value survives restart | Change size, restart the app | P0 | TODO |
| T41 | Terminal colour schemes | Light/dark menu switches bg/fg + full ANSI palette; persisted | View → Terminal colors; restart | P0 | TODO |
| T42 | Full screen | F11/button hides chrome; Escape exits only when detached | Toggle with and without a session | P0 | TODO |
| T43 | Focus after attach | Typing works immediately after Attach/click | Attach and type without clicking | P0 | TODO |
| T44 | Context menu | Native menu with Copy/Paste/Select all/Zoom actions; correct enabled states | Right-click with/without a selection | P1 | TODO |
| T45 | Fixed 120×30 | Window resize never changes the remote PTY or column count; no reflow | Resize the window, run `stty size` remotely | P0 | TODO |
| T46 | Narrow window / zoom | Horizontal scroll (or agreed minimum-font shrink) at large fonts | Maximize font, shrink window to ~800 px | P1 | TODO |
| T47 | IME composition | Chinese/Japanese composition window positions at the cursor; committed text is sent once | Type Chinese in the terminal input method | P0 | TODO |
| T48 | High-DPI | 100%/125%/150% scaling renders correctly | Change Windows scaling; restart | P1 | TODO |
| T49 | Drag/drop and window title | No accidental navigation; title unchanged or deliberately mapped | Drag a file onto the terminal | P2 | TODO |

## 6. Robustness, security and packaging

| ID | Capability | Target behaviour | How to verify | Pri | Status |
|---|---|---|---|---|---|
| T50 | Large output | `cat` 10 MB and 100k lines; UI responsive; bounded memory | Run the commands in §4 | P0 | TODO |
| T51 | Backpressure | Python queue ≤4 MB; in-flight JS chunk ≤64 KB; no dropped or reordered output | Instrument the bridge during T50 | P0 | TODO |
| T52 | Split frames | Multi-byte UTF-8 and escape sequences split across USB frames render correctly | Large output with CJK; synthetic split feed test | P0 | TODO |
| T53 | Disconnect/reconnect | Web view does not crash; attach works again after reconnect | Pull the USB cable, reconnect, attach | P1 | TODO |
| T54 | Output safety | Terminal-looking HTML/JS/OSC 8 is rendered as text; no navigation or script execution | Feed `<img src=x onerror=...>`, OSC 8 link | P0 | TODO |
| T55 | Navigation lockdown | Any navigation attempt is denied; no remote URLs are loaded | Inspect `acceptNavigationRequest`; click a link-like string | P0 | TODO |
| T56 | Offscreen smoke test | Page loads and `term.write` works with `QT_QPA_PLATFORM=offscreen` | Run the automated test | P1 | TODO |
| T57 | Packaged app | Clean Windows machine without Python; terminal works | Run `dist/USBBridge/USBBridge.exe` | P0 | TODO |
| T58 | Size/startup/memory | Meets G2 budgets (record values) | Measure dist size, cold start, RSS | P0 | TODO |
| T59 | Linux/macOS smoke | Import + page load + basic terminal I/O | CI or VM | P1 | TODO |
| T60 | Feature-flag rollback | `USBBRIDGE_TERMINAL_ENGINE=pyte` restores the old widget | Run with the env var | P1 | TODO |

## 7. Manual test commands

Run these on the remote target through the app. Record pass/fail next to the
checklist IDs.

```sh
# 16 colours (T20)
for i in 30 31 32 33 34 35 36 37; do printf "\033[${i}m%s\033[0m " "$i"; done; echo
for i in 90 91 92 93 94 95 96 97; do printf "\033[${i}m%s\033[0m " "$i"; done; echo

# 256 and truecolor (T21, T22)
printf '\033[38;5;208m256-colour\033[0m\n'
printf '\033[38;2;12;200;99mtruecolour\033[0m\n'
printf '\033[48;2;40;40;120m\033[38;2;255;255;255m block \033[0m\n'

# attributes (T23)
printf '\033[1mbold\033[0m \033[2mdim\033[0m \033[3mitalic\033[0m \033[4munderline\033[0m \033[7mreverse\033[0m \033[9mstrike\033[0m\n'

# wide characters and combining (T24)
printf '中文 😀 é\n'

# alternate screen (T26)
printf '\033[?1049hALT-SCREEN\033[?1049l\n'

# bracketed paste (T38): enable, then paste multi-line text at a shell prompt or in vim
printf '\033[?2004h'

# scrolling / flood (T30–T34, T50)
seq 1 50000
yes | head -n 100000
cat /path/to/a/10MB/file

# application cursor mode (T13)
vim      # move with arrows; :q
less /etc/services   # arrows, PageUp/PageDown; q
htop     # arrows/scroll; q
```

Also test:

- Chinese IME composition in the terminal prompt (T47).
- Copy a multi-line selection to Notepad, and paste multi-line text into `cat`
  (T36, T38).
- Resize the app window and run `stty size` remotely (T45).
- Reattach after generating output while detached (T02, T05).
- Pull the USB cable, reconnect, attach (T53).
- Run the app at 125% and 150% Windows scaling (T48).
- Run the packaged build on a machine without Python (T57).

## 8. Sign-off

| Gate | Criteria | Evidence | Result |
|---|---|---|---|
| G1 | P0 spike passes on Qt 5.15.2 | §0 table | |
| G2 | Size/startup/memory within budget | T58 measurements | |
| G3 | Throughput/latency within budget | T50/T51 measurements | |
| G4 | Security lockdown verified | T54/T55 | |
| G5 | All P0/P1 checklist items pass | This file | |
| G6 | Packaged app + cross-platform smoke | T57/T59 | |
| G7 | Feature flag flipped; pyte rollback works | T60 | |

Approver: ____________________  Date: ____________
