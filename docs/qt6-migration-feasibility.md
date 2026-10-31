# Qt 5.15 → Qt 6 migration feasibility

> **Decision (2026-10-31):** approved. Migrate to Qt 6 **before** the terminal
> view rewrite. The assessment below is the implementation reference; the
> terminal work in [`terminal-fix-plan.md`](./terminal-fix-plan.md) starts
> after this phase.

**Status:** Approved — first phase of the terminal programme  
**Date:** 2026-10-31  
**Scope:** PC client (`pc/`), PyQt5 5.15.11 / Qt 5.15.2 → PyQt6 / Qt 6.x  
**Related:** [`terminal-fix-plan.md`](./terminal-fix-plan.md)

## 0. Verdict

**Feasible, low risk, no blockers.** The PC client uses a small, conventional
subset of Qt. The migration is mostly mechanical enum/import work plus a
packaging pass: **4–6 person-days** including QA and a clean-machine build.

Qt 6 does **not** fix the terminal defects by itself. It is a platform
upgrade; the terminal fix is independent. The approved order is:

1. Migrate to Qt 6 first (this document), so the new terminal renderer is
   built and tested against the target platform once.
2. Then implement the terminal fix in
   [`terminal-fix-plan.md`](./terminal-fix-plan.md).

Keep the migration in its own branch/commit; do not mix it with the terminal
view rewrite.

## 1. Current stack

| Item | Current |
|---|---|
| Python | 3.13.9 |
| Qt bindings | PyQt5 5.15.11 |
| Qt runtime | PyQt5-Qt5 5.15.2 (Chromium 83 if WebEngine is added; not used) |
| Packaging | PyInstaller 6.22.3, `pc/usb_bridge.spec` (one-folder) |
| Platforms | Windows 10 primary; README also claims Linux/macOS |
| Other deps | pyte 0.8.2, cryptography 41+ (unchanged by Qt 6) |

## 2. What changes

### 2.1 Imports

`QAction`, `QActionGroup` and `QShortcut` moved from `QtWidgets` to `QtGui`
in Qt 6.

| File | Current import | Fix |
|---|---|---|
| `pc/terminal.py:13` | `from PyQt5.QtWidgets import ... QAction, QMenu` | `QAction` from `PyQt6.QtGui` |
| `pc/ui.py:11` | `QAction, QActionGroup` from `QtWidgets` | from `QtGui` |
| `pc/ui.py:12` | `QShortcut` from `QtWidgets` | from `QtGui` |

### 2.2 Scoped enums

PyQt6 exposes enums only through their scope classes. The codebase has about
50–60 sites; all are mechanical.

| Pattern | Occurrences (approx.) | Examples | Qt 6 form |
|---|---|---|---|
| `Qt.Align*` | 11 | `dialogs.py:115`, `theme.py:391`, `ui.py:423,425,770,918`, `widgets.py:39,67,95,170,183` | `Qt.AlignmentFlag.Align*` |
| `Qt.UserRole` | 4 | `ui.py:302,816,1074,1086` | `Qt.ItemDataRole.UserRole` |
| `Qt.TextWordWrap` | 1 | `widgets.py:130` | `Qt.TextFlag.TextWordWrap` |
| `QPalette.*` | 4 | `terminal.py:79-82` | `QPalette.ColorRole.*` |
| `QFont.Monospace` | 1 | `terminal.py:90` | `QFont.StyleHint.Monospace` |
| `QTextCursor.*` | 4 | `terminal.py:272,278,280-281` | `QTextCursor.MoveOperation.*` / `MoveMode.KeepAnchor` |
| `QKeySequence.Copy/Paste` | 2 | `terminal.py:411,415` | `QKeySequence.StandardKey.*` |
| `QLineEdit.Password/Normal` | 2 | `dialogs.py:69,197` | `QLineEdit.EchoMode.*` |
| `QSizePolicy.*` | 1 | `theme.py:326` | `QSizePolicy.Policy.*` |
| `QFrame.NoFrame` | 2 | `theme.py:378`, `widgets.py:28` | `QFrame.Shape.NoFrame` |
| `QHeaderView.*` | 10 | `ui.py:967-969,982-985`, `widgets.py:173-177` | `QHeaderView.ResizeMode.*` |
| `QMessageBox.Yes` | 3 | `ui.py:667,1102,1144` | `QMessageBox.StandardButton.Yes` |

`terminal.py` is being rewritten by the fix plan, so its enum sites disappear;
the remaining migration is smaller than the table suggests. A quick sweep:

