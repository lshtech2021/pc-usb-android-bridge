"""Design tokens, the Qt style sheet, and shared presentational widgets.

Everything visual pulls from here so surfaces stay consistent instead of
hard-coding colours and paddings per widget.
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

# ---- Spacing scale (px) ----
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24

# ---- Palette ----
SURFACE = "#FFFFFF"
CANVAS = "#F5F6F8"
BORDER = "#D8DCE3"
BORDER_SOFT = "#EDEFF2"
TEXT = "#1F2430"
TEXT_MUTED = "#6B7280"
TEXT_FAINT = "#9096A1"

ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
ACCENT_PRESSED = "#1E40AF"

DOT_DISCONNECTED = "#9CA3AF"
DOT_CONNECTING = "#D97706"
DOT_CONNECTED = "#16A34A"
DOT_ERROR = "#DC2626"

MONO_STACK = "'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace"

# Fraction of the viewport a chat bubble may occupy.
BUBBLE_MAX_FRACTION = 0.72

BUBBLE_MINE_BG = "#E5EDFD"
BUBBLE_MINE_BORDER = "#CFDDF8"


def stylesheet() -> str:
    """Application-wide style sheet."""
    return f"""
QMainWindow, QDialog {{ background: {CANVAS}; }}

#root, #tabPage {{ background: {CANVAS}; }}

/* Header card */
#header {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

/* Field labels */
QLabel[role="fieldLabel"] {{ color: {TEXT_MUTED}; }}
QLabel[role="caption"] {{ color: {TEXT_MUTED}; font-size: 12px; }}
QLabel[role="mono"] {{ font-family: {MONO_STACK}; color: {TEXT_MUTED}; }}
QLabel[role="sectionTitle"] {{ color: {TEXT}; font-weight: 600; }}

QFrame[role="vsep"] {{ background: {BORDER_SOFT}; }}

/* Inputs */
QLineEdit, QComboBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    color: {TEXT};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ background: {CANVAS}; color: {TEXT_FAINT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}

/* Buttons */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: {CANVAS}; }}
QPushButton:pressed {{ background: {BORDER_SOFT}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {CANVAS}; border-color: {BORDER_SOFT}; }}

QPushButton[variant="primary"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[variant="primary"]:pressed {{ background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}
QPushButton[variant="primary"]:disabled {{
    background: #B9C6E8; border-color: #B9C6E8; color: #FFFFFF;
}}

QPushButton[variant="subtle"] {{ padding: 4px 8px; color: {TEXT_MUTED}; }}

/* Tabs */
QTabWidget::pane {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    border: 1px solid transparent;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 7px 16px;
    margin-right: 4px;
    color: {TEXT_MUTED};
}}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}
QTabBar::tab:selected {{
    background: {SURFACE};
    border-color: {BORDER};
    border-bottom-color: {SURFACE};
    color: {TEXT};
    font-weight: 600;
}}

/* Tables */
QTableWidget {{
    background: {SURFACE};
    border: none;
    gridline-color: {BORDER_SOFT};
    color: {TEXT};
    selection-background-color: #E5EDFD;
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {CANVAS};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    color: {TEXT_MUTED};
    font-weight: 600;
}}
QTableCornerButton::section {{ background: {CANVAS}; border: none; }}

/* Progress */
QProgressBar {{
    border: none;
    border-radius: 4px;
    background: {BORDER_SOFT};
    height: 8px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

/* Log / terminal surfaces */
QPlainTextEdit {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}

/* Message log and chat bubbles */
QScrollArea#messageLog {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QScrollArea#messageLog > QWidget > QWidget {{ background: transparent; }}
#messageBody {{ background: transparent; }}

#bubbleMine {{
    background: {BUBBLE_MINE_BG};
    border: 1px solid {BUBBLE_MINE_BORDER};
    border-radius: 10px;
}}
#bubbleTheirs {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#bubbleText {{ color: {TEXT}; }}
#bubbleTime {{ color: {TEXT_FAINT}; font-size: 11px; }}

QLabel[role="emptyState"], #fileEmpty {{ color: {TEXT_FAINT}; }}

/* Status chip */
#statusChip {{
    background: {CANVAS};
    border: 1px solid {BORDER};
    border-radius: 13px;
}}
#statusText {{ color: {TEXT}; font-weight: 600; }}
#statusDetail {{ color: {TEXT_MUTED}; }}

/* Scrollbars */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C7CCD4; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #AEB5C0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #C7CCD4; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


class StatusChip(QWidget):
    """Colour-coded connection state: a dot plus a label, optionally detailed."""

    STATES = {
        "disconnected": (DOT_DISCONNECTED, "Not connected"),
        "connecting": (DOT_CONNECTING, "Connecting…"),
        "connected": (DOT_CONNECTED, "Connected"),
        "error": (DOT_ERROR, "Connection error"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statusChip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(int(SPACE_M), int(SPACE_XS), int(SPACE_M), int(SPACE_XS))
        lay.setSpacing(int(SPACE_S))
        self._dot = QLabel()
        self._dot.setObjectName("statusDot")
        self._dot.setFixedSize(10, 10)
        self._detail = QLabel()
        self._detail.setObjectName("statusDetail")
        lay.addWidget(self._dot)
        lay.addWidget(self._detail)
        self.set_state("disconnected")

    def set_state(self, state: str, detail: str = ""):
        color, label = self.STATES.get(state, self.STATES["disconnected"])
        self._dot.setStyleSheet(
            f"#statusDot {{ background: {color}; border-radius: 5px; }}")
        self._detail.setText(label if not detail else f"{label} · {detail}")
        self.setToolTip(detail or label)


def vertical_separator() -> QWidget:
    """Thin rule used to group fields in the header row."""
    from PyQt5.QtWidgets import QFrame
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
