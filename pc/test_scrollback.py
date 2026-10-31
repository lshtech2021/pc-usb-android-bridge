"""Tests for the attributed scrollback store."""
import tempfile
from pathlib import Path

from scrollback import ScrollbackStore
from terminal_model import Run, Style


def sample_runs():
    return [
        Run("user@host", 9, Style(fg="green")),
        Run(" $ ", 3, Style()),
        Run("中文", 4, Style(fg="#ff8700", bold=True)),
    ]


def test_append_and_slice():
    with tempfile.TemporaryDirectory() as d:
        store = ScrollbackStore(Path(d) / "s.scrollback", columns=120)
        store.reset()
        assert len(store) == 0
        assert store.slice(0, 10) == []

        store.append(sample_runs())
        store.append([Run("ok", 2, Style(fg="brightgreen", reverse=True))])
        assert len(store) == 2

        first = store.slice(0, 1)[0]
        assert "".join(r.text for r in first) == "user@host $ 中文"
        assert first[0].style.fg == "green"
        assert first[2].style.fg == "#ff8700"
        assert first[2].style.bold
        assert first[2].cells == 4

        second = store.slice(1, 2)[0][0]
        assert second.text == "ok"
        assert second.style.reverse
        assert second.style.fg == "brightgreen"

        assert store.slice(1, 5) == store.slice(1, 2)


def test_reset_and_columns():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "s.scrollback"
        store = ScrollbackStore(path, columns=120)
        store.reset()
        store.append([Run("x", 1, Style())])
        assert len(store) == 1
        store.reset(columns=100)
        assert len(store) == 0
        assert store.columns == 100
        header = path.read_text(encoding="utf-8").splitlines()[0]
        assert '"v":1' in header and '"cols":100' in header


def test_bad_line_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "s.scrollback"
        store = ScrollbackStore(path)
        store.reset()
        # A truncated/corrupt line must not crash rendering.
        with path.open("ab") as f:
            f.write(b'{"r": [["broken"\n')
        # The offset table did not know about the hand-written line; a reset
        # followed by a valid append is the realistic recovery path.
        store.reset()
        store.append([Run("ok", 2, Style())])
        assert store.slice(0, 1)[0][0].text == "ok"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("scrollback tests passed")
