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
from dataclasses import dataclass
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
from evetrader.pipeline import CharacterReport, OpportunityReport
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

FetchCharacter = Callable[[], Awaitable[CharacterReport]]
FetchOpportunities = Callable[[CharacterReport], Awaitable[OpportunityReport]]
LoginFn = Callable[[], Awaitable[CharacterIdentity]]
RemoveTokenFn = Callable[[int], None]


@dataclass
class RefreshFeed:
    """The two-phase data source for one character: quick character info, then the
    slower market scan."""

    character: FetchCharacter
    opportunities: FetchOpportunities


MakeFeed = Callable[[int], RefreshFeed]


class TradingScreen(Screen[None]):
    """Per-character advisor view: opportunities plus character and skill-queue."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "app.pop_screen", "Switch character")]

    DEFAULT_CSS = """
    TradingScreen #location { padding: 0 2; text-style: bold; color: $accent; }
    TradingScreen #status { padding: 0 2; color: $text-muted; }
    TradingScreen #stats { height: 6; padding: 1 1 0 1; }
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
    TradingScreen .section { padding: 1 2 0 2; text-style: bold; color: $accent; }
    TradingScreen #buys, TradingScreen #sells, TradingScreen #skillqueue {
        margin: 0 1;
        height: 1fr;
    }
    """

    def __init__(self, feed: RefreshFeed, interval_seconds: int) -> None:
        super().__init__()
        self._feed = feed
        self._interval = interval_seconds

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="location")
        yield Static("Fetching market data…", id="status")
        with TabbedContent():
            with TabPane("Advisor", id="advisor"):
                with Horizontal(id="stats"):
                    yield Static(id="stat-wallet", classes="stat")
                    yield Static(id="stat-slots", classes="stat")
                    yield Static(id="stat-tax", classes="stat")
                    yield Static(id="stat-broker", classes="stat")
                yield Static("", id="training")
                yield Static("BUY — trading below normal", classes="section")
                yield DataTable(id="buys", zebra_stripes=True)
                yield Static("SELL — your holdings above normal", classes="section")
                yield DataTable(id="sells", zebra_stripes=True)
            with TabPane("Skill Queue", id="queue"):
                yield DataTable(id="skillqueue", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#buys", DataTable).add_columns(
            "Item", "Now", "Fair", "Range", "Units", "Est. profit", "Note"
        )
        self.query_one("#sells", DataTable).add_columns(
            "Item", "Held", "Bid", "Fair", "Range", "Est. gain", "Note"
        )
        self.query_one("#skillqueue", DataTable).add_columns("Skill", "Time left", "Completion")
        self.run_worker(self._refresh(), exclusive=True)
        self.set_interval(self._interval, self._refresh)

    async def _refresh(self) -> None:
        status = self.query_one("#status", Static)
        # Phase 1: character, holdings and skill queue render immediately.
        status.update("Loading character…")
        try:
            character_report = await self._feed.character()
        except Exception as error:  # surface it instead of a blank screen
            status.update(f"[Character load failed] {type(error).__name__}: {error}")
            return
        self.query_one("#location", Static).update(f"📍 {character_report.station_name}")
        self._render_stats(character_report)
        self._render_training(character_report)
        self._render_skill_queue(character_report)

        # Phase 2: the market scan is slower; character info is already on screen.
        status.update("Scanning for value…")
        try:
            report = await self._feed.opportunities(character_report)
        except Exception as error:
            status.update(f"[Market scan failed] {type(error).__name__}: {error}")
            return
        self._render_buys(report)
        self._render_sells(report)
        status.update(f"{len(report.buys)} to buy · {len(report.sells)} to sell — updated")

    def _set_tile(self, selector: str, label: str, value: str, value_style: str) -> None:
        content = Text()
        content.append(f"{label}\n", style="dim")
        content.append(value, style=value_style)
        self.query_one(selector, Static).update(content)

    def _render_stats(self, report: CharacterReport) -> None:
        fees = report.character.fees
        self._set_tile(
            "#stat-wallet", "WALLET", f"{_isk(report.character.wallet_balance)} ISK", "bold green"
        )
        self._set_tile(
            "#stat-slots", "FREE ORDER SLOTS", str(report.character.free_order_slots), "bold cyan"
        )
        self._set_tile("#stat-tax", "SALES TAX", f"{fees.sales_tax:.2%}", "bold yellow")
        self._set_tile("#stat-broker", "BROKER FEE", f"{fees.broker_fee:.2%}", "bold yellow")

    def _render_buys(self, report: OpportunityReport) -> None:
        table = self.query_one("#buys", DataTable)
        table.clear()
        for index, signal in enumerate(report.buys):
            name = report.names.get(signal.type_id, str(signal.type_id))
            table.add_row(
                Text(name, style="bold" if index == 0 else ""),
                Text(_isk(signal.current_price), justify="right"),
                Text(_isk(signal.fair_value), justify="right", style="dim"),
                Text(f"{signal.channel_position:.0%}", justify="right", style="green"),
                Text(f"{signal.quantity:,}", justify="right"),
                Text(_isk(signal.expected_profit), justify="right", style="bold green"),
                Text(signal.reasoning, style="dim"),
            )

    def _render_sells(self, report: OpportunityReport) -> None:
        table = self.query_one("#sells", DataTable)
        table.clear()
        for signal in report.sells:
            name = report.names.get(signal.type_id, str(signal.type_id))
            table.add_row(
                Text(name),
                Text(f"{signal.quantity:,}", justify="right"),
                Text(_isk(signal.current_price), justify="right"),
                Text(_isk(signal.fair_value), justify="right", style="dim"),
                Text(f"{signal.channel_position:.0%}", justify="right", style="yellow"),
                Text(_isk(signal.expected_profit), justify="right", style="bold yellow"),
                Text(signal.reasoning, style="dim"),
            )

    def _render_training(self, report: CharacterReport) -> None:
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

    def _render_skill_queue(self, report: CharacterReport) -> None:
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
        make_feed: MakeFeed,
        login_fn: LoginFn,
        remove_token_fn: RemoveTokenFn,
        interval_seconds: int,
    ) -> None:
        super().__init__()
        self._store = store
        self._make_feed = make_feed
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
        self.app.push_screen(TradingScreen(self._make_feed(character_id), self._interval))

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
        make_feed: MakeFeed,
        login_fn: LoginFn,
        remove_token_fn: RemoveTokenFn,
        interval_seconds: int,
        theme: str = "kemika-purple",
    ) -> None:
        super().__init__()
        self._store = store
        self._make_feed = make_feed
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
            self._make_feed,
            self._login_fn,
            self._remove_token_fn,
            self._interval,
        )
