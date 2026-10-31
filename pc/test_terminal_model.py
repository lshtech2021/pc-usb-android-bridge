"""Headless tests for the pyte terminal model (no Qt required)."""
import pyte

from terminal_model import (
    DualScreen,
    FLAG_BOLD,
    normalize_color,
    private_mode,
    row_runs,
)


def feed(screen, data: bytes) -> None:
    pyte.ByteStream(screen).feed(data)


def line_text(screen, y: int) -> str:
    return "".join(screen.buffer[y][x].data for x in range(screen.columns)).rstrip()


def row_text(row, columns: int) -> str:
    return "".join(row[x].data for x in range(columns)).rstrip()


def test_private_modes():
    screen = DualScreen(20, 4)
    assert not private_mode(screen, 1)
    feed(screen, b"\x1b[?1h")
    assert private_mode(screen, 1)
    feed(screen, b"\x1b[?1l")
    assert not private_mode(screen, 1)
    feed(screen, b"\x1b[?2004h")
    assert private_mode(screen, 2004)


def test_colours_and_runs():
    screen = DualScreen(20, 4)
    feed(screen, b"\x1b[1;31mRED\x1b[0m plain")
    runs = row_runs(screen.buffer[0], 20)
    assert runs[0].text == "RED"
    assert runs[0].style.fg == "red"
    assert runs[0].style.bold
    assert runs[0].style.flags() & FLAG_BOLD
    assert runs[0].cells == 3
    assert runs[1].text.startswith(" plain")
    assert runs[1].style.fg == "default"


def test_wide_character_cell_count():
    screen = DualScreen(12, 3)
    feed(screen, b"\x1b[31ma" + "中".encode("utf-8") + b"b\x1b[0m")
    runs = row_runs(screen.buffer[0], 12)
    # a + 中 (2 cells) + b = 4 cells; text has three characters.
    assert runs[0].text == "a中b"
    assert runs[0].cells == 4


def test_truecolor_and_256_colours():
    screen = DualScreen(20, 4)
    feed(screen, b"\x1b[38;5;208mA\x1b[0m\x1b[38;2;1;2;3mB\x1b[0m")
    assert screen.buffer[0][0].fg == "ff8700"
    assert normalize_color(screen.buffer[0][0].fg) == "#ff8700"
    assert screen.buffer[0][1].fg == "010203"
    assert normalize_color(screen.buffer[0][1].fg) == "#010203"


def test_pyte_colour_quirks():
    assert normalize_color("brown") == "yellow"
    assert normalize_color("bfightmagenta") == "brightmagenta"
    assert normalize_color("default") == "default"
    assert normalize_color("brightred") == "brightred"


def test_alt_screen_roundtrip():
    screen = DualScreen(20, 4)
    feed(screen, b"NORMAL")
    feed(screen, b"\x1b[?1049h")
    assert screen.alt_active
    feed(screen, b"ALT")
    assert line_text(screen, 0) == "ALT"
    assert line_text(screen.normal, 0) == "NORMAL"
    feed(screen, b"\x1b[?1049l")
    assert not screen.alt_active
    assert line_text(screen, 0) == "NORMAL"


def test_alt_screen_does_not_pollute_history():
    screen = DualScreen(10, 3, history=8)
    for i in range(6):
        feed(screen, ("line%d\r\n" % i).encode())
    before = [row_text(r, screen.columns) for r in screen.normal.history.top]
    feed(screen, b"\x1b[?1049h")
    for i in range(6):
        feed(screen, ("alt%d\r\n" % i).encode())
    feed(screen, b"\x1b[?1049l")
    after = [row_text(r, screen.columns) for r in screen.normal.history.top]
    assert before == after


def test_1047_clears_alt_on_exit():
    screen = DualScreen(20, 4)
    feed(screen, b"\x1b[?1047h")
    feed(screen, b"ALT")
    assert line_text(screen, 0) == "ALT"
    feed(screen, b"\x1b[?1047l")
    assert not screen.alt_active
    # Re-enter without clearing: the alt buffer should be blank.
    feed(screen, b"\x1b[?1047h")
    assert line_text(screen, 0) == ""


def test_deccolm_does_not_resize():
    screen = DualScreen(20, 4)
    feed(screen, b"\x1b[?3h")
    assert screen.columns == 20
    assert screen.normal.columns == 20
    assert screen.alt.columns == 20


def test_history_spill():
    screen = DualScreen(10, 3, history=2)
    for i in range(6):
        feed(screen, ("L%d\r\n" % i).encode())
    history = [row_text(r, screen.columns) for r in screen.history.top]
    assert history == ["L2", "L3"]


def test_split_utf8():
    screen = DualScreen(20, 4)
    data = "中文".encode("utf-8")
    stream = pyte.ByteStream(screen)
    stream.feed(data[:2])
    stream.feed(data[2:])
    assert line_text(screen, 0) == "中文"


def test_reset_clears_both_buffers():
    screen = DualScreen(20, 4)
    feed(screen, b"NORMAL\x1b[?1049hALT")
    screen.reset()
    assert not screen.alt_active
    assert line_text(screen, 0) == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("terminal model tests passed")
