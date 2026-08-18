"""The evetrader TUI: a character picker, then the per-character advisor screen.

On launch you pick a set-up character (or add/remove one); selecting it opens the
trading screen. Refresh is timer-driven, never keystroke-driven; the injected
``refresh_fn`` fetches through the cached client, so a tick only touches the network
when a resource is stale.

The screens depend only on injected callables (built in cli.py), so the whole app
is testable without ESI.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, TypeVar

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from evetrader.data.assets import AssetLocation, AssetNode
from evetrader.data.sde_download import ProgressFn, SdeState
from evetrader.data.skills import SkillReference
from evetrader.esi.auth import CharacterIdentity
from evetrader.esi.models import (
    Blueprint,
    IndustryJob,
    Skill,
    SkillQueueEntry,
    WalletTransaction,
)
from evetrader.market.listings import ListingStatus
from evetrader.pipeline import BuildOpportunity, CharacterReport, OpportunityReport
from evetrader.session import CharacterRecord, CharacterStore
from evetrader.tui.themes import KEMIKA_PURPLE


def _local(moment: datetime) -> str:
    # ESI gives UTC; astimezone() (no arg) converts to the machine's local zone.
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _finish(entry: SkillQueueEntry) -> str:
    if entry.finish_date is None:
        return "unknown"
    return _local(entry.finish_date)


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


def _duration(seconds: float) -> str:
    """Compact training time: 7800 -> "2h10m", 2700 -> "45m", 2360760 -> "27d7h"."""
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _volume(cubic_metres: float) -> str:
    """Compact m³: 310 -> "310 m³", 12500 -> "12.5k m³"."""
    if cubic_metres >= 1e6:
        return f"{cubic_metres / 1e6:.1f}M m³"
    if cubic_metres >= 1e3:
        return f"{cubic_metres / 1e3:.1f}k m³"
    return f"{cubic_metres:.0f} m³"


def _slug(title: str) -> str:
    """A filesystem-safe slug from a product name (e.g. "Rifter" -> "rifter")."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _truncate(name: str, width: int) -> str:
    """Name clipped to ``width`` cells with an ellipsis when it doesn't fit."""
    return name if len(name) <= width else name[: width - 1] + "…"


def recipe_markdown(build: BuildOpportunity, names: dict[int, str]) -> str:
    """The crafting recipe as a Markdown document: materials (with buy price + volume), how to
    self-source them (mine / build), what to buy, and quick-train skills. Pure."""

    def name(type_id: int) -> str:
        return names.get(type_id, str(type_id))

    product = name(build.product_type_id)
    out: list[str] = [f"# {product} — crafting recipe", ""]
    out.append(f"Blueprint ME {build.material_efficiency}, per run.")
    plan = build.plan
    if plan is None:
        return "\n".join(out) + "\n"

    _tag = {"refine": "mine", "build": "build", "buildable": "build\\*", "buy": "buy"}
    if plan.materials:
        out += ["", "## Materials required", ""]
        out += ["| Qty | Material | Buy @ Jita | Volume | Self-source |"]
        out += ["|---:|---|---:|---:|---|"]
        for material in plan.materials:
            buy = _isk(material.buy_cost) if material.buy_cost is not None else "—"
            tag = _tag.get(material.source, material.source)
            out.append(
                f"| {material.quantity:,} | {name(material.type_id)} | {buy} |"
                f" {_volume(material.volume)} | {tag} |"
            )
        total = plan.total_buy_cost
        buy_all = _isk(total) if total is not None else "—"
        out += ["", f"**Buy all:** {buy_all} · {_volume(plan.total_material_volume)}"]
        if any(m.source == "buildable" for m in plan.materials):
            out += ["", "\\* buildable — you'd need its blueprint (see below)."]

    if plan.missing_skills:
        out += ["", "## Skills to build this (not yet trained)", ""]
        for skill in plan.missing_skills:
            when = f", train ~{_duration(skill.train_seconds)}" if skill.train_seconds else ""
            out.append(f"- {name(skill.type_id)} {skill.level} (have {skill.current_level}{when})")
        out += ["", f"**Total training:** ~{_duration(plan.total_training_seconds)}"]

    if plan.blueprints:
        out += ["", "## Blueprints needed (BPCs — est. copy-job cost)", ""]
        for bp in plan.blueprints:
            cost = f" — ~{_isk(bp.copy_cost)}" if bp.copy_cost is not None else ""
            out.append(f"- {name(bp.blueprint_type_id)}{cost}")
        bp_total = plan.total_blueprint_cost
        if bp_total is not None:
            out += ["", f"**Total blueprints (copy):** ~{_isk(bp_total)}"]

    if plan.mine:
        out += ["", "## To self-source — mine & refine", ""]
        out += ["| Ore | Units | Volume (m³) | Location |", "|---|---:|---:|---|"]
        for line in plan.mine:
            ore = name(line.ore_type_id)
            out.append(f"| {ore} | {line.quantity:,} | {line.volume:,.0f} | {line.location} |")
        out += ["", f"**Total to haul:** {_volume(plan.total_mine_volume)}"]

    if plan.build:
        out += ["", "## To self-source — build (dependencies first)", ""]
        for step in plan.build:
            product = name(step.product_type_id)
            out += ["", f"### {step.quantity:,} x {product} ({step.runs} run(s))", ""]
            out += ["| Material | Qty | Volume (m³) |", "|---|---:|---:|"]
            for inp in step.inputs:
                out.append(f"| {name(inp.type_id)} | {inp.quantity:,} | {inp.volume:,.0f} |")

    if plan.buy:
        out += ["", "## Buy or produce (reactions / PI not modelled yet)", ""]
        out += ["| Qty | Item | Buy @ Jita | Make via |", "|---:|---|---:|---|"]
        _method = {"buy": "—", "reaction": "reaction", "pi": "PI"}
        for item in plan.buy:
            cost = _isk(item.buy_cost) if item.buy_cost is not None else "—"
            out.append(
                f"| {item.quantity:,} | {name(item.type_id)} | {cost} |"
                f" {_method.get(item.method, item.method)} |"
            )
        buy_total = plan.total_buy_cost_items
        if buy_total is not None:
            out += ["", f"**Total to buy:** {_isk(buy_total)}"]

    if build.training_tips:
        out += ["", "## Recommended skills", ""]
        for tip in build.training_tips:
            if tip.unlocks_type_id is not None:
                benefit = f"unlocks building {name(tip.unlocks_type_id)}"
            else:
                benefit = f"~{tip.ore_reduction:.0%} less ore to mine"
            out.append(
                f"- {name(tip.skill_id)} L{tip.current_level}→{tip.target_level}"
                f" (~{_duration(tip.train_seconds)}): {benefit}"
            )
    out.append("")
    return "\n".join(out)


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


