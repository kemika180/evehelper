"""Colour themes for the TUI.

kemika-purple mirrors the theme from the audiobook_scripts project. Users can pick
any registered theme (this one plus Textual's built-ins) via config or the command
palette (ctrl+p → "Change theme").
"""

from textual.theme import Theme

KEMIKA_PURPLE = Theme(
    name="kemika-purple",
    primary="#5c3b8a",
    secondary="#2f2c4a",
    background="#0f0f15",
    surface="#141320",
    panel="#12121a",
    foreground="#e2e2e9",
    accent="#cbb2ff",
    boost="#1e1d32",
    success="#b53580",
    variables={
        "scrollbar": "#5c3b8a",
        "scrollbar-hover": "#7b52ab",
        "scrollbar-active": "#cbb2ff",
        "scrollbar-background": "#0d0c15",
        "scrollbar-background-hover": "#0d0c15",
        "scrollbar-background-active": "#0d0c15",
        "scrollbar-corner-color": "#0d0c15",
    },
    dark=True,
)
