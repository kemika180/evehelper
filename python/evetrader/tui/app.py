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
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, OptionList, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from evetrader.esi.auth import CharacterIdentity
from evetrader.esi.models import SkillQueueEntry
from evetrader.pipeline import AdvisorReport
from evetrader.session import CharacterRecord, CharacterStore
from evetrader.tui.themes import KEMIKA_PURPLE


def _finish(entry: SkillQueueEntry) -> str:
    # ESI gives UTC; astimezone() (no arg) converts to the machine's local zone.
    if entry.finish_date is None:
        return "unknown"
    return entry.finish_date.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _isk(value: float) -> str:
    """Compact ISK: 648,161,887 -> 648.16m."""
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{value / 1e9:.2f}b"
    if magnitude >= 1e6:
        return f"{value / 1e6:.2f}m"
    if magnitude >= 1e3:
        return f"{value / 1e3:.1f}k"
    return f"{value:.0f}"


def _current_training(
    skill_queue: list[SkillQueueEntry], reference: datetime
) -> SkillQueueEntry | None:
    """The skill actually training at `reference`, or None (empty/paused/finished).

    ESI can return a completed skill at the top of the queue, or a paused queue with
    no dates, so the front entry is not reliably "the one training" — check the
    window instead.
    """
    for entry in sorted(skill_queue, key=lambda e: e.queue_position):
        if (
            entry.start_date is not None
            and entry.finish_date is not None
            and entry.start_date <= reference < entry.finish_date
        ):
            return entry
    return None


def _is_completed(entry: SkillQueueEntry, reference: datetime) -> bool:
    return entry.finish_date is not None and entry.finish_date <= reference


def _completion(entry: SkillQueueEntry, reference: datetime) -> str:
    # A skill that already finished doesn't need a completion time shown.
    return "—" if _is_completed(entry, reference) else _finish(entry)


def _time_left(entry: SkillQueueEntry, reference: datetime) -> str:
    """Human time from `reference` (the report's capture time) to completion."""
    if entry.finish_date is None:
        return "unknown"
    total = int((entry.finish_date - reference).total_seconds())
    if total <= 0:
        return "done"
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

RefreshFn = Callable[[], Awaitable[AdvisorReport]]
MakeRefreshFn = Callable[[int], RefreshFn]
LoginFn = Callable[[], Awaitable[CharacterIdentity]]
RemoveTokenFn = Callable[[int], None]