def _skill_queue_pips(
    skill_id: int, trained: int, queue: list[SkillQueueEntry], reference: datetime
) -> Text:
    """Five level pips (mirroring the in-game squares), with queued levels coloured.

    A trained level is a cyan ■ and an untrained one a dim □ — the same regardless of
    the queue. On top of that, a level *pending* training is highlighted magenta: the
    one part-trained right now shows a ◪, other queued levels a □. So a skill not in
    the queue reads plainly, and a queued skill lights up exactly the levels it will
    train.

    A queue level that has already *finished* counts as trained (a full ■), even if the
    skills endpoint hasn't caught up to it yet — so a just-completed level never lingers
    as a queued/part-trained box."""
    entries = [e for e in queue if e.skill_id == skill_id]
    finished = max((e.finished_level for e in entries if _is_completed(e, reference)), default=0)
    trained = max(0, min(5, max(trained, finished)))
    pending = [e for e in entries if not _is_completed(e, reference)]
    queued = {entry.finished_level for entry in pending}
    partial_level: int | None = None
    for entry in pending:
        progress = _skill_progress(entry, reference)
        if progress.fraction is not None and 0.0 < progress.fraction < 1.0:
            partial_level = entry.finished_level
            break

    pips = Text()
    for level in range(1, 6):
        if level <= trained:
            pips.append("■", style="cyan")  # trained: unchanged by the queue
        elif level == partial_level:
            pips.append("◪", style="magenta")  # queued and part-trained now
        elif level in queued:
            pips.append("□", style="magenta")  # queued, not yet started
        else:
            pips.append("□", style="dim")  # untrained and not queued
    return pips


# Industry activity ids -> human names (the current, non-legacy activities).
_ACTIVITY_NAMES: dict[int, str] = {
    1: "Manufacturing",
    3: "TE Research",
    4: "ME Research",
    5: "Copying",
    8: "Invention",
    9: "Reactions",
}

# Per-state row marker + style and popup label, mirroring the skill-queue convention.
_JOB_STATES: dict[str, tuple[str, str, str]] = {
    # state: (row marker, style, popup label)
    "ready": ("● ", "bold green", "● Ready to deliver"),
    "active": ("▶ ", "cyan", "▶ In progress"),
    "paused": ("⏸ ", "yellow", "⏸ Paused"),
    "delivered": ("  ", "dim", "✓ Delivered"),
    "cancelled": ("  ", "dim", "✗ Cancelled"),
    "reverted": ("  ", "dim", "✗ Reverted"),
}


def _activity_name(activity_id: int) -> str:
    return _ACTIVITY_NAMES.get(activity_id, f"Activity {activity_id}")


def _job_state(job: IndustryJob, reference: datetime) -> str:
    """Display state at `reference`: 'ready' (finished, awaiting delivery), 'paused',
    'active', or a terminal status ESI may still return (delivered/cancelled/reverted)."""
    if job.status in {"paused", "delivered", "cancelled", "reverted"}:
        return job.status
    return "ready" if job.end_date <= reference else "active"


def _job_subject_type(job: IndustryJob) -> int:
    """The type id that names the job — the product for manufacturing/invention/
    reactions, else the blueprint (research and copying produce no new item)."""
    return job.product_type_id if job.product_type_id is not None else job.blueprint_type_id


def _job_time_left(job: IndustryJob, reference: datetime) -> str:
    if job.end_date <= reference:
        return "ready"
    return _humanize_span(int((job.end_date - reference).total_seconds()))


# Manufacturing line slots: base 1, +1 per level of Mass Production and Advanced Mass
# Production. Reactions and research are separate pools, not counted here. Game constants.
_MASS_PRODUCTION = 3387
_ADVANCED_MASS_PRODUCTION = 24625
_MANUFACTURING_ACTIVITY = 1


def _free_manufacturing_slots(report: CharacterReport) -> int:
    """Free manufacturing job lines: the slot cap (1 + Mass Production + Advanced Mass
    Production levels) minus the manufacturing jobs currently occupying a line."""
    levels = {skill.skill_id: skill.active_skill_level for skill in report.skills}
    slots = 1 + levels.get(_MASS_PRODUCTION, 0) + levels.get(_ADVANCED_MASS_PRODUCTION, 0)
    running = sum(1 for job in report.industry_jobs if job.activity_id == _MANUFACTURING_ACTIVITY)
    return max(0, slots - running)


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


def _asset_section(flag: str, *, allow_fit: bool = True) -> str | None:
    """The ship/container compartment a flag belongs to (a group heading), or None for
    items that just sit loose in a hangar.

    ``allow_fit`` is False when the parent is itself a fitted module: ESI gives a
    loaded charge the same slot flag as the weapon holding it, so a slot flag one
    level down means "loaded ammo", not a fitting — list it loose, not under "Fit"."""
    if flag == "Cargo":
        return "Cargo"
    if flag.startswith("FighterTube"):
        return "Fighter Bay"
    if flag.startswith(_SLOT_PREFIXES):
        return "Fit" if allow_fit else None
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
    """A tree that tints each row's full-width background by depth, for readability.

    Also accepts vim hjkl motion: j/k mirror the down/up arrows, while l expands (or
    toggles) the focused node and h steps out to its parent."""

    DEFAULT_CSS = "DepthTree { overflow-x: hidden; }"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "toggle_node", "Expand", show=False),
        Binding("right", "toggle_node", "Expand", show=False),
        Binding("h", "cursor_parent", "Parent", show=False),
        Binding("left", "cursor_parent", "Parent", show=False),
    ]

    def render_label(self, node: TreeNode[TreeData], base_style: Style, style: Style) -> Text:
        label = super().render_label(node, base_style, style)
        theme = self.app.current_theme
        label.pad_right(max(0, self.size.width - label.cell_len))  # fill the row width
        # The cursor row would be lost under the per-depth tint below, so give it an
        # unmistakable full-width bar: bright accent bg with dark bold text. Only while
        # focused, so it doesn't shout when the search box holds focus instead.
        if node is self.cursor_node and self.has_focus:
            accent = Color.parse(theme.accent or theme.primary)
            ink = Color.parse(theme.background or "#000000")
            label.stylize(Style(color=ink.rich_color, bgcolor=accent.rich_color, bold=True))
            return label
        depth = 0
        parent = node.parent
        while parent is not None:
            depth += 1
            parent = parent.parent
        # Guard against a theme leaving a field unset (None); primary is always set.
        hue = getattr(theme, _DEPTH_HUES[depth % len(_DEPTH_HUES)]) or theme.primary
        surface = theme.surface or theme.background or "#000000"
        bar = Color.parse(hue).blend(Color.parse(surface), _BAR_MIX)
        label.stylize(Style(bgcolor=bar.rich_color))  # bg only, keep the fg accents
        return label


class NavDataTable(DataTable[object]):
    """A DataTable that also accepts vim hjkl motion, mirroring the arrow keys."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("h", "cursor_left", "Left", show=False),
        Binding("l", "cursor_right", "Right", show=False),
    ]


class NavOptionList(OptionList):
    """An OptionList that also accepts vim j/k motion, mirroring the up/down arrows."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


FetchCharacter = Callable[[], Awaitable[CharacterReport]]
FetchOpportunities = Callable[[CharacterReport], Awaitable[OpportunityReport]]
LoginFn = Callable[[], Awaitable[CharacterIdentity]]
RemoveTokenFn = Callable[[int], None]
# Download the SDE (in the background) and make it available; True on success. The
# ProgressFn is called with (bytes downloaded, total|None) so the prompt can show a bar.
DownloadSdeFn = Callable[[ProgressFn], Awaitable[bool]]
# Check whether the local SDE is missing/stale/current; drives the launch-time prompt.
# None when the host didn't wire it (e.g. tests) — no prompt is shown then.
SdeCheckFn = Callable[[], Awaitable[SdeState]]


@dataclass
class RefreshFeed:
    """The two-phase data source for one character: quick character info, then the
    slower market scan."""

    character: FetchCharacter
    opportunities: FetchOpportunities


