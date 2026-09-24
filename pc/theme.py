"""Design tokens, palettes, the Qt style sheet, and shared presentational widgets.

Everything visual pulls from here so surfaces stay consistent instead of
hard-coding colours and paddings per widget. Call set_palette() then
stylesheet() to switch between light and dark.
"""
from string import Template

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

# ---- Spacing scale (px) ----
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24

# Fraction of the viewport a chat bubble may occupy.
BUBBLE_MAX_FRACTION = 0.72

MONO_STACK = "'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace"

PALETTES = {
    "light": {
        "canvas": "#F5F6F8",
        "surface": "#FFFFFF",
        "border": "#D8DCE3",
        "border_soft": "#EDEFF2",
        "text": "#1F2430",
        "text_muted": "#6B7280",
        "text_faint": "#9096A1",
        "accent": "#2563EB",
        "accent_hover": "#1D4ED8",
        "accent_pressed": "#1E40AF",
        "accent_disabled": "#B9C6E8",
        "on_accent": "#FFFFFF",
        "selection": "#E5EDFD",
        "bubble_mine_bg": "#E5EDFD",
        "bubble_mine_border": "#CFDDF8",
        "progress_track": "#EDEFF2",
        "scroll_handle": "#C7CCD4",
        "scroll_handle_hover": "#AEB5C0",
        "dot_disconnected": "#9CA3AF",
        "dot_connecting": "#D97706",
        "dot_connected": "#16A34A",
        "dot_error": "#DC2626",
        "term_bg": "#1E1E1E",
        "term_fg": "#D4D4D4",
    },
    "dark": {
        "canvas": "#17191D",
        "surface": "#212429",
        "border": "#343841",
        "border_soft": "#2A2E35",
        "text": "#E6E8EC",
        "text_muted": "#A2A8B4",
        "text_faint": "#7C828E",
        "accent": "#4C8DFF",
        "accent_hover": "#6BA0FF",
        "accent_pressed": "#3A7AE0",
        "accent_disabled": "#38466B",
        "on_accent": "#0F1319",
        "selection": "#2B3A56",
        "bubble_mine_bg": "#2B4166",
        "bubble_mine_border": "#3A5588",
        "progress_track": "#2E323A",
        "scroll_handle": "#464C57",
        "scroll_handle_hover": "#5A6170",
        "dot_disconnected": "#6B7280",
        "dot_connecting": "#E0A33A",
        "dot_connected": "#34C759",
        "dot_error": "#F0524B",
        "term_bg": "#0E1013",
        "term_fg": "#D4D4D4",
    },
}

_active = "light"


def set_palette(name: str):
    global _active
    if name in PALETTES:
        _active = name


def palette_name() -> str:
    return _active


def palette() -> dict:
    return PALETTES[_active]


def color(key: str) -> str:
    return palette()[key]


