"""The evetrader TUI: lists ranked opportunities plus a character and skill-queue
panel, refreshed on an interval.

Refresh is driven by a timer, never by a keystroke; the injected ``refresh_fn``
(the pipeline in production) fetches through the cached client, so a tick only
touches the network when a resource is actually stale.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.widgets import DataTable, Footer, Header, Static

from evetrader.pipeline import AdvisorReport

RefreshFn = Callable[[], Awaitable[AdvisorReport]]


class EveTraderApp(App[None]):
    """Advises trades; never executes them."""

    TITLE = "evetrader"
    BINDINGS: ClassVar[list[BindingType]] = [("q", "quit", "Quit")]

    def __init__(self, refresh_fn: RefreshFn, interval_seconds: int) -> None:
        super().__init__()
        self._refresh_fn = refresh_fn
        self._interval = interval_seconds

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Fetching market data…", id="status")
        yield Static("", id="character")
        yield DataTable(id="opportunities")
        yield Static("", id="skillqueue")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#opportunities", DataTable)
        table.add_columns("Type", "Buy", "Sell", "Qty", "Capital", "ISK/hr", "Notes")
        # Run in a worker so the UI paints immediately instead of blocking on the fetch.
        self.run_worker(self._refresh(), exclusive=True)
        self.set_interval(self._interval, self._refresh)

    async def _refresh(self) -> None:
        status = self.query_one("#status", Static)
        status.update("Fetching market data…")
        try:
            report = await self._refresh_fn()
        except Exception as error:  # surface it instead of a blank screen
            status.update(f"[Refresh failed] {type(error).__name__}: {error}")
            return
        self._render_character(report)
        self._render_opportunities(report)
        self._render_skill_queue(report)
        count = len(report.opportunities)
        status.update(f"{count} opportunit{'y' if count == 1 else 'ies'} — updated")

    def _render_character(self, report: AdvisorReport) -> None:
        character = report.character
        summary = (
            f"Station {character.station_id}  |  "
            f"Wallet {character.wallet_balance:,.0f} ISK  |  "
            f"Free order slots {character.free_order_slots}  |  "
            f"Sales tax {character.fees.sales_tax:.2%}  Broker {character.fees.broker_fee:.2%}"
        )
        self.query_one("#character", Static).update(summary)

    def _render_opportunities(self, report: AdvisorReport) -> None:
        table = self.query_one("#opportunities", DataTable)
        table.clear()
        for opportunity in report.opportunities:
            name = report.names.get(opportunity.type_id, str(opportunity.type_id))
            table.add_row(
                name,
                f"{opportunity.buy_price:,.2f}",
                f"{opportunity.sell_price:,.2f}",
                str(opportunity.quantity),
                f"{opportunity.capital_required:,.0f}",
                f"{opportunity.expected_isk_per_hour:,.0f}",
                opportunity.reasoning,
            )

    def _render_skill_queue(self, report: AdvisorReport) -> None:
        if not report.skill_queue:
            self.query_one("#skillqueue", Static).update("Skill queue: empty")
            return
        lines = ["Skill queue:"]
        for entry in report.skill_queue:
            name = report.names.get(entry.skill_id, str(entry.skill_id))
            finish = entry.finish_date.strftime("%Y-%m-%d %H:%M") if entry.finish_date else "?"
            lines.append(f"  {entry.queue_position + 1}. {name} L{entry.finished_level} → {finish}")
        self.query_one("#skillqueue", Static).update("\n".join(lines))