MakeFeed = Callable[[int], RefreshFeed]


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


class BlueprintInfoScreen(ModalScreen[None]):
    """Blueprint detail: original vs copy, research (ME/TE savings), and runs.

    Only blueprints get a popup — for an ordinary asset there's nothing solid to show
    without the SDE (volume/group, deferred to hauling) or a per-click ESI fetch (which
    the cache rules forbid), so those rows are inert."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    BlueprintInfoScreen { align: center middle; }
    BlueprintInfoScreen #bpbox {
        width: 64;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    BlueprintInfoScreen #bphint { padding: 1 0 0 0; color: $text-muted; }
    """

    def __init__(self, name: str, node: AssetNode, blueprint: Blueprint) -> None:
        super().__init__()
        self._name = name
        self._node = node
        self._blueprint = blueprint

    def compose(self) -> ComposeResult:
        with Vertical(id="bpbox"):
            yield Static(self._body(), id="bpbody")
            yield Static("click or esc to close", id="bphint")

    def on_click(self) -> None:
        self.dismiss()

    def _body(self) -> Text:
        blueprint = self._blueprint
        original = blueprint.runs == -1
        text = Text()
        text.append(f"{self._name}\n", style="bold")
        text.append(
            "Blueprint Original (BPO)\n" if original else "Blueprint Copy (BPC)\n",
            style="bold magenta",
        )
        # ESI reports ME/TE as the percentage saved directly (ME 0-10%, TE 0-20%).
        text.append(
            f"-{blueprint.material_efficiency}% materials  ·  "
            f"-{blueprint.time_efficiency}% time\n",
            style="cyan",
        )
        if original:
            text.append("unlimited runs\n", style="dim")
        else:
            text.append(f"{blueprint.runs:,} runs remaining\n", style="dim")
        if self._node.quantity > 1:
            text.append(f"stack of {self._node.quantity:,}\n", style="dim")
        return text


class IndustryJobScreen(ModalScreen[None]):
    """Detail for one industry job: activity, product/blueprint, runs, where it runs,
    timing, cost, and (for invention) success chance."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    IndustryJobScreen { align: center middle; }
    IndustryJobScreen #jobbox {
        width: 64;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    IndustryJobScreen #jobhint { padding: 1 0 0 0; color: $text-muted; }
    """

    def __init__(self, name: str, job: IndustryJob, facility: str, reference: datetime) -> None:
        super().__init__()
        self._name = name
        self._job = job
        self._facility = facility
        self._reference = reference

    def compose(self) -> ComposeResult:
        with Vertical(id="jobbox"):
            yield Static(self._body(), id="jobbody")
            yield Static("click or esc to close", id="jobhint")

    def on_click(self) -> None:
        self.dismiss()

    def _body(self) -> Text:
        job = self._job
        state = _job_state(job, self._reference)
        _, _, status_label = _JOB_STATES[state]

        text = Text()
        text.append(f"{self._name}\n", style="bold")
        runs = "run" if job.runs == 1 else "runs"
        text.append(f"{_activity_name(job.activity_id)}  ·  {job.runs:,} {runs}\n", style="dim")
        text.append(f"{status_label}\n", style=_JOB_STATES[state][1])

        if state == "active":
            text.append(f"\n{_job_time_left(job, self._reference)} left\n", style="yellow")
        text.append(f"\nat {self._facility}\n", style="dim")
        text.append(f"starts  {_local(job.start_date)}\n", style="dim")
        text.append(f"ends    {_local(job.end_date)}\n", style="dim")
        if job.cost is not None:
            text.append(f"job cost  {_isk(job.cost)} ISK\n", style="dim")
        if job.probability is not None:
            text.append(f"success   {job.probability:.0%}\n", style="dim")  # invention
        return text