```sh
grep -rnE 'Qt\.(Align|UserRole|TextWordWrap|AA_)|QPalette\.|QFont\.Monospace|QTextCursor\.|QKeySequence\.(Copy|Paste)|QLineEdit\.(Password|Normal)|QSizePolicy\.|QFrame\.NoFrame|QHeaderView\.|QMessageBox\.Yes|\.exec_\(' pc/
```

### 2.3 Removed/deprecated APIs

| Item | Location | Fix |
|---|---|---|
| `QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)` | `ui.py:1267` | Remove; Qt 6 enables high-DPI scaling by default |
| `QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)` | `ui.py:1268` | Remove; always on in Qt 6 |
| `menu.exec_(...)` | `terminal.py:317` | `menu.exec(...)` |
| `app.exec_()` | `ui.py:1273` | `app.exec()` |

### 2.4 APIs checked and **not** used

- `QDesktopWidget` (removed in Qt 6) — not used.
- `QRegExp` / `QTextCodec` — not used.
- `QMouseEvent.pos()` / `QEnterEvent` overrides — not used.
- `QFontMetrics.width()` — not used; the `.width()` calls in `widgets.py:120-130`
  are `QRect.width()`, which is unchanged.
- `QWebEngine` / `QWebChannel` / `QWebSockets` — not used (and excluded in the
  current spec).
- `QVariant` conversions, model/view subclasses, custom style classes — not
  used.

### 2.5 What does not change

- Signal/slot syntax, `pyqtSignal`, `QTimer`, `QSettings`, layouts, `QPainter`,
  `QFontMetricsF`, `QColor`, `QPixmap`, `QClipboard`.
- The protocol, Android app, pyte, cryptography and the terminal widget API.
- The QSS style sheet is largely compatible; verify dialogs, tabs, tables and
  scrollbars visually.

## 3. Scoped enums work on PyQt5 5.15.11

A useful de-risking fact: PyQt5 5.15.11 already accepts the Qt 6 scoped forms
(verified in this environment):

```python
Qt.AlignmentFlag.AlignLeft
Qt.ItemDataRole.UserRole
QPalette.ColorRole.Base
QFont.StyleHint.Monospace
QTextCursor.MoveOperation.Start
QKeySequence.StandardKey.Copy
QLineEdit.EchoMode.Password
QSizePolicy.Policy.Maximum
QFrame.Shape.NoFrame
QHeaderView.ResizeMode.Stretch
QMessageBox.StandardButton.Yes
```

Therefore the new terminal code can be written in the Qt 6 style while the
app is still on PyQt5, avoiding a second conversion pass. The existing files
can also be converted incrementally if the team prefers a gradual migration.

## 4. Packaging

- `pc/requirements.txt`: replace `PyQt5>=5.15` with a pinned `PyQt6` version.
  Candidate baselines: **PyQt6 6.8.1** (Qt 6.8 LTS, conservative) or
  **PyQt6 6.12.0** (latest, more features). Verify the wheels exist for
  Python 3.13 on every target, then pin the chosen version.
- `pc/usb_bridge.spec`: change the PyQt module names from `PyQt5.*` to
  `PyQt6.*`; keep the same excludes (no WebEngine). PyInstaller 6.22.3 has
  PyQt6 hooks; verify `Qt6Core`, `Qt6Gui`, `Qt6Widgets`, platform plugins and
  translations are collected.
- Clean-machine test on Windows 10 without Python; Linux/macOS smoke builds.
- Qt 6 platform minimums: Windows 10 1809+, macOS 12+ (check the target
  matrix), Linux glibc 2.28+. Confirm the README's supported platforms before
  release.
- No new runtime dependency beyond the Qt binding; the native terminal fix does
  not add any.

## 5. Test plan

1. **Import/compile sweep:** start the app, open every tab and dialog, run the
   offscreen smoke tests. PyQt6 fails loudly on unscoped enums, so this finds
   most issues.
2. **UI matrix:** connection dialog, messages, files table, remote terminal,
   SFTP explorer, menus, shortcuts, dark/light palettes, full screen.
3. **High DPI:** 100%/125%/150% scaling; Qt 6 enables scaling by default, so
   verify fonts, icons, table headers, terminal cell metrics.
4. **Persistence:** window geometry, tab index, device, font size, terminal
   theme, proxy settings survive restart (QSettings keys unchanged).
5. **Terminal:** run the fix-plan acceptance matrix on the migrated build.
6. **Packaging:** clean Windows machine, one-folder build, no Python.
7. **Cross-platform:** at least import + page load + basic terminal I/O on
   Linux/macOS.