_QSS = Template("""
QMainWindow, QDialog { background: $canvas; }

#root, #tabPage { background: $canvas; }

/* Header card */
#header {
    background: $surface;
    border: 1px solid $border;
    border-radius: 10px;
}

/* Text roles */
QLabel[role="fieldLabel"] { color: $text_muted; }
QLabel[role="caption"] { color: $text_muted; font-size: 12px; }
QLabel[role="mono"] { font-family: MONO; color: $text_muted; }
QLabel[role="sectionTitle"] { color: $text; font-weight: 600; }
QLabel[role="emptyState"], #fileEmpty { color: $text_faint; }

QFrame[role="vsep"] { background: $border_soft; }

/* Inputs */
QLineEdit, QComboBox {
    background: $surface;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 5px 8px;
    color: $text;
    selection-background-color: $accent;
    selection-color: $on_accent;
}
QLineEdit:focus, QComboBox:focus { border-color: $accent; }
QLineEdit:disabled, QComboBox:disabled { background: $canvas; color: $text_faint; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: $surface;
    border: 1px solid $border;
    color: $text;
    selection-background-color: $selection;
    selection-color: $text;
}

/* Buttons */
QPushButton {
    background: $surface;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 6px 14px;
    color: $text;
}
QPushButton:hover { background: $canvas; }
QPushButton:pressed { background: $border_soft; }
QPushButton:disabled { color: $text_faint; background: $canvas; border-color: $border_soft; }

QPushButton[variant="primary"] {
    background: $accent;
    border-color: $accent;
    color: $on_accent;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton[variant="primary"]:pressed { background: $accent_pressed; border-color: $accent_pressed; }
QPushButton[variant="primary"]:disabled {
    background: $accent_disabled; border-color: $accent_disabled; color: $on_accent;
}

QPushButton[variant="subtle"] { padding: 4px 8px; color: $text_muted; }

/* Tabs */
QTabWidget::pane {
    background: $surface;
    border: 1px solid $border;
    border-radius: 8px;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    border: 1px solid transparent;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 7px 16px;
    margin-right: 4px;
    color: $text_muted;
}
QTabBar::tab:hover:!selected { color: $text; }
QTabBar::tab:selected {
    background: $surface;
    border-color: $border;
    border-bottom-color: $surface;
    color: $text;
    font-weight: 600;
}

/* Tables */
QTableWidget {
    background: $surface;
    border: none;
    gridline-color: $border_soft;
    color: $text;
    selection-background-color: $selection;
    selection-color: $text;
}
QHeaderView::section {
    background: $canvas;
    border: none;
    border-bottom: 1px solid $border;
    padding: 6px 8px;
    color: $text_muted;
    font-weight: 600;
}
QTableCornerButton::section { background: $canvas; border: none; }

/* Progress */
QProgressBar {
    border: none;
    border-radius: 4px;
    background: $progress_track;
    height: 8px;
}
QProgressBar::chunk { background: $accent; border-radius: 4px; }

/* Message log and chat bubbles */
QScrollArea#messageLog {
    background: $surface;
    border: 1px solid $border;
    border-radius: 8px;
}
QScrollArea#messageLog > QWidget > QWidget { background: transparent; }
#messageBody { background: transparent; }

#bubbleMine {
    background: $bubble_mine_bg;
    border: 1px solid $bubble_mine_border;
    border-radius: 10px;
}
#bubbleTheirs {
    background: $surface;
    border: 1px solid $border;
    border-radius: 10px;
}
#bubbleText { color: $text; }
#bubbleTime { color: $text_faint; font-size: 11px; }

/* State pills */
#statePill {
    background: $canvas;
    border: 1px solid $border;
    border-radius: 13px;
}
#statePillText { color: $text; font-weight: 600; }
#statePillDetail { color: $text_muted; }

/* Menus and status bar */
QMenuBar { background: $canvas; color: $text; }
QMenuBar::item:selected { background: $border_soft; }
QMenu { background: $surface; border: 1px solid $border; color: $text; }
QMenu::item:selected { background: $selection; }
QMenu::separator { height: 1px; background: $border_soft; margin: 4px 8px; }
QStatusBar { background: $canvas; color: $text_muted; }
QStatusBar::item { border: none; }

/* Footer strip */
#strip { background: $surface; border: 1px solid $border; border-radius: 8px; }

/* Scrollbars */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: $scroll_handle_hover; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scroll_handle; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
""")


def stylesheet() -> str:
    """Application-wide style sheet for the active palette."""
    values = dict(palette())
    values["MONO"] = MONO_STACK
    return _QSS.substitute(values)


class StatePill(QWidget):
    """Colour-coded state: a dot plus a label, optionally with detail text.

    ``states`` maps a state name to (palette colour key, display label).
    """

    DEFAULT_STATES = {"default": ("dot_disconnected", "Unknown")}

    def __init__(self, states=None, initial="default", parent=None):
        super().__init__(parent)
        self.setObjectName("statePill")
        self._states = states or self.DEFAULT_STATES
        lay = QHBoxLayout(self)
        lay.setContentsMargins(int(SPACE_M), int(SPACE_XS), int(SPACE_M), int(SPACE_XS))
        lay.setSpacing(int(SPACE_S))
        self._dot = QLabel()
        self._dot.setObjectName("stateDot")
        self._dot.setFixedSize(10, 10)
        self._text = QLabel()
        self._text.setObjectName("statePillText")
        self._detail = QLabel()
        self._detail.setObjectName("statePillDetail")
        lay.addWidget(self._dot)
        lay.addWidget(self._text)
        lay.addWidget(self._detail)
        self._state = initial
        self._extra = ""
        self.set_state(initial)

    def set_state(self, state: str, detail: str = ""):
        self._state = state
        self._extra = detail
        self.apply()

    def apply(self):
        """Re-render in the current palette (call after a theme change)."""
        key, label = self._states.get(self._state, self._states.get("default"))
        self._dot.setStyleSheet(
            f"#stateDot {{ background: {color(key)}; border-radius: 5px; }}")
        self._text.setText(label)
        self._detail.setText(f"· {self._extra}" if self._extra else "")
        self.setToolTip(f"{label} · {self._extra}" if self._extra else label)


CONNECTION_STATES = {
    "disconnected": ("dot_disconnected", "Not connected"),
    "connecting": ("dot_connecting", "Connecting…"),
    "connected": ("dot_connected", "Connected"),
    "error": ("dot_error", "Connection error"),
}


def connection_pill() -> StatePill:
    return StatePill(CONNECTION_STATES, "disconnected")


def vertical_separator() -> QWidget:
    """Thin rule used to group fields in a row."""
    sep = QFrame()
    sep.setProperty("role", "vsep")
    sep.setFixedWidth(1)
    sep.setMinimumHeight(22)
    sep.setFrameShape(QFrame.NoFrame)
    return sep


def caption(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("role", "caption")
    return lbl


def field_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("role", "fieldLabel")
    lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
    return lbl
