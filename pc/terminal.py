"""Public terminal widget: native Qt cell view backed by pyte.

`ui.py` imports ``TerminalWidget`` from here. The previous QPlainTextEdit
implementation is kept in ``terminal_qplain.py`` for rollback/reference.
"""
from terminal_view import TerminalWidget

__all__ = ["TerminalWidget"]