class TradingScreen(Screen[None]):
    """Per-character advisor view: opportunities plus character and skill-queue."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "app.pop_screen", "Switch character")]

    DEFAULT_CSS = """
    TradingScreen #status { padding: 0 2; color: $text-muted; }
    TradingScreen #stats { height: 4; padding: 1 1 0 1; }
    TradingScreen .stat {
        width: 1fr;
        height: 100%;
        border: round $primary;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    TradingScreen #training {
        margin: 1 2 0 2;
        padding: 0 1;
        background: $boost;
    }
    TradingScreen #opportunities, TradingScreen #skillqueue { margin: 1 1; height: 1fr; }
    """

    def __init__(self, refresh_fn: RefreshFn, interval_seconds: int) -> None:
        super().__init__()
        self._refresh_fn = refresh_fn
        self._interval = interval_seconds

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Fetching market data…", id="status")
        with TabbedContent():
            with TabPane("Advisor", id="advisor"):
                with Horizontal(id="stats"):
                    yield Static(id="stat-wallet", classes="stat")
                    yield Static(id="stat-slots", classes="stat")
                    yield Static(id="stat-tax", classes="stat")
                    yield Static(id="stat-broker", classes="stat")
                yield Static("", id="training")
                yield DataTable(id="opportunities", zebra_stripes=True)
            with TabPane("Skill Queue", id="queue"):
                yield DataTable(id="skillqueue", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        opportunities = self.query_one("#opportunities", DataTable)
        opportunities.add_columns("Item", "Buy", "Sell", "Margin", "Units", "Capital", "ISK/hr")
        queue = self.query_one("#skillqueue", DataTable)
        queue.add_columns("Skill", "Time left", "Completion")
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
        self._render_stats(report)
        self._render_training(report)
        self._render_opportunities(report)
        self._render_skill_queue(report)
        count = len(report.opportunities)
        noun = "opportunity" if count == 1 else "opportunities"
        status.update(f"{count} {noun} at station {report.character.station_id} — updated")

    def _set_tile(self, selector: str, label: str, value: str, value_style: str) -> None:
        content = Text()
        content.append(f"{label}\n", style="dim")
        content.append(value, style=value_style)
        self.query_one(selector, Static).update(content)

    def _render_stats(self, report: AdvisorReport) -> None:
        fees = report.character.fees
        self._set_tile(
            "#stat-wallet", "WALLET", f"{_isk(report.character.wallet_balance)} ISK", "bold green"
        )
        self._set_tile(
            "#stat-slots", "FREE ORDER SLOTS", str(report.character.free_order_slots), "bold cyan"
        )
        self._set_tile("#stat-tax", "SALES TAX", f"{fees.sales_tax:.2%}", "bold yellow")
        self._set_tile("#stat-broker", "BROKER FEE", f"{fees.broker_fee:.2%}", "bold yellow")

    def _render_opportunities(self, report: AdvisorReport) -> None:
        table = self.query_one("#opportunities", DataTable)
        table.clear()
        for index, opportunity in enumerate(report.opportunities):
            name = report.names.get(opportunity.type_id, str(opportunity.type_id))
            table.add_row(
                Text(name, style="bold" if index == 0 else ""),
                Text(f"{opportunity.buy_price:,.2f}", justify="right"),
                Text(f"{opportunity.sell_price:,.2f}", justify="right"),
                Text(f"{opportunity.margin:.1%}", justify="right", style="green"),
                Text(f"{opportunity.quantity:,}", justify="right"),
                Text(_isk(opportunity.capital_required), justify="right", style="cyan"),
                Text(_isk(opportunity.expected_isk_per_hour), justify="right", style="bold green"),
            )

    def _render_training(self, report: AdvisorReport) -> None:
        """The skill actually training now, on the main tab."""
        current = _current_training(report.skill_queue, report.captured_at)
        target = self.query_one("#training", Static)
        if current is None:
            idle = "⏸  Skill queue empty" if not report.skill_queue else "⏸  Training paused"
            target.update(Text(idle, style="bold"))
            return
        name = report.names.get(current.skill_id, str(current.skill_id))
        bar = Text()
        bar.append("▶ TRAINING  ", style="bold magenta")
        bar.append(f"{name} → L{current.finished_level}", style="bold")
        bar.append(f"   {_time_left(current, report.captured_at)} left   ")
        bar.append(f"· completes {_finish(current)}", style="dim")
        target.update(bar)

    def _render_skill_queue(self, report: AdvisorReport) -> None:
        """The full queue as Skill / Time left / Completion; current row highlighted,
        already-completed entries marked done."""
        table = self.query_one("#skillqueue", DataTable)
        table.clear()
        current = _current_training(report.skill_queue, report.captured_at)
        reference = report.captured_at
        for entry in sorted(report.skill_queue, key=lambda e: e.queue_position):
            name = report.names.get(entry.skill_id, str(entry.skill_id))
            if current is not None and entry.queue_position == current.queue_position:
                marker, style = "▶ ", "bold magenta"
            elif _is_completed(entry, reference):
                marker, style = "✓ ", "dim strike"
            else:
                marker, style = "  ", "cyan"
            table.add_row(
                Text(f"{marker}{name} → L{entry.finished_level}", style=style),
                Text(_time_left(entry, reference), style="yellow"),
                Text(_completion(entry, reference), style="dim"),
            )


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
        theme: str = "kemika-purple",
    ) -> None:
        super().__init__()
        self._store = store
        self._make_refresh_fn = make_refresh_fn
        self._login_fn = login_fn
        self._remove_token_fn = remove_token_fn
        self._interval = interval_seconds
        self._theme_name = theme

    def on_mount(self) -> None:
        # Register the custom theme, then apply the configured one if it's known.
        # Users can also switch at runtime via the command palette (ctrl+p).
        self.register_theme(KEMIKA_PURPLE)
        if self._theme_name in self.available_themes:
            self.theme = self._theme_name

    def get_default_screen(self) -> Screen[None]:
        return CharacterPickerScreen(
            self._store,
            self._make_refresh_fn,
            self._login_fn,
            self._remove_token_fn,
            self._interval,
        )
