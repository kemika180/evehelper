"""The evetrader TUI: a character picker, then the per-character advisor screen.

On launch you pick a set-up character (or add/remove one); selecting it opens the
trading screen. Refresh is timer-driven, never keystroke-driven; the injected
``refresh_fn`` fetches through the cached client, so a tick only touches the network
when a resource is stale.

The screens depend only on injected callables (built in cli.py), so the whole app
is testable without ESI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from evetrader.esi.auth import CharacterIdentity
from evetrader.pipeline import AdvisorReport
from evetrader.session import CharacterRecord, CharacterStore

RefreshFn = Callable[[], Awaitable[AdvisorReport]]
MakeRefreshFn = Callable[[int], RefreshFn]
LoginFn = Callable[[], Awaitable[CharacterIdentity]]
RemoveTokenFn = Callable[[int], None]


class TradingScreen(Screen[None]):
    """Per-character advisor view: opportunities plus character and skill-queue."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "app.pop_screen", "Switch character")]

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

    def on_mount(self) -> None:
        table = self.query_one("#opportunities", DataTable)
        table.add_columns("Type", "Buy", "Sell", "Qty", "Capital", "ISK/hr", "Notes")
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
        self.query_one("#character", Static).update(
            f"Station {character.station_id}  |  "
            f"Wallet {character.wallet_balance:,.0f} ISK  |  "
            f"Free order slots {character.free_order_slots}  |  "
            f"Sales tax {character.fees.sales_tax:.2%}  Broker {character.fees.broker_fee:.2%}"
        )

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


class CharacterPickerScreen(Screen[None]):
    """Lists set-up characters; add (SSO login) or remove them, then select one."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("a", "add", "Add character"),
        ("d", "remove", "Remove character"),
    ]

    def __init__(
        self,
        store: CharacterStore,
        make_refresh_fn: MakeRefreshFn,
        login_fn: LoginFn,
        remove_token_fn: RemoveTokenFn,
        interval_seconds: int,
    ) -> None:
        super().__init__()
        self._store = store
        self._make_refresh_fn = make_refresh_fn
        self._login_fn = login_fn
        self._remove_token_fn = remove_token_fn
        self._interval = interval_seconds

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Select a character  ·  [a] add  ·  [d] remove", id="hint")
        yield OptionList(id="characters")
        yield Static("", id="picker_status")
        yield Footer()

    def on_mount(self) -> None:
        self._reload()

    def _reload(self) -> None:
        option_list = self.query_one("#characters", OptionList)
        option_list.clear_options()
        for record in self._store.records():
            option_list.add_option(Option(record.name, id=str(record.character_id)))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.select_character(int(event.option.id))

    def select_character(self, character_id: int) -> None:
        self.app.push_screen(TradingScreen(self._make_refresh_fn(character_id), self._interval))

    def action_add(self) -> None:
        self.run_worker(self._add(), exclusive=True)

    async def _add(self) -> None:
        status = self.query_one("#picker_status", Static)
        status.update("Opening browser for EVE SSO login…")
        try:
            identity = await self._login_fn()
        except Exception as error:  # surface login failures
            status.update(f"[Login failed] {type(error).__name__}: {error}")
            return
        self._store.add(CharacterRecord(identity.character_id, identity.name))
        self._reload()
        status.update(f"Added {identity.name}")

    def action_remove(self) -> None:
        option_list = self.query_one("#characters", OptionList)
        if option_list.highlighted is None:
            return
        option = option_list.get_option_at_index(option_list.highlighted)
        if option.id is None:
            return
        character_id = int(option.id)
        self._store.remove(character_id)
        self._remove_token_fn(character_id)
        self._reload()
        self.query_one("#picker_status", Static).update("Removed character")


class EveTraderApp(App[None]):
    """Advises trades; never executes them."""

    TITLE = "evetrader"
    BINDINGS: ClassVar[list[BindingType]] = [("q", "quit", "Quit")]

    def __init__(
        self,
        store: CharacterStore,
        make_refresh_fn: MakeRefreshFn,
        login_fn: LoginFn,
        remove_token_fn: RemoveTokenFn,
        interval_seconds: int,
    ) -> None:
        super().__init__()
        self._store = store
        self._make_refresh_fn = make_refresh_fn
        self._login_fn = login_fn
        self._remove_token_fn = remove_token_fn
        self._interval = interval_seconds

    def get_default_screen(self) -> Screen[None]:
        return CharacterPickerScreen(
            self._store,
            self._make_refresh_fn,
            self._login_fn,
            self._remove_token_fn,
            self._interval,
        )