## 6. Effort and risk

| Task | Estimate |
|---|---|
| Mechanical import/enum/API conversion (~50–60 sites) | 1.5–2.5 d |
| Qt 6 environment, requirements, spec, first build | 0.5–1 d |
| QA: UI matrix, DPI, persistence, terminal, packaging | 2–3 d |
| **Total** | **4–6 d** |

| Risk | Likelihood | Mitigation |
|---|---|---|
| Missed unscoped enum → import/runtime error | Medium | App start + offscreen smoke catches most; grep for `Qt\.`, `QPalette\.`, etc. |
| QSS styling differences | Low–medium | Visual matrix; adjust selectors/properties |
| Font metrics/DPI differences | Medium | Test 100/125/150%; the terminal computes metrics dynamically |
| PyInstaller/Qt 6 plugin packaging | Low | Clean-machine build; PyInstaller 6.x PyQt6 hooks |
| `QAction`/`QShortcut` import mistakes | High but trivial | Mechanical fix; compile/run |
| macOS/Linux minimum versions | Low | Document supported versions; CI smoke |
| PyQt6 licensing | None new | Same GPL/commercial model as PyQt5; PySide6 is an alternative but a larger binding migration |

## 7. Recommendation

- **Proceed with Qt 6 first**, as a separate 4–6 day task. It removes the
  aging Qt 5.15.2 runtime, improves high-DPI/font behaviour, and keeps the
  door open for future Qt 6 features (including a modern WebEngine if the
  terminal decision is ever revisited).
- **Do it before the terminal view rewrite** (approved). The renderer is
  Qt-heavy, and testing it twice is avoidable.
- Do not combine the Qt 6 migration and the terminal rewrite in one commit:
  keep the mechanical migration reviewable and reversible.
- Keep the PyQt5 build available as a rollback tag until the Qt 6 app passes
  the manual matrix and the packaged-build check.

## 8. References

- Qt 6 porting guide: <https://doc.qt.io/qt-6/porting-guide.html>
- PyQt6 differences from PyQt5:
  <https://www.riverbankcomputing.com/static/Docs/PyQt6/pyqt5_differences.html>
- Qt 6 high-DPI:
  <https://doc.qt.io/qt-6/highdpi.html>
- PyInstaller Qt 6 hooks:
  <https://pyinstaller.org/en/stable/hooks.html>

## 9. Migration log (2026-10-31)

Completed in the working tree:

- Installed **PyQt6 6.8.1 / PyQt6-Qt6 6.8.2 / PyQt6-sip 13.13.0** on Python
  3.13.9.
- Updated `pc/requirements.txt` to `PyQt6==6.8.1`.
- Converted all `pc/*.py` imports and enum access:
  - `QAction` / `QActionGroup` / `QShortcut` now from `PyQt6.QtGui`.
  - Scoped `Qt.*`, `QPalette.*`, `QTextCursor.*`, `QKeySequence.*`,
    `QLineEdit.*`, `QSizePolicy.*`, `QFrame.*`, `QHeaderView.*`,
    `QMessageBox.*`, `QEvent.*`, `QPlainTextEdit.*`, `QAbstractItemView.*`,
    `QPainter.*` forms.
  - Removed the Qt 5 high-DPI `setAttribute` calls; `exec_()` → `exec()`.
- Updated `pc/usb_bridge.spec` excludes to PyQt6; README/PRD/PROTOCOL now say
  PyQt6 and Python 3.9+.
- Verified:
  - `import ui` and `python -m compileall` pass.
  - Offscreen `MainWindow` with all four tabs, `ConnectionDialog`, terminal
    feed/zoom/key handling/context-menu construction, `MessageLog`,
    `FileDropTable`, theme switching and `state_dot_icon` pass.
  - Existing protocol tests `test_text_receive.py`, `test_fs_explorer.py`
    (one flaky temp-file cleanup on first run, passes on rerun) and
    `test_proxy_tether.py` pass.
  - PyInstaller one-folder build succeeds: `pc/dist/USBBridge` is ~97 MB
    (smaller than the previous 105 MB PyQt5 build); the packaged executable
    starts offscreen.

Pending before this phase is signed off:

- Visual QA on a real display at 100%/125%/150% DPI (the offscreen harness
  has no usable fonts).
- Clean-machine test of the packaged build.
- Linux/macOS smoke builds.
- Final removal of the PyQt5 packages from the build environment (they can
  coexist with PyQt6 for the rollback tag).

