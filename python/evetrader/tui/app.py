"""Textual application and the ``evetrader`` console entry point.

Milestone 1 is an empty shell: it launches, shows a placeholder, and quits. Later
milestones add the refresh loop (driven by ESI cache expiry, never by keystrokes)
and the opportunity widgets.
"""

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.widgets import Footer, Header, Static

_PLACEHOLDER = "evetrader — no opportunities yet (milestone 1 scaffold)."


class EveTraderApp(App[None]):
    """The evetrader TUI. Advises trades; it never executes them."""

    TITLE = "evetrader"
    BINDINGS: ClassVar[list[BindingType]] = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(_PLACEHOLDER, id="placeholder")
        yield Footer()


def main() -> None:
    """Console-script entry point for ``evetrader``."""
    EveTraderApp().run()
