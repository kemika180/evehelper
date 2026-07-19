"""The TUI mounts headlessly, shows the placeholder, and quits cleanly."""

import asyncio

from textual.widgets import Static

from evetrader.tui.app import EveTraderApp


def test_app_mounts_and_quits() -> None:
    async def _drive() -> None:
        app = EveTraderApp()
        async with app.run_test() as pilot:
            assert app.query_one("#placeholder", Static) is not None
            await pilot.press("q")

    asyncio.run(_drive())