class MaterialsScreen(ModalScreen[None]):
    """The crafting recipe for one build: the materials it needs (with Jita buy price and
    volume), how to self-source each (mine the byproduct-aware ore plan, or build the
    sub-components), what must be bought, and quick skills to train. Exportable to Markdown."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("e", "export", "Export .md"),
    ]

    DEFAULT_CSS = """
    MaterialsScreen { align: center middle; }
    MaterialsScreen #matbox {
        width: 90;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    MaterialsScreen #mathint { padding: 1 0 0 0; color: $text-muted; }
    """

    def __init__(self, title: str, build: BuildOpportunity, names: dict[int, str]) -> None:
        super().__init__()
        self._title = title
        self._build = build
        self._names = names

    def compose(self) -> ComposeResult:
        with Vertical(id="matbox"):
            yield Static(self._body(), id="matbody")
            yield Static("e export .md · esc to close", id="mathint")

    def _tname(self, type_id: int) -> str:
        return self._names.get(type_id, str(type_id))

    def _body(self) -> Text:
        text = Text()
        text.append(f"{self._title}\n", style="bold")
        text.append(f"ME {self._build.material_efficiency}  ·  per run\n", style="dim")
        self._append_materials(text)
        self._append_required_skills(text)
        self._append_self_source(text)
        self._append_blueprints(text)
        self._append_buy(text)
        self._append_tips(text)
        return text

    _SOURCE_TAG: ClassVar[dict[str, str]] = {
        "refine": "mine",
        "build": "build",
        "buildable": "build*",
        "buy": "buy",
    }

    def _append_materials(self, text: Text) -> None:
        plan = self._build.plan
        if plan is None or not plan.materials:
            return
        text.append("\nmaterials required\n", style="bold")
        text.append(
            f"  {'Qty':>10}  {'Material':<24} {'Buy @ Jita':>11} {'Volume':>10}  Get\n",
            style="dim",
        )
        for material in plan.materials:
            name = _truncate(self._tname(material.type_id), 24)
            buy = _isk(material.buy_cost) if material.buy_cost is not None else "—"
            tag = self._SOURCE_TAG.get(material.source, material.source)
            text.append(
                f"  {material.quantity:>10,}  {name:<24} {buy:>11} {_volume(material.volume):>10}"
                f"  {tag}\n"
            )
        total = plan.total_buy_cost
        buy_all = _isk(total) if total is not None else "—"
        text.append(
            f"  buy all: {buy_all}  ·  {_volume(plan.total_material_volume)}\n", style="dim"
        )
        if any(m.source == "buildable" for m in plan.materials):
            text.append("  * buildable — you'd need its blueprint (see below)\n", style="dim")

    def _append_required_skills(self, text: Text) -> None:
        plan = self._build.plan
        if plan is None or not plan.missing_skills:
            return
        text.append("\nskills to build this (not yet trained)\n", style="bold")
        for skill in plan.missing_skills:
            name = self._tname(skill.type_id)
            when = f"  train ~{_duration(skill.train_seconds)}" if skill.train_seconds else ""
            text.append(
                f"  {name} {skill.level}  (have {skill.current_level}){when}\n", style="red"
            )
        text.append(
            f"  total training: ~{_duration(plan.total_training_seconds)}\n", style="dim red"
        )

    def _append_blueprints(self, text: Text) -> None:
        plan = self._build.plan
        if plan is None or not plan.blueprints:
            return
        text.append("\nblueprints needed (BPCs — est. copy-job cost)\n", style="bold")
        for bp in plan.blueprints:
            cost = f"   ~{_isk(bp.copy_cost)}" if bp.copy_cost is not None else "   —"
            text.append(f"    {self._tname(bp.blueprint_type_id)}{cost}\n")
        total = plan.total_blueprint_cost
        if total is not None:
            text.append(f"  total blueprints (copy): ~{_isk(total)}\n", style="dim")

    def _append_self_source(self, text: Text) -> None:
        plan = self._build.plan
        if plan is None or not (plan.mine or plan.build):
            return
        text.append("\nto self-source\n", style="bold")
        if plan.mine:
            haul = _volume(plan.total_mine_volume)
            text.append(f"  mine & refine  ({haul} to haul)\n", style="cyan")
            for line in plan.mine:
                ore = _truncate(self._tname(line.ore_type_id), 24)
                vol = _volume(line.volume)
                text.append(
                    f"    ~{line.quantity:>10,}  {ore:<24} {line.location:<8} {vol:>10}\n",
                    style="cyan",
                )
        if plan.build:
            text.append("  build (dependencies first)\n", style="green")
            for step in plan.build:
                name = self._tname(step.product_type_id)
                text.append(
                    f"    {step.quantity:,} x {name}  ({step.runs} run(s))\n", style="green"
                )
                for inp in step.inputs:
                    imat = _truncate(self._tname(inp.type_id), 24)
                    text.append(
                        f"        {inp.quantity:>10,}  {imat:<24} {_volume(inp.volume):>10}\n",
                        style="dim green",
                    )
        ores = len(plan.mine)
        acquire = f"  to acquire: {ores} ore type(s), {_volume(plan.total_mine_volume)}"
        if plan.build:
            acquire += f" · {len(plan.build)} sub-build(s)"
        if plan.buy:
            acquire += f" · {len(plan.buy)} to buy"
        text.append(f"{acquire}\n", style="dim")

    _BUY_METHOD: ClassVar[dict[str, str]] = {
        "buy": "buy",
        "reaction": "buy or react",
        "pi": "buy or PI",
    }

    def _append_buy(self, text: Text) -> None:
        plan = self._build.plan
        if plan is None or not plan.buy:
            return
        text.append("\nbuy or produce (reactions / PI not modelled yet)\n", style="bold")
        for item in plan.buy:
            name = _truncate(self._tname(item.type_id), 28)
            cost = _isk(item.buy_cost) if item.buy_cost is not None else "—"
            tag = self._BUY_METHOD.get(item.method, item.method)
            text.append(f"    {item.quantity:>8,}  {name:<28} {cost:>9}  {tag}\n")
        total = plan.total_buy_cost_items
        if total is not None:
            text.append(f"  total to buy: {_isk(total)}\n", style="dim")

    def _append_tips(self, text: Text) -> None:
        """Quick skills that ease self-sourcing — less ore to mine, or a new build unlocked."""
        tips = self._build.training_tips
        if not tips:
            return
        text.append("\ntrain to help\n", style="bold magenta")
        for tip in tips[:3]:
            name = self._tname(tip.skill_id)
            if tip.unlocks_type_id is not None:
                benefit = f"unlocks building {self._tname(tip.unlocks_type_id)}"
            else:
                benefit = f"~{tip.ore_reduction:.0%} less ore to mine"
            text.append(
                f"  {name} L{tip.current_level}→{tip.target_level}"
                f"  (~{_duration(tip.train_seconds)})  {benefit}\n",
                style="magenta",
            )

    def action_export(self) -> None:
        """Write the recipe detail to a Markdown file in the working directory."""
        slug = _slug(self._title) or "recipe"
        path = Path.cwd() / f"{slug}-recipe.md"
        try:
            path.write_text(recipe_markdown(self._build, self._names), encoding="utf-8")
        except OSError as error:
            self.query_one("#mathint", Static).update(f"export failed: {error}")
            return
        self.query_one("#mathint", Static).update(f"saved to {path}")


class SdeUpdateScreen(ModalScreen[None]):
    """Launch-time prompt when the local SDE is missing or a newer dump is published.
    Offers to download it (blocking, off the event loop) or skip for this session."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "skip", "Not now")]

    DEFAULT_CSS = """
    SdeUpdateScreen { align: center middle; }
    SdeUpdateScreen #sdebox {
        width: 66;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    SdeUpdateScreen #sdemsg { padding: 0 0 1 0; }
    SdeUpdateScreen #sdestatus { padding: 1 0 0 0; color: $text-muted; }
    SdeUpdateScreen #sdeprogress { display: none; padding: 1 0 0 0; width: 100%; }
    SdeUpdateScreen #sdebuttons { height: auto; align: right middle; }
    SdeUpdateScreen Button { margin: 0 0 0 2; }
    """

    def __init__(self, state: SdeState, download_sde_fn: DownloadSdeFn) -> None:
        super().__init__()
        self._state = state
        self._download_sde_fn = download_sde_fn

    def compose(self) -> ComposeResult:
        with Vertical(id="sdebox"):
            yield Static(self._message(), id="sdemsg")
            with Horizontal(id="sdebuttons"):
                yield Button("Not now", id="sde-skip", compact=True)
                yield Button(
                    self._download_label(), id="sde-download", variant="primary", compact=True
                )
            yield Static("", id="sdestatus")
            yield ProgressBar(id="sdeprogress", show_eta=True)

    def _message(self) -> Text:
        text = Text()
        if self._state is SdeState.MISSING:
            text.append("The EVE static data (SDE) isn't installed.\n", style="bold")
            text.append(
                "It powers the Crafting tab and asset ISK values. "
                "Download it now? (~250 MB, one-time)",
                style="dim",
            )
        else:  # STALE
            text.append("A newer EVE static data (SDE) dump is available.\n", style="bold")
            text.append("Update now? (~250 MB)", style="dim")
        return text

    def _download_label(self) -> str:
        return "Download" if self._state is SdeState.MISSING else "Update"

    def action_skip(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sde-skip":
            self.dismiss()
        elif event.button.id == "sde-download":
            self.run_worker(self._download(), exclusive=True)

    async def _download(self) -> None:
        for button in self.query(Button):
            button.disabled = True
        status = self.query_one("#sdestatus", Static)
        status.update("Downloading… this can take a minute.")
        bar = self.query_one("#sdeprogress", ProgressBar)
        bar.display = True
        # The download runs in a worker thread, so marshal each progress tick back onto
        # the UI thread. total=None leaves the bar indeterminate (server sent no length).
        app = self.app

        def report(downloaded: int, total: int | None) -> None:
            app.call_from_thread(bar.update, total=total, progress=downloaded)

        ok = await self._download_sde_fn(report)
        if ok:
            self.dismiss()
            return
        bar.display = False
        status.update("Download failed — check your connection, or continue without it.")
        self.query_one("#sde-skip", Button).disabled = False
        self.query_one("#sde-download", Button).disabled = False


class TradingScreen(Screen[None]):
    """Per-character advisor view: opportunities plus character and skill-queue."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "app.pop_screen", "Switch character"),
        Binding("slash", "focus_search", "Search", show=False),
        # Shift+h/l (and Shift+arrows) cycle tabs from anywhere. Non-priority so a
        # focused Input still gets them for typing/selection; the tables/tree don't
        # bind these (they use bare h/l and arrows), so the keys bubble up here.
        Binding("H,shift+left", "prev_tab", "Prev tab", show=False),
        Binding("L,shift+right", "next_tab", "Next tab", show=False),
    ]

    # Pane id -> the scrollable widget to focus when its tab is activated, so the arrow
    # keys (and hjkl) scroll it straight away without a click or tab-in.
    _TAB_FOCUS: ClassVar[dict[str, str]] = {
        "overview": "#digest",
        "trading": "#my-buys",
        "queue": "#skillqueue",
        "skills": "#skilltree",
        "assets": "#assettree",
        "industry-tab": "#industry",
        "manufacturing-tab": "#manufacturing",
    }
    # Tabs whose "/" shortcut jumps to a filter box, by pane id -> that box's selector.
    _TAB_SEARCH: ClassVar[dict[str, str]] = {
        "assets": "#assetsearch",
        "manufacturing-tab": "#manufacturing-search",
    }

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
    TradingScreen #digest, TradingScreen #skillqueue,
    TradingScreen #skilltree, TradingScreen #assettree, TradingScreen #industry,
    TradingScreen #manufacturing {
        margin: 0 1;
        height: 1fr;
    }
    TradingScreen #digest-body { padding: 1 2; }
    TradingScreen #my-buys, TradingScreen #my-sells {
        margin: 0 1;
        height: auto;
        max-height: 8;
    }
    TradingScreen #assetsearch { margin: 0 1; }
    /* Make the selected row unmistakable — the theme default is a muted purple
       that's easy to lose, especially over zebra stripes. */
    TradingScreen DataTable > .datatable--cursor {
        background: $accent;
        color: $background;
        text-style: bold;
    }
    TradingScreen #manufacturing-hint { padding: 0 2; color: $text-muted; }
    TradingScreen #manufacturing-search { margin: 0 1; }
    """

    def __init__(self, feed: RefreshFeed, interval_seconds: int, character_name: str) -> None:
        super().__init__()
        self._feed = feed
        self._interval = interval_seconds
        self._character_name = character_name
        self._report: OpportunityReport | None = None
        self._character: CharacterReport | None = None
        # Signatures of the data last drawn into each view, so a periodic refresh
        # doesn't rebuild it (resetting cursor/scroll/expansions) when unchanged.
        self._skills_key: object | None = None
        self._assets_key: object | None = None
        self._digest_key: object | None = None
        # Assets value from the last market scan (phase 2); None until the first scan,
        # so the phase-1 digest can show a placeholder and re-render when it arrives.
        self._digest_assets_value: float | None = None
        self._listings_key: object | None = None
        self._builds_key: object | None = None
        self._asset_query: str = ""
        # Last ISK values drawn per place. Phase 1 of a refresh re-renders assets before
        # the market scan supplies fresh values; reusing these keeps its signature equal to
        # phase 2's, so an unchanged refresh doesn't rebuild the tree (and reset expansions).
        self._location_values: dict[int, float] = {}
        self._mfg_query: str = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._character_name, id="location")
        yield Static("Fetching market data…", id="status")
        with TabbedContent():
            with TabPane("Overview", id="overview"):
                with Horizontal(id="stats"):
                    yield Static(id="stat-wallet", classes="stat")
                    yield Static(id="stat-assets", classes="stat")
                    yield Static(id="stat-sp", classes="stat")
                    yield Static(id="stat-slots", classes="stat")
                    yield Static(id="stat-industry", classes="stat")
                yield Static("", id="training")
                with VerticalScroll(id="digest"):
                    yield Static("", id="digest-body")
            with TabPane("Trading", id="trading"):
                yield Static("YOUR BUY ORDERS", classes="section", id="my-buys-section")
                yield NavDataTable(id="my-buys", zebra_stripes=True, cursor_type="row")
                yield Static("YOUR SELL ORDERS", classes="section", id="my-sells-section")
                yield NavDataTable(id="my-sells", zebra_stripes=True, cursor_type="row")
            with TabPane("Skill Queue", id="queue"):
                yield NavDataTable(id="skillqueue", zebra_stripes=True, cursor_type="row")
            with TabPane("Skills", id="skills"):
                yield DepthTree[int]("Skills", id="skilltree")
            with TabPane("Assets", id="assets"):
                yield Input(placeholder="Search items…", id="assetsearch")
                yield DepthTree[AssetNode]("Assets", id="assettree")
            with TabPane("Industry", id="industry-tab"):
                yield NavDataTable(id="industry", zebra_stripes=True, cursor_type="row")
            with TabPane("Crafting", id="manufacturing-tab"):
                yield Static("", id="manufacturing-hint")
                yield Input(placeholder="Search products…", id="manufacturing-search")
                yield NavDataTable(id="manufacturing", zebra_stripes=True, cursor_type="row")
        yield Footer()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "skillqueue":
            self._open_skill_info(event)
            return
        if event.data_table.id == "industry":
            self._open_industry_job(event)
            return
        if event.data_table.id == "manufacturing":
            self._open_materials(event)
            return
        # Remaining tables (my-buys / my-sells) carry no drill-down.

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

    def _open_materials(self, event: DataTable.RowSelected) -> None:
        if self._report is None or event.row_key.value is None:
            return
        item_id = int(event.row_key.value)
        build = next((b for b in self._report.builds if b.blueprint_item_id == item_id), None)
        if build is None:
            return
        name = self._report.names.get(build.product_type_id, str(build.product_type_id))
        self.app.push_screen(MaterialsScreen(name, build, self._report.names))

    def _open_industry_job(self, event: DataTable.RowSelected) -> None:
        if self._character is None or event.row_key.value is None:
            return
        job_id = int(event.row_key.value)
        job = next((j for j in self._character.industry_jobs if j.job_id == job_id), None)
        if job is None:
            return
        subject = _job_subject_type(job)
        name = self._character.names.get(subject, str(subject))
        facility = self._character.names.get(job.facility_id, str(job.facility_id))
        self.app.push_screen(
            IndustryJobScreen(name, job, facility, self._character.captured_at)
        )

    def on_tree_node_selected(self, event: Tree.NodeSelected[object]) -> None:
        """Both trees share this handler; the node's data type says which it is: a
        Skills-tree leaf carries its skill id (int), an Assets-tree blueprint leaf its
        AssetNode. Everything else (groups, places, containers, non-blueprint items)
        carries no data and is ignored."""
        if self._character is None:
            return
        data = event.node.data
        if isinstance(data, int):
            self._open_skill_detail(data)
        elif isinstance(data, AssetNode):
            self._open_blueprint_detail(data)

    def _open_skill_detail(self, skill_id: int) -> None:
        assert self._character is not None
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

    def _open_blueprint_detail(self, node: AssetNode) -> None:
        assert self._character is not None
        blueprint = self._character.blueprints.get(node.item_id)
        if blueprint is None:  # only blueprint leaves carry data, but guard anyway
            return
        name = self._character.names.get(node.type_id, str(node.type_id))
        self.app.push_screen(BlueprintInfoScreen(name, node, blueprint))

    def _skill_name(self, skill_id: int, info: SkillReference | None) -> str:
        if info is not None:
            return info.name
        assert self._character is not None
        return self._character.names.get(skill_id, str(skill_id))

    def on_mount(self) -> None:
        for order_table in ("#my-buys", "#my-sells"):
            self.query_one(order_table, DataTable).add_columns(
                "Item", "Where", "Qty", "Your price", "Best other", "Status"
            )
        self.query_one("#skillqueue", DataTable).add_columns("Skill", "Time left", "Completion")
        self.query_one("#industry", DataTable).add_columns(
            "Activity", "Item", "Runs", "Time left", "Where"
        )
        self.query_one("#manufacturing", DataTable).add_columns("Product", "ME")
        self.run_worker(self._refresh(), exclusive=True)
        self.set_interval(self._interval, self._refresh)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Give the newly-shown tab's scrollable area focus, so the arrow keys and hjkl
        scroll it immediately instead of leaving focus on the tab bar."""
        pane = event.pane
        target = self._TAB_FOCUS.get(pane.id) if pane is not None and pane.id else None
        if target is not None:
            self.query_one(target).focus()

    def action_focus_search(self) -> None:
        """"/" jumps to the active tab's filter box (Assets / Crafting), if it has one."""
        active = self.query_one(TabbedContent).active
        selector = self._TAB_SEARCH.get(active)
        if selector is not None:
            self.query_one(selector, Input).focus()

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def _cycle_tab(self, step: int) -> None:
        """Move the active tab by `step`, wrapping around the tab strip."""
        tabbed = self.query_one(TabbedContent)
        pane_ids = [pane.id for pane in tabbed.query(TabPane) if pane.id is not None]
        if not pane_ids:
            return
        try:
            index = pane_ids.index(tabbed.active)
        except ValueError:
            return
        tabbed.active = pane_ids[(index + step) % len(pane_ids)]

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
        self.query_one("#location", Static).update(self._header_line(character_report))
        self._render_stats(character_report)
        self._render_training(character_report)
        self._render_skill_queue(character_report)
        self._render_skills(character_report)
        self._render_assets(character_report)
        self._render_industry(character_report)
        self._render_digest(character_report)

        # Phase 2: the market scan is slower; character info is already on screen.
        status.update("Updating ESI data…")
        try:
            report = await self._feed.opportunities(character_report)
        except Exception as error:
            status.update(f"[Market scan failed] {type(error).__name__}: {error}")
            return
        self._report = report
        self._render_listings(report)
        self._render_builds(report)
        # Assets value comes from the market scan; fill the tile (and the digest) now.
        assets_value = sum(report.location_values.values())
        self._digest_assets_value = assets_value
        self._set_tile("#stat-assets", "ASSETS", f"{_isk(assets_value)} ISK", "bold green")
        self._render_assets(character_report, report.location_values)  # now with ISK values
        self._render_digest(character_report)
        new_items = len(character_report.completed_transactions)
        updated = character_report.captured_at.astimezone().strftime("%H:%M:%S")
        status.update(f"{new_items} trades since your last visit · updated {updated}")

    def _header_line(self, report: CharacterReport) -> str:
        """Header identity: character, corp, alliance (if in one), current ship (if the
        scope granted it), and where they actually are now — all joined into one line."""
        parts = [self._character_name]
        for value in (report.corporation_name, report.alliance_name, report.ship_name):
            if value:
                parts.append(value)
        if report.current_location_name:
            parts.append(report.current_location_name)
        return "  ·  ".join(parts)

    def _set_tile(self, selector: str, label: str, value: str, value_style: str) -> None:
        content = Text()
        content.append(f"{label}\n", style="dim")
        content.append(value, style=value_style)
        self.query_one(selector, Static).update(content)

    def _render_stats(self, report: CharacterReport) -> None:
        self._set_tile(
            "#stat-wallet", "WALLET", f"{_isk(report.character.wallet_balance)} ISK", "bold green"
        )
        # Assets value needs market prices (phase 2); show the last value (or a placeholder
        # on first load) so it doesn't flicker to "…" on every refresh.
        assets = self._digest_assets_value
        assets_text = "…" if assets is None else f"{_isk(assets)} ISK"
        self._set_tile("#stat-assets", "ASSETS", assets_text, "bold green")
        self._set_tile("#stat-sp", "SKILL POINTS", f"{_isk(report.total_sp)} SP", "bold cyan")
        self._set_tile(
            "#stat-slots", "FREE ORDER SLOTS", str(report.character.free_order_slots), "bold cyan"
        )
        free_industry = str(_free_manufacturing_slots(report))
        self._set_tile("#stat-industry", "FREE INDUSTRY JOBS", free_industry, "bold cyan")

    def _render_digest(self, report: CharacterReport) -> None:
        """The Overview activity digest: a header of assets value and open/running counts,
        then what's happened — trades since your last visit, jobs ready to deliver, and
        skills that finished training. Skipped when unchanged, to keep scroll position."""
        reference = report.captured_at
        buys = [order for order in report.open_orders if order.is_buy_order]
        sells = [order for order in report.open_orders if not order.is_buy_order]
        running = [job for job in report.industry_jobs if _job_state(job, reference) == "active"]
        ready = [job for job in report.industry_jobs if _job_state(job, reference) == "ready"]
        finished = [
            entry
            for entry in report.skill_queue
            if entry.finish_date is not None and entry.finish_date <= reference
        ]
        txns = report.completed_transactions
        key = (
            len(buys),
            len(sells),
            len(running),
            tuple((job.job_id, _job_subject_type(job)) for job in ready),
            tuple((entry.skill_id, entry.finished_level) for entry in finished),
            tuple(
                (txn.transaction_id, txn.is_buy, txn.type_id, txn.quantity, txn.unit_price)
                for txn in txns
            ),
        )
        if key == self._digest_key:
            return
        self._digest_key = key

        text = Text()
        text.append(str(len(buys)), style="bold")
        text.append(" buy / ", style="dim")
        text.append(str(len(sells)), style="bold")
        text.append(" sell orders open", style="dim")
        text.append("   ·   ", style="dim")
        text.append(str(len(running)), style="bold")
        text.append(" jobs running\n", style="dim")

        self._digest_transactions(text, report, txns)
        self._digest_ready_jobs(text, report, ready)
        self._digest_finished_training(text, report, finished)
        self.query_one("#digest-body", Static).update(text)

    def _theme_color(self, *names: str) -> str:
        """The first set colour among ``names`` from the active theme, so the digest tracks
        the user's theme instead of hard-coded colours. Falls back to the last name."""
        theme = self.app.current_theme
        for name in names:
            value = getattr(theme, name, None)
            if value:
                return str(value)
        return names[-1]

    def _digest_header(self, text: Text, title: str) -> None:
        text.append(f"\n{title}\n", style=f"bold {self._theme_color('accent', 'primary')}")

    def _digest_transactions(
        self, text: Text, report: CharacterReport, txns: list[WalletTransaction]
    ) -> None:
        self._digest_header(text, "Since your last visit")
        if not txns:
            text.append("  nothing new\n", style="dim italic")
            return
        buy_color = self._theme_color("warning", "secondary")
        sell_color = self._theme_color("success", "primary")
        for txn in txns:
            name = report.names.get(txn.type_id, str(txn.type_id))
            verb, style = ("Bought", buy_color) if txn.is_buy else ("Sold", sell_color)
            text.append(f"  {verb:<7}", style=style)
            text.append(f"{txn.quantity:,} x {name}")
            text.append(f"   {_isk(txn.quantity * txn.unit_price)} ISK\n", style="dim")

    def _digest_ready_jobs(
        self, text: Text, report: CharacterReport, ready: list[IndustryJob]
    ) -> None:
        self._digest_header(text, "Ready to deliver")
        if not ready:
            text.append("  none\n", style="dim italic")
            return
        marker_style = f"bold {self._theme_color('success', 'primary')}"
        for job in sorted(ready, key=lambda job: job.end_date):
            subject = _job_subject_type(job)
            name = report.names.get(subject, str(subject))
            text.append("  ● ", style=marker_style)
            text.append(name)
            text.append(f"   {_activity_name(job.activity_id)} x{job.runs:,}\n", style="dim")

    def _digest_finished_training(
        self, text: Text, report: CharacterReport, finished: list[SkillQueueEntry]
    ) -> None:
        self._digest_header(text, "Finished training")
        if not finished:
            text.append("  none\n", style="dim italic")
            return
        marker_style = f"bold {self._theme_color('success', 'primary')}"
        for entry in sorted(finished, key=lambda entry: entry.queue_position):
            name = self._skill_name(entry.skill_id, report.skill_reference.get(entry.skill_id))
            text.append("  ✓ ", style=marker_style)
            text.append(f"{name} → L{entry.finished_level}\n")

    def _render_listings(self, report: OpportunityReport) -> None:
        # Both overlays share one signature — a periodic refresh with unchanged orders
        # leaves the cursor/scroll of both tables alone.
        key = (tuple(report.listing_buys), tuple(report.listing_sells))
        if key == self._listings_key:
            return
        self._listings_key = key
        self._fill_listings("#my-buys", report.listing_buys, report, "overcut")
        self._fill_listings("#my-sells", report.listing_sells, report, "undercut")

    def _fill_listings(
        self, table_id: str, statuses: list[ListingStatus], report: OpportunityReport, beaten: str
    ) -> None:
        table = self.query_one(table_id, DataTable)
        table.clear()
        for status in statuses:
            name = report.names.get(status.type_id, str(status.type_id))
            where = str(status.location_id)
            if self._character is not None:
                where = self._character.names.get(status.location_id, where)
            best = _isk(status.best_competing) if status.best_competing is not None else "—"
            style = "green" if status.is_best else "red"
            label = "✓ best" if status.is_best else f"✗ {beaten}"
            table.add_row(
                Text(name),
                Text(where, style="dim"),
                Text(f"{status.volume_remain:,}", justify="right"),
                Text(_isk(status.price), justify="right"),
                Text(best, justify="right", style="dim"),
                Text(label, style=style),
                key=str(status.order_id),
            )

    def _sorted_builds(self, report: OpportunityReport) -> list[tuple[BuildOpportunity, int]]:
        """Owned builds as (build, copies-owned), filtered by the product search and sorted
        alphabetically by product. Copies of a blueprint at the same research level are
        identical, so they collapse to one row carrying the count."""
        query = self._mfg_query.strip().lower()

        def product(build: BuildOpportunity) -> str:
            return report.names.get(build.product_type_id, str(build.product_type_id))

        counts: dict[tuple[int, int], int] = {}
        for build in report.builds:
            row_id = (build.blueprint_type_id, build.material_efficiency)
            counts[row_id] = counts.get(row_id, 0) + 1

        rows: list[tuple[BuildOpportunity, int]] = []
        seen: set[tuple[int, int]] = set()
        for build in report.builds:
            row_id = (build.blueprint_type_id, build.material_efficiency)
            if row_id in seen or query not in product(build).lower():
                continue
            seen.add(row_id)
            rows.append((build, counts[row_id]))

        rows.sort(key=lambda row: (product(row[0]).lower(), row[0].material_efficiency))
        return rows

    def _render_builds(self, report: OpportunityReport) -> None:
        """Owned blueprints, filtered and alphabetical — just the product and its ME. The
        self-source detail (what to mine/build/buy, and where) is in the row's popup, so the
        list stays scannable and carries no (misleading) mining ISK. The hint says why the
        tab is empty when it is, so it's never blank."""
        # The hint has no cursor, so refresh it every call.
        self.query_one("#manufacturing-hint", Static).update(self._manufacturing_hint(report))

        rows = self._sorted_builds(report)
        key = (tuple(rows), self._mfg_query)
        if key == self._builds_key:
            return
        self._builds_key = key
        table = self.query_one("#manufacturing", DataTable)
        table.clear()
        for build, copies in rows:
            name = report.names.get(build.product_type_id, str(build.product_type_id))
            product = Text(name)
            if copies > 1:
                product.append(f"  ×{copies}", style="cyan")  # noqa: RUF001 (copies owned)
            table.add_row(
                product,
                Text(f"ME {build.material_efficiency}", justify="right", style="dim"),
                key=str(build.blueprint_item_id),
            )

    def _manufacturing_hint(self, report: OpportunityReport) -> Text:
        if report.builds:
            return Text(
                f"{len(report.builds)} blueprint(s) — select one for its self-source recipe: "
                "what to mine (and where), build, or buy."
            )
        if not report.sde_available:
            hint = Text()
            hint.append("Crafting cost needs the EVE SDE. Download it once:  ", style="yellow")
            hint.append("uv run evetrader sde", style="bold")
            return hint
        owns_blueprints = self._character is not None and bool(self._character.blueprints)
        if not owns_blueprints:
            return Text("No blueprints owned — this tab costs the blueprints you hold.")
        return Text("No owned blueprint is manufacturable from the SDE.")

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

    def _render_industry(self, report: CharacterReport) -> None:
        """Running/ready industry jobs: Activity / Item / Runs / Time left / Where.
        Ready-to-deliver jobs sort to the top; each row is coloured by state."""
        table = self.query_one("#industry", DataTable)
        table.clear()
        reference = report.captured_at
        jobs = sorted(
            report.industry_jobs,
            key=lambda job: (0 if _job_state(job, reference) == "ready" else 1, job.end_date),
        )
        for job in jobs:
            state = _job_state(job, reference)
            marker, style, _ = _JOB_STATES[state]
            subject = _job_subject_type(job)
            item = report.names.get(subject, str(subject))
            where = report.names.get(job.facility_id, str(job.facility_id))
            time_style = "green" if state == "ready" else "yellow"
            table.add_row(
                Text(f"{marker}{_activity_name(job.activity_id)}", style=style),
                Text(item),
                Text(f"{job.runs:,}", justify="right"),
                Text(_job_time_left(job, reference), style=time_style),
                Text(where, style="dim"),
                key=str(job.job_id),
            )

    def _render_skills(self, report: CharacterReport) -> None:
        """All trained skills as a tree grouped by skill category; a leaf's data is
        its skill id so selecting it opens the detail popup.

        Skipped when the skill set is unchanged, so a periodic refresh leaves the
        tree (and the user's expand/collapse state) untouched.
        """
        # Rebuild when the trained set changes, or the queue's shape does (so the
        # queued-skill highlight tracks queue edits) — but not on continuous SP, which
        # would collapse the tree every tick.
        trained_key = tuple(sorted((s.skill_id, s.trained_skill_level) for s in report.skills))
        queue_key = tuple(
            sorted((e.skill_id, e.finished_level, e.queue_position) for e in report.skill_queue)
        )
        key = (trained_key, queue_key)
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
                label.append_text(
                    _skill_queue_pips(
                        skill.skill_id,
                        skill.trained_skill_level,
                        report.skill_queue,
                        report.captured_at,
                    )
                )
                branch.add_leaf(label, data=skill.skill_id)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "assetsearch" and self._character is not None:
            self._asset_query = event.value
            self._render_assets(self._character)
        elif event.input.id == "manufacturing-search" and self._report is not None:
            self._mfg_query = event.value
            self._render_builds(self._report)

    def _render_assets(
        self, report: CharacterReport, location_values: dict[int, float] | None = None
    ) -> None:
        """All assets as a tree of places -> items -> container/ship contents, filtered to
        the search box. Each place shows its total ISK value once the market phase supplies
        prices (``location_values``).

        Skipped when neither the assets, the query, nor a shown value changed, so a periodic
        refresh leaves the tree (and the user's expand/collapse state) alone.
        """
        # Phase 2 supplies fresh values; phase 1 (location_values is None) reuses the last
        # ones so its render signature matches phase 2's and an unchanged tick is a no-op.
        if location_values is not None:
            self._location_values = location_values
        values = self._location_values
        query = self._asset_query.strip().lower()
        # Key on the *displayed* value strings, not raw floats, so tiny price drift between
        # refreshes doesn't rebuild the tree (and reset expansions) every tick.
        shown = tuple(sorted((place, _isk(value)) for place, value in values.items()))
        key = (_asset_signature(report.assets), query, shown)
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
            place_id = location.location_id
            label = self._place_label(place_id, report, values.get(place_id))
            place = tree.root.add(label, expand=True)
            self._add_asset_children(
                place, location.items, report, query=query, show_all=not query, parent_flag=""
            )

    def _place_label(
        self, location_id: int, report: CharacterReport, value: float | None
    ) -> Text | str:
        """A place's name, with its total ISK value appended once prices are known."""
        name = self._location_label(location_id, report)
        if not value:
            return name
        label = Text(name)
        label.append(f"   {_isk(value)} ISK", style="dim")
        return label

    def _add_asset_children(
        self,
        parent: TreeNode[AssetNode],
        children: tuple[AssetNode, ...],
        report: CharacterReport,
        *,
        query: str,
        show_all: bool,
        parent_flag: str,
    ) -> None:
        """Render a node's contents, grouping ship/container compartments (Fit, Cargo,
        Drone Bay…) under headings; loose items list directly."""
        # Fittings only exist directly inside a ship; a slot-flagged item inside a module
        # (parent already in a slot) is loaded ammo, not a fitting.
        allow_fit = not parent_flag.startswith(_SLOT_PREFIXES)
        # A loaded charge is a ship-level asset carrying its weapon's slot flag. Pull these
        # out and nest each under the module in the same slot, so a crystal shows under its
        # laser instead of as its own bogus "High Slot" fitting.
        # NB: ESI only returns *persistent* loaded charges this way — frequency/mining
        # crystals, scripts, scan probes. Consumable ammo loaded into a weapon (missiles,
        # hybrid/projectile charges) is NOT in the assets payload at all; only the spare
        # stack in cargo is. So a missile launcher legitimately shows no loaded ammo here —
        # it's a data gap, not a bug, and we can't tell a loaded launcher from an empty one.
        loaded: dict[str, list[AssetNode]] = defaultdict(list)
        fittings: list[AssetNode] = []
        for child in children:
            if (
                allow_fit
                and child.location_flag.startswith(_SLOT_PREFIXES)
                and child.type_id in report.charge_type_ids
            ):
                loaded[child.location_flag].append(child)
            else:
                fittings.append(child)

        def matches(item: AssetNode) -> bool:
            # Keep a module visible when its loaded ammo matches, so the nested ammo stays
            # reachable by search.
            return self._asset_matches(item, report, query) or any(
                self._asset_matches(c, report, query) for c in loaded.get(item.location_flag, ())
            )

        visible = fittings if show_all else [c for c in fittings if matches(c)]
        sections: dict[str | None, list[AssetNode]] = defaultdict(list)
        for child in visible:
            sections[_asset_section(child.location_flag, allow_fit=allow_fit)].append(child)

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
                    node,
                    child,
                    report,
                    query=query,
                    show_all=show_all,
                    section=section,
                    loaded=tuple(loaded.get(child.location_flag, ())),
                )

    def _add_asset_node(
        self,
        parent: TreeNode[AssetNode],
        item: AssetNode,
        report: CharacterReport,
        *,
        query: str,
        show_all: bool,
        section: str | None,
        loaded: tuple[AssetNode, ...] = (),
    ) -> None:
        name = report.names.get(item.type_id, str(item.type_id))
        custom = report.asset_names.get(item.item_id)
        expandable = bool(item.children) or bool(loaded)
        label = Text()
        if section == "Fit":  # fitted module: slot name first, then the item, no number
            label.append(f"{_slot_label(item.location_flag)}   ", style="cyan")
        if item.children and custom and custom != name:
            # A named container/ship: its player name, then its type dimmed.
            label.append(custom, style="bold")
            label.append(f"  {name}", style="dim")
        else:
            label.append(name, style="bold" if expandable else "")
        if item.quantity > 1:
            label.append(f"  ×{item.quantity:,}", style="cyan")  # noqa: RUF001 (multiplier)
        # Blueprints are the only items with a detail popup; tag them (BPO/BPC) so it's
        # clear which rows open one, and only they carry data so only they respond.
        blueprint = report.blueprints.get(item.item_id) if not item.children else None
        if blueprint is not None:
            label.append(f"  {'BPO' if blueprint.runs == -1 else 'BPC'}", style="magenta")
        if not expandable:
            parent.add_leaf(label, data=item if blueprint is not None else None)
            return
        node = parent.add(label, expand=bool(query))
        # Loaded ammo/crystals nest under their module, listed like loose items.
        for charge in sorted(loaded, key=lambda c: self._asset_label_name(c, report)):
            self._add_asset_node(node, charge, report, query=query, show_all=True, section=None)
        if not item.children:
            return
        # A container/ship matched by name reveals everything inside it.
        child_show_all = show_all or (bool(query) and query in name.lower())
        self._add_asset_children(
            node,
            item.children,
            report,
            query=query,
            show_all=child_show_all,
            parent_flag=item.location_flag,
        )

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
        download_sde_fn: DownloadSdeFn | None = None,
        sde_check_fn: SdeCheckFn | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._make_feed = make_feed
        self._login_fn = login_fn
        self._remove_token_fn = remove_token_fn
        self._interval = interval_seconds
        self._download_sde_fn = download_sde_fn
        self._sde_check_fn = sde_check_fn
        self._last_character_id: int | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Select a character", id="hint")
        yield NavOptionList(id="characters")
        yield Static("", id="picker_status")
        yield Footer()

    def on_mount(self) -> None:
        self._reload()
        # Before the user picks a character, offer to download/update the SDE if it's
        # missing or a newer dump is published — so Crafting and asset values are ready.
        if self._sde_check_fn is not None and self._download_sde_fn is not None:
            self.run_worker(self._prompt_sde_if_needed(), exclusive=False)

    async def _prompt_sde_if_needed(self) -> None:
        assert self._sde_check_fn is not None and self._download_sde_fn is not None
        state = await self._sde_check_fn()
        if state in (SdeState.MISSING, SdeState.STALE):
            await self.app.push_screen_wait(SdeUpdateScreen(state, self._download_sde_fn))

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
        record = next((r for r in self._store.records() if r.character_id == character_id), None)
        name = record.name if record is not None else str(character_id)
        self.app.push_screen(TradingScreen(self._make_feed(character_id), self._interval, name))

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
        download_sde_fn: DownloadSdeFn | None = None,
        sde_check_fn: SdeCheckFn | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._make_feed = make_feed
        self._login_fn = login_fn
        self._remove_token_fn = remove_token_fn
        self._interval = interval_seconds
        self._theme_name = theme
        self._download_sde_fn = download_sde_fn
        self._sde_check_fn = sde_check_fn

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
            self._download_sde_fn,
            self._sde_check_fn,
        )
