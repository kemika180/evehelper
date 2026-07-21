"""The evetrader TUI: a character picker, then the per-character advisor screen.

On launch you pick a set-up character (or add/remove one); selecting it opens the
trading screen. Refresh is timer-driven, never keystroke-driven; the injected
``refresh_fn`` fetches through the cached client, so a tick only touches the network
when a resource is stale.

The screens depend only on injected callables (built in cli.py), so the whole app
is testable without ESI.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, TypeVar

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.color import Color
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode
from textual_plotext import PlotextPlot

from evetrader.data.assets import AssetLocation, AssetNode
from evetrader.data.skills import SkillReference
from evetrader.esi.auth import CharacterIdentity
from evetrader.esi.models import MarketHistoryDay, Skill, SkillQueueEntry
from evetrader.market.investment import InvestmentSignal
from evetrader.pipeline import CharacterReport, OpportunityReport
from evetrader.session import CharacterRecord, CharacterStore
from evetrader.tui.themes import KEMIKA_PURPLE


def _local(moment: datetime) -> str:
    # ESI gives UTC; astimezone() (no arg) converts to the machine's local zone.
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _finish(entry: SkillQueueEntry) -> str:
    if entry.finish_date is None:
        return "unknown"
    return _local(entry.finish_date)


def _nice_ticks(low: float, high: float, count: int) -> list[float]:
    """Round tick values spanning [low, high] at 1/2/2.5/5 x10^k intervals."""
    if high <= low or count < 2:
        return [low]
    raw_step = (high - low) / (count - 1)
    magnitude = 10.0 ** math.floor(math.log10(raw_step))
    step = next(
        (m * magnitude for m in (1.0, 2.0, 2.5, 5.0, 10.0) if m * magnitude >= raw_step),
        10.0 * magnitude,
    )
    ticks: list[float] = []
    value = math.floor(low / step) * step
    while value <= high + step * 0.5:
        if value >= low - step * 0.5:
            ticks.append(value)
        value += step
    return ticks


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


def _humanize_span(total_seconds: int) -> str:
    """A positive duration as `Nd Nh` / `Nh Nm` / `Nm`."""
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _time_left(entry: SkillQueueEntry, reference: datetime) -> str:
    """Human time from `reference` (the report's capture time) to completion."""
    if entry.finish_date is None:
        return "unknown"
    total = int((entry.finish_date - reference).total_seconds())
    if total <= 0:
        return "done"
    return _humanize_span(total)


def _train_time(entry: SkillQueueEntry, reference: datetime) -> str:
    """Time to train just this skill level — the remaining time for the one in
    progress, the full duration for a queued one — not the cumulative wait.

    (`finish - max(start, reference)`: for the training skill `reference` is past
    its start, so it's the remainder; for a queued skill nothing has elapsed yet.)
    """
    if entry.finish_date is None or entry.start_date is None:
        return _time_left(entry, reference)
    began = max(entry.start_date, reference)
    total = int((entry.finish_date - began).total_seconds())
    if total <= 0:
        return "done"
    return _humanize_span(total)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class SkillProgress:
    """Where a queued skill level stands at a reference time: its SP span and how
    far into it the character is — SP-based, interpolated by time while training."""

    status: str  # "training" | "queued" | "completed" | "paused"
    level_sp: int | None  # total SP the level requires
    trained_sp: int | None  # SP already earned toward it (clamped to the level)
    fraction: float | None  # trained_sp / level_sp, if computable


def _skill_status(entry: SkillQueueEntry, reference: datetime) -> str:
    if _is_completed(entry, reference):
        return "completed"
    if entry.start_date is None or entry.finish_date is None:
        return "paused"
    if reference < entry.start_date:
        return "queued"
    return "training"


def _current_sp(entry: SkillQueueEntry, reference: datetime, status: str) -> int | None:
    """SP the character holds toward this level at `reference` (None if ESI omits SP).

    A skill trains at a constant rate, so the SP earned while training is a linear
    interpolation between the queue-entry's starting SP and the level's end SP.
    """
    if entry.training_start_sp is None or entry.level_end_sp is None:
        return None
    if status == "completed":
        return entry.level_end_sp
    if status != "training" or entry.start_date is None or entry.finish_date is None:
        return entry.training_start_sp  # queued/paused: no training has elapsed
    span = (entry.finish_date - entry.start_date).total_seconds()
    if span <= 0:
        return entry.level_end_sp
    elapsed = _clamp((reference - entry.start_date).total_seconds() / span, 0.0, 1.0)
    return round(entry.training_start_sp + elapsed * (entry.level_end_sp - entry.training_start_sp))


def _skill_progress(entry: SkillQueueEntry, reference: datetime) -> SkillProgress:
    status = _skill_status(entry, reference)
    if entry.level_start_sp is None or entry.level_end_sp is None:
        return SkillProgress(status, None, None, None)
    level_sp = entry.level_end_sp - entry.level_start_sp
    current_sp = _current_sp(entry, reference, status)
    if current_sp is None or level_sp <= 0:
        return SkillProgress(status, level_sp if level_sp > 0 else None, None, None)
    trained_sp = int(_clamp(current_sp - entry.level_start_sp, 0, level_sp))
    return SkillProgress(status, level_sp, trained_sp, trained_sp / level_sp)


def _bar(fraction: float, width: int = 24) -> str:
    filled = round(_clamp(fraction, 0.0, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _level_dots(level: int) -> str:
    """Trained skill level as filled/empty pips, e.g. L3 -> ●●●○○."""
    filled = max(0, min(5, level))
    return "●" * filled + "○" * (5 - filled)


def _asset_signature(locations: list[AssetLocation]) -> tuple[object, ...]:
    """A value that changes iff the asset tree changed — item moves between places,
    stack sizes, additions/removals — so the tree only rebuilds when it must."""

    def flatten(nodes: tuple[AssetNode, ...]) -> tuple[tuple[int, int], ...]:
        out: list[tuple[int, int]] = []
        for node in nodes:
            out.append((node.item_id, node.quantity))
            out.extend(flatten(node.children))
        return tuple(out)

    return tuple((loc.location_id, flatten(loc.items)) for loc in locations)


# Fitted-module slots collapse into one "Fit" group; each is labelled by slot type.
_SLOT_NAMES: tuple[tuple[str, str], ...] = (
    ("HiSlot", "High Slot"),
    ("MedSlot", "Mid Slot"),
    ("LoSlot", "Low Slot"),
    ("RigSlot", "Rig"),
    ("SubSystemSlot", "Subsystem"),
    ("ServiceSlot", "Service"),
)
_SLOT_PREFIXES = tuple(prefix for prefix, _ in _SLOT_NAMES)
# location_flags that just mean "loosely here" — no compartment worth grouping under.
_LOOSE_FLAGS = frozenset({"", "Hangar", "HangarAll", "Unlocked", "Locked", "AutoFit", "Deliveries"})


def _humanize_flag(flag: str) -> str:
    """Split a CamelCase location_flag: 'DroneBay' -> 'Drone Bay', 'OreHold' -> 'Ore Hold'."""
    out: list[str] = []
    for index, char in enumerate(flag):
        if index and char.isupper() and not flag[index - 1].isupper():
            out.append(" ")
        out.append(char)
    return "".join(out)


def _asset_section(flag: str) -> str | None:
    """The ship/container compartment a flag belongs to (a group heading), or None for
    items that just sit loose in a hangar."""
    if flag == "Cargo":
        return "Cargo"
    if flag.startswith("FighterTube"):
        return "Fighter Bay"
    if flag.startswith(_SLOT_PREFIXES):
        return "Fit"
    if flag in _LOOSE_FLAGS:
        return None
    if flag.endswith(("Bay", "Hold", "Hangar")):
        return _humanize_flag(flag)  # DroneBay, FleetHangar, OreHold, FuelBay…
    return None


def _slot_label(flag: str) -> str:
    for prefix, name in _SLOT_NAMES:
        if flag.startswith(prefix):
            return name
    return _humanize_flag(flag)


def _slot_rank(flag: str) -> int:
    for rank, prefix in enumerate(_SLOT_PREFIXES):
        if flag.startswith(prefix):
            return rank
    return len(_SLOT_PREFIXES)


def _section_order(section: str) -> tuple[int, str]:
    return ({"Fit": 0, "Cargo": 1}.get(section, 2), section)


# Theme colours cycled by tree depth to tint each row's full-width background — a
# faint hierarchy guide (mostly the surface colour, a hint of hue), not a loud bar.
# Only always-defined theme fields (some themes leave warning/error unset -> None).
_DEPTH_HUES = ("primary", "accent", "success", "secondary")
_BAR_MIX = 0.78  # blend fraction toward the surface colour

TreeData = TypeVar("TreeData")


class DepthTree(Tree[TreeData]):
    """A tree that tints each row's full-width background by depth, for readability."""

    DEFAULT_CSS = "DepthTree { overflow-x: hidden; }"

    def render_label(self, node: TreeNode[TreeData], base_style: Style, style: Style) -> Text:
        label = super().render_label(node, base_style, style)
        depth = 0
        parent = node.parent
        while parent is not None:
            depth += 1
            parent = parent.parent
        theme = self.app.current_theme
        # Guard against a theme leaving a field unset (None); primary is always set.
        hue = getattr(theme, _DEPTH_HUES[depth % len(_DEPTH_HUES)]) or theme.primary
        surface = theme.surface or theme.background or "#000000"
        bar = Color.parse(hue).blend(Color.parse(surface), _BAR_MIX)
        label.pad_right(max(0, self.size.width - label.cell_len))  # fill the row width
        label.stylize(Style(bgcolor=bar.rich_color))  # bg only, keep the fg accents
        return label


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


class PriceHistoryScreen(ModalScreen[None]):
    """A modal price-history chart for one item (its N-day average/high/low, the
    moving average, and today's price), plotted in the terminal."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    PriceHistoryScreen { align: center middle; }
    PriceHistoryScreen #plotbox {
        width: 88%;
        height: 82%;
        border: round $primary;
        background: $surface;
    }
    PriceHistoryScreen #plothint { padding: 0 1; color: $text-muted; }
    """

    def __init__(self, title: str, days: list[MarketHistoryDay], signal: InvestmentSignal) -> None:
        super().__init__()
        self._title = title
        self._days = sorted(days, key=lambda day: day.date)
        self._signal = signal

    def compose(self) -> ComposeResult:
        with Vertical(id="plotbox"):
            yield PlotextPlot()
            yield Static(
                "cyan = daily avg · magenta = fair value · yellow = now   ·   esc to close",
                id="plothint",
            )

    def on_mount(self) -> None:
        plt = self.query_one(PlotextPlot).plt
        days = self._days
        count = len(days)
        if count == 0:
            plt.title(f"{self._title} — no history")
            return
        averages = [day.average for day in days]
        plt.plot(list(range(count)), averages, color="cyan")
        plt.hline(self._signal.fair_value, color="magenta")
        plt.hline(self._signal.current_price, color="yellow")

        # X axis: real dates at ~6 evenly spaced positions instead of 0..N indices.
        ticks = min(6, count)
        if count > 1:
            positions = [round(i * (count - 1) / (ticks - 1)) for i in range(ticks)]
        else:
            positions = [0]
        plt.xticks(
            [float(p) for p in positions], [days[p].date.strftime("%b %d") for p in positions]
        )

        # Y axis: round, human-readable ISK values.
        low = min(*averages, self._signal.current_price, self._signal.fair_value)
        high = max(*averages, self._signal.current_price, self._signal.fair_value)
        y_ticks = _nice_ticks(low, high, 5)
        plt.yticks(y_ticks, [_isk(value) for value in y_ticks])

        plt.title(
            f"{self._title}   ·   now {_isk(self._signal.current_price)}   ·   "
            f"fair {_isk(self._signal.fair_value)}   ·   last {count} days"
        )


_STATUS_LABEL: dict[str, tuple[str, str]] = {
    "training": ("▶ Training now", "bold magenta"),
    "queued": ("• Queued", "cyan"),
    "completed": ("✓ Completed", "green"),
    "paused": ("⏸ Paused", "yellow"),
}


class SkillInfoScreen(ModalScreen[None]):
    """Details for one skill-queue entry: level, SP progress, and timing."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    SkillInfoScreen { align: center middle; }
    SkillInfoScreen #skillbox {
        width: 64;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    SkillInfoScreen #skillhint { padding: 1 0 0 0; color: $text-muted; }
    """

    def __init__(
        self,
        name: str,
        info: SkillReference | None,
        *,
        entry: SkillQueueEntry | None = None,
        reference: datetime | None = None,
        trained_level: int | None = None,
    ) -> None:
        super().__init__()
        self._skill_name = name
        self._info = info
        self._entry = entry
        self._reference = reference
        self._trained_level = trained_level

    def compose(self) -> ComposeResult:
        with Vertical(id="skillbox"):
            yield Static(self._body(), id="skillbody")
            yield Static("click or esc to close", id="skillhint")

    def on_click(self) -> None:
        self.dismiss()

    def _body(self) -> Text:
        info = self._info

        text = Text()
        text.append(self._skill_name, style="bold")
        if self._entry is not None:
            text.append(f"   →   Level {self._entry.finished_level}\n", style="bold")
        elif self._trained_level is not None:
            text.append(f"   ·   Level {self._trained_level} trained\n", style="bold")
        else:
            text.append("\n")
        if info is not None:
            text.append(f"rank {info.rank}  ·  {info.primary} / {info.secondary}\n", style="dim")
        text.append("\n")

        if self._entry is not None and self._reference is not None:
            self._append_training(text, self._entry, self._reference)
        if info is not None and info.description:
            text.append(info.description, style="italic dim")
        return text

    def _append_training(self, text: Text, entry: SkillQueueEntry, reference: datetime) -> None:
        """The queue-side detail: status, SP progress, and timing."""
        progress = _skill_progress(entry, reference)
        label, style = _STATUS_LABEL[progress.status]
        text.append(f"{label}\n", style=style)

        if progress.fraction is not None and progress.trained_sp is not None:
            text.append(f"\n{_bar(progress.fraction)}  {progress.fraction:.0%}\n", style="magenta")
            text.append(
                f"{progress.trained_sp:,} / {progress.level_sp:,} SP this level\n", style="dim"
            )
        elif progress.level_sp is not None:
            text.append(f"\n{progress.level_sp:,} SP for this level\n", style="dim")

        if progress.status != "completed" and entry.finish_date is not None:
            text.append(f"\n{_time_left(entry, reference)} left\n", style="yellow")
        if entry.start_date is not None:
            text.append(f"starts     {_local(entry.start_date)}\n", style="dim")
        if entry.finish_date is not None:
            text.append(f"completes  {_local(entry.finish_date)}\n", style="dim")
        text.append("\n")


class TradingScreen(Screen[None]):
    """Per-character advisor view: opportunities plus character and skill-queue."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "app.pop_screen", "Switch character")]

    DEFAULT_CSS = """
    TradingScreen #location { padding: 0 2; text-style: bold; color: $accent; }
    TradingScreen #status { padding: 0 2; color: $text-muted; }
    /* Fill the window down the tab chain so only the inner tables/tree scroll —
       otherwise every level is height:auto and the screen scrolls too. */
    TradingScreen TabbedContent,
    TradingScreen ContentSwitcher,
    TradingScreen TabPane { height: 1fr; }
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
    TradingScreen #buys, TradingScreen #sells, TradingScreen #skillqueue,
    TradingScreen #skilltree, TradingScreen #assettree {
        margin: 0 1;
        height: 1fr;
    }
    TradingScreen #assetsearch { margin: 0 1; }
    """

    def __init__(self, feed: RefreshFeed, interval_seconds: int) -> None:
        super().__init__()
        self._feed = feed
        self._interval = interval_seconds
        self._report: OpportunityReport | None = None
        self._character: CharacterReport | None = None
        # Signatures of the data last drawn into each tree, so a periodic refresh
        # doesn't rebuild them (and collapse the user's expansions) when unchanged.
        self._skills_key: tuple[tuple[int, int], ...] | None = None
        self._assets_key: object | None = None
        self._asset_query: str = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="location")
        yield Static("Fetching market data… (select a row for its price chart)", id="status")
        with TabbedContent():
            with TabPane("Advisor", id="advisor"):
                with Horizontal(id="stats"):
                    yield Static(id="stat-wallet", classes="stat")
                    yield Static(id="stat-slots", classes="stat")
                    yield Static(id="stat-tax", classes="stat")
                    yield Static(id="stat-broker", classes="stat")
                yield Static("", id="training")
                yield Static("BUY — trading below normal", classes="section")
                yield DataTable(id="buys", zebra_stripes=True, cursor_type="row")
                yield Static("SELL — your holdings above normal", classes="section")
                yield DataTable(id="sells", zebra_stripes=True, cursor_type="row")
            with TabPane("Skill Queue", id="queue"):
                yield DataTable(id="skillqueue", zebra_stripes=True, cursor_type="row")
            with TabPane("Skills", id="skills"):
                yield DepthTree[int]("Skills", id="skilltree")
            with TabPane("Assets", id="assets"):
                yield Input(placeholder="Search items…", id="assetsearch")
                yield DepthTree[None]("Assets", id="assettree")
        yield Footer()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "skillqueue":
            self._open_skill_info(event)
            return
        if self._report is None or event.row_key.value is None:
            return
        type_id = int(event.row_key.value)
        days = self._report.history.get(type_id)
        signal = next(
            (s for s in (*self._report.buys, *self._report.sells) if s.type_id == type_id), None
        )
        if days and signal is not None:
            name = self._report.names.get(type_id, str(type_id))
            self.app.push_screen(PriceHistoryScreen(name, days, signal))

    def _open_skill_info(self, event: DataTable.RowSelected) -> None:
        if self._character is None or event.row_key.value is None:
            return
        position = int(event.row_key.value)
        entry = next(
            (e for e in self._character.skill_queue if e.queue_position == position), None
        )
        if entry is None:
            return
        info = self._character.skill_reference.get(entry.skill_id)
        name = self._skill_name(entry.skill_id, info)
        self.app.push_screen(
            SkillInfoScreen(name, info, entry=entry, reference=self._character.captured_at)
        )

    def on_tree_node_selected(self, event: Tree.NodeSelected[int]) -> None:
        """A leaf in the Skills tree is a skill id; group nodes carry no data."""
        if self._character is None or not isinstance(event.node.data, int):
            return
        skill_id = event.node.data
        info = self._character.skill_reference.get(skill_id)
        name = self._skill_name(skill_id, info)
        # If it's currently in the queue, show the live training detail; otherwise
        # just the trained level and static facts.
        entry = next((e for e in self._character.skill_queue if e.skill_id == skill_id), None)
        if entry is not None:
            self.app.push_screen(
                SkillInfoScreen(name, info, entry=entry, reference=self._character.captured_at)
            )
            return
        trained = next(
            (s.trained_skill_level for s in self._character.skills if s.skill_id == skill_id), None
        )
        self.app.push_screen(SkillInfoScreen(name, info, trained_level=trained))

    def _skill_name(self, skill_id: int, info: SkillReference | None) -> str:
        if info is not None:
            return info.name
        assert self._character is not None
        return self._character.names.get(skill_id, str(skill_id))

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
        self._character = character_report
        self.query_one("#location", Static).update(f"📍 {character_report.station_name}")
        self._render_stats(character_report)
        self._render_training(character_report)
        self._render_skill_queue(character_report)
        self._render_skills(character_report)
        self._render_assets(character_report)

        # Phase 2: the market scan is slower; character info is already on screen.
        status.update("Scanning for value…")
        try:
            report = await self._feed.opportunities(character_report)
        except Exception as error:
            status.update(f"[Market scan failed] {type(error).__name__}: {error}")
            return
        self._report = report
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
                key=str(signal.type_id),
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
                key=str(signal.type_id),
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
                Text(_train_time(entry, reference), style="yellow"),
                Text(_completion(entry, reference), style="dim"),
                key=str(entry.queue_position),
            )

    def _render_skills(self, report: CharacterReport) -> None:
        """All trained skills as a tree grouped by skill category; a leaf's data is
        its skill id so selecting it opens the detail popup.

        Skipped when the skill set is unchanged, so a periodic refresh leaves the
        tree (and the user's expand/collapse state) untouched.
        """
        key = tuple(sorted((s.skill_id, s.trained_skill_level) for s in report.skills))
        if key == self._skills_key:
            return
        self._skills_key = key

        tree = self.query_one("#skilltree", Tree)
        tree.clear()
        tree.root.expand()

        grouped: dict[str, list[Skill]] = defaultdict(list)
        for skill in report.skills:
            info = report.skill_reference.get(skill.skill_id)
            grouped[info.group if info is not None else "Other"].append(skill)

        def display_name(skill: Skill) -> str:
            return self._skill_name(skill.skill_id, report.skill_reference.get(skill.skill_id))

        for group in sorted(grouped):
            branch = tree.root.add(group, expand=True)
            for skill in sorted(grouped[group], key=lambda s: display_name(s).lower()):
                label = Text(f"{display_name(skill)}  ")
                label.append(_level_dots(skill.trained_skill_level), style="cyan")
                branch.add_leaf(label, data=skill.skill_id)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "assetsearch" and self._character is not None:
            self._asset_query = event.value
            self._render_assets(self._character)

    def _render_assets(self, report: CharacterReport) -> None:
        """All assets as a tree of places -> items -> container/ship contents,
        filtered to the search box.

        Skipped when neither the assets nor the query changed, so a periodic refresh
        leaves the tree (and the user's expand/collapse state) alone.
        """
        query = self._asset_query.strip().lower()
        key = (_asset_signature(report.assets), query)
        if key == self._assets_key:
            return
        self._assets_key = key

        tree = self.query_one("#assettree", Tree)
        tree.clear()
        tree.root.expand()
        for location in report.assets:
            if query and not any(self._asset_matches(i, report, query) for i in location.items):
                continue  # nothing here matches the search
            # Places open so their top-level items show; containers/ships stay closed
            # until opened — unless a search needs them open to reveal a match.
            place = tree.root.add(self._location_label(location.location_id, report), expand=True)
            self._add_asset_children(place, location.items, report, query=query, show_all=not query)

    def _add_asset_children(
        self,
        parent: TreeNode[None],
        children: tuple[AssetNode, ...],
        report: CharacterReport,
        *,
        query: str,
        show_all: bool,
    ) -> None:
        """Render a node's contents, grouping ship/container compartments (Fit, Cargo,
        Drone Bay…) under headings; loose items list directly."""
        visible = (
            list(children)
            if show_all
            else [c for c in children if self._asset_matches(c, report, query)]
        )
        sections: dict[str | None, list[AssetNode]] = defaultdict(list)
        for child in visible:
            sections[_asset_section(child.location_flag)].append(child)

        def by_name(item: AssetNode) -> str:
            return self._asset_label_name(item, report)

        for child in sorted(sections.pop(None, []), key=by_name):
            self._add_asset_node(
                parent, child, report, query=query, show_all=show_all, section=None
            )

        for section in sorted((s for s in sections if s is not None), key=_section_order):
            items = sections[section]
            heading = Text(f"{section}  ({len(items)})", style="italic dim")
            node = parent.add(heading, expand=bool(query))
            if section == "Fit":
                items.sort(key=lambda i: (_slot_rank(i.location_flag), by_name(i)))
            else:
                items.sort(key=by_name)
            for child in items:
                self._add_asset_node(
                    node, child, report, query=query, show_all=show_all, section=section
                )

    def _add_asset_node(
        self,
        parent: TreeNode[None],
        item: AssetNode,
        report: CharacterReport,
        *,
        query: str,
        show_all: bool,
        section: str | None,
    ) -> None:
        name = report.names.get(item.type_id, str(item.type_id))
        custom = report.asset_names.get(item.item_id)
        label = Text()
        if section == "Fit":  # fitted module: slot name first, then the item, no number
            label.append(f"{_slot_label(item.location_flag)}   ", style="cyan")
        if item.children and custom and custom != name:
            # A named container/ship: its player name, then its type dimmed.
            label.append(custom, style="bold")
            label.append(f"  {name}", style="dim")
        else:
            label.append(name, style="bold" if item.children else "")
        if item.quantity > 1:
            label.append(f"  ×{item.quantity:,}", style="cyan")  # noqa: RUF001 (multiplier)
        if not item.children:
            parent.add_leaf(label)
            return
        node = parent.add(label, expand=bool(query))
        # A container/ship matched by name reveals everything inside it.
        child_show_all = show_all or (bool(query) and query in name.lower())
        self._add_asset_children(node, item.children, report, query=query, show_all=child_show_all)

    def _asset_matches(self, item: AssetNode, report: CharacterReport, query: str) -> bool:
        """Whether this item, its assigned name, or anything nested inside it matches."""
        if query in report.names.get(item.type_id, str(item.type_id)).lower():
            return True
        custom = report.asset_names.get(item.item_id)
        if custom is not None and query in custom.lower():
            return True
        return any(self._asset_matches(child, report, query) for child in item.children)

    def _asset_label_name(self, item: AssetNode, report: CharacterReport) -> str:
        return report.names.get(item.type_id, str(item.type_id)).lower()

    def _location_label(self, location_id: int, report: CharacterReport) -> str:
        if location_id == report.character.station_id:
            return report.station_name  # the home place, named (incl. structures)
        return report.names.get(location_id, f"Location {location_id}")


class CharacterPickerScreen(Screen[None]):
    """Lists set-up characters; add (SSO login) or remove them, then select one."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("a", "add", "Add character"),
        ("d", "remove", "Remove character"),
        ("escape", "resume", "Back"),
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
        self._last_character_id: int | None = None

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
        self._last_character_id = character_id
        self.app.push_screen(TradingScreen(self._make_feed(character_id), self._interval))

    def action_resume(self) -> None:
        """Esc with a character already open returns to it, rather than sitting here."""
        if self._last_character_id is not None:
            self.select_character(self._last_character_id)

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
