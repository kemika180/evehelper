"""Textual TUI for building EVE skill-training plans.

Standalone: depends only on Textual and the pure ``planner`` core (no imports
from the eve_trading package, no network). Add prioritised goals (a ship mastery
tier or an individual skill), tune your character attributes, and the plan is
recomputed on every edit: prerequisites first, higher-priority goals earlier,
ties broken by shortest training time. It also recommends an optimal attribute
remap without changing the plan unless you apply it.

Run: ``uv run python tools/skillplan/tui.py``
"""

from __future__ import annotations

import difflib
from dataclasses import replace
from typing import ClassVar

import planner
from planner import (
    Attributes,
    Goal,
    PlanEntry,
    PlanError,
    SortMode,
    format_duration,
    format_importable,
    load_air_plans,
    magic_14,
    match_air_plan,
    parse_wishlist,
    recommend_remap,
    roman,
)
from textual import on
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    TextArea,
)

_LEVEL_OPTIONS = [(roman(level), level) for level in range(1, 6)]
_SORT_OPTIONS = [
    ("Goal priority, then shortest", SortMode.OPTIMIZED.value),
    ("Shortest time (ignore goals)", SortMode.SHORTEST.value),
    ("As entered / pasted", SortMode.ENTERED.value),
    ("Goal priority, then longest", SortMode.LONGEST.value),
]
_GOAL_TYPES = [
    ("Ship mastery", "ship"),
    ("Skill", "skill"),
    ("Core skills (Magic 14)", "magic14"),
    ("AIR / career plan", "air"),
]

# attribute input id -> (label, Attributes field)
_ATTR_FIELDS = (
    ("attr_per", "Perception", "perception"),
    ("attr_wil", "Willpower", "willpower"),
    ("attr_int", "Intelligence", "intelligence"),
    ("attr_mem", "Memory", "memory"),
    ("attr_cha", "Charisma", "charisma"),
    ("attr_imp", "Implant +", "implant"),
)


class SkillPlanApp(App[None]):
    TITLE = "EVE Skill Planner"
    CSS = """
    #body { height: 1fr; }
    #left { width: 54; border-right: solid $panel; padding: 0 1; }
    #right { width: 1fr; padding: 0 1; }
    .heading { text-style: bold; color: $accent; margin: 1 0 0 0; }

    /* full-width sidebar controls */
    #goal_type, #goal_level, #goal_name, #air_plan, #add, #add_paste, #remap, #apply_remap {
        width: 100%;
    }
    #paste { width: 100%; height: 7; border: round $panel; margin-bottom: 1; }

    .row { height: auto; }
    /* three buttons share the row instead of overflowing it */
    #goal_buttons { height: auto; margin-bottom: 1; }
    #goal_buttons Button { width: 1fr; min-width: 0; margin: 0 1 0 0; }

    ListView { height: auto; max-height: 10; border: round $panel; margin-bottom: 1; }

    .attr-row { height: auto; }
    .attr-label { width: 16; height: 3; content-align: left middle; }
    .attr-input { width: 1fr; }

    #sort_row { height: auto; }
    #sort_row > Label { width: 6; height: 3; content-align: left middle; }
    #sort { width: 1fr; }
    #plan { height: 1fr; }  /* fixed-fill so it reflows immediately on update */

    #remap_advice { color: $text-muted; margin: 1 0; height: auto; }
    #totals { text-style: bold; margin-top: 1; height: auto; }
    #status { color: $warning; height: auto; }
    Input { margin-bottom: 1; }
    Select { margin-bottom: 1; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+e", "export", "Export plan"),
        ("ctrl+r", "remap", "Recommend remap"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._skills = planner.load_skills()
        self._masteries = planner.load_masteries()
        self._air = load_air_plans()
        self._goals: list[Goal] = []
        self._attrs = Attributes()
        self._sort = SortMode.OPTIMIZED
        self._plan: list[PlanEntry] = []
        self._advice_attrs: Attributes | None = None

    # ---- layout ----------------------------------------------------------- #
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with VerticalScroll(id="left"):
                yield Label("Add goal", classes="heading")
                yield Select(_GOAL_TYPES, value="ship", allow_blank=False, id="goal_type")
                yield Input(placeholder="Ship or skill name", id="goal_name")
                air_options = [(name, name) for name in sorted(self._air)]
                yield Select(air_options, prompt="Select AIR plan", id="air_plan")
                yield Select(_LEVEL_OPTIONS, value=5, allow_blank=False, id="goal_level")
                yield Button("Add goal", variant="primary", id="add")

                yield Label("…or paste a skill list", classes="heading")
                yield TextArea(id="paste")
                yield Button("Add pasted list", id="add_paste")
                yield Label("", id="status")

                yield Label("Goals (priority order)", classes="heading")
                yield ListView(id="goals")
                with Horizontal(id="goal_buttons"):
                    yield Button("Up", id="up")
                    yield Button("Down", id="down")
                    yield Button("Remove", id="remove")

                yield Label("Attributes", classes="heading")
                for input_id, label, field in _ATTR_FIELDS:
                    with Horizontal(classes="attr-row"):
                        yield Label(label, classes="attr-label")
                        value = str(getattr(self._attrs, field))
                        yield Input(value, id=input_id, classes="attr-input")
                yield Button("Recommend remap", id="remap")
                yield Label("", id="remap_advice")
                yield Button("Apply remap", id="apply_remap", disabled=True)
            with Vertical(id="right"):
                with Horizontal(id="sort_row"):
                    yield Label("Sort", classes="attr")
                    yield Select(
                        _SORT_OPTIONS,
                        value=SortMode.OPTIMIZED.value,
                        allow_blank=False,
                        id="sort",
                    )
                yield DataTable(id="plan")
                yield Label("", id="totals")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#plan", DataTable)
        table.cursor_type = "row"
        table.add_columns("#", "Skill", "Lv", "SP", "Time", "Cumulative", "For")
        self._update_goal_inputs()
        self._refresh_goals()
        self._recompute()

    @on(Select.Changed, "#goal_type")
    def _goal_type_changed(self) -> None:
        self._update_goal_inputs()

    def _update_goal_inputs(self) -> None:
        """Show only the inputs that apply to the selected goal type."""
        goal_type = self._select_value("#goal_type")
        self.query_one("#goal_name", Input).display = goal_type in ("ship", "skill")
        self.query_one("#goal_level", Select).display = goal_type in ("ship", "skill", "magic14")
        self.query_one("#air_plan", Select).display = goal_type == "air"

    # ---- goal management -------------------------------------------------- #
    @on(Button.Pressed, "#add")
    def _add_goal(self) -> None:
        goal_type = self._select_value("#goal_type")
        level = int(str(self._select_value("#goal_level")))
        if goal_type == "air":
            selected = self._select_value("#air_plan")
            name = selected if isinstance(selected, str) else ""
            if not name:
                self._status("Select an AIR plan.")
                return
        else:
            name = self.query_one("#goal_name", Input).value.strip()
            if goal_type != "magic14" and not name:
                self._status("Enter a ship or skill name.")
                return
        try:
            goal = self._build_goal(goal_type, name, level)
        except PlanError as error:
            self._status(str(error).splitlines()[0])
            return
        self._goals.append(goal)
        self.query_one("#goal_name", Input).value = ""
        self._status("")
        self._refresh_goals()
        self._recompute()

    def _build_goal(self, goal_type: object, name: str, level: int) -> Goal:
        if goal_type == "magic14":
            return Goal(label=f"Magic 14 {roman(level)}", skills=magic_14(self._skills, level))
        if goal_type == "air":
            plan = match_air_plan(name, self._air)
            if plan is None:
                close = difflib.get_close_matches(name, list(self._air), n=1)
                hint = f" (did you mean {close[0]!r}?)" if close else ""
                raise PlanError(f"unknown AIR plan {name!r}{hint}")
            return Goal(label=plan, skills=self._air[plan])
        if goal_type == "ship":
            ship = self._masteries.find_ship(name)
            if ship is None:
                raise PlanError(f"unknown ship {name!r}")
            skills = self._masteries.resolve(ship.type_id, level)
            return Goal(label=f"{ship.name} Mastery {roman(level)}", skills=skills)
        skill = planner.find_skill(name, self._skills)
        if skill is None:
            raise PlanError(f"unknown skill {name!r}")
        return Goal(label=f"{skill.name} {roman(level)}", skills={skill.type_id: level})

    @on(Select.Changed, "#sort")
    def _sort_changed(self) -> None:
        self._sort = SortMode(str(self._select_value("#sort")))
        self._recompute()

    @on(Button.Pressed, "#add_paste")
    def _add_pasted(self) -> None:
        """Add a whole pasted 'Skill Name <level>' block as one priority goal."""
        text = self.query_one("#paste", TextArea).text
        if not text.strip():
            self._status("Paste some 'Skill Name <level>' lines first.")
            return
        try:
            skills = parse_wishlist(text, self._skills)
        except PlanError as error:
            issues = str(error).splitlines()[1:]
            first = issues[0].strip() if issues else str(error)
            more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            self._status(f"{first}{more}")
            return
        self._goals.append(Goal(label=f"Pasted list ({len(skills)} skills)", skills=skills))
        self.query_one("#paste", TextArea).text = ""
        self._status("")
        self._refresh_goals()
        self._recompute()

    @on(Button.Pressed, "#remove")
    def _remove_goal(self) -> None:
        index = self.query_one("#goals", ListView).index
        if index is not None and 0 <= index < len(self._goals):
            del self._goals[index]
            self._refresh_goals()
            self._recompute()

    @on(Button.Pressed, "#up")
    def _move_up(self) -> None:
        self._move(-1)

    @on(Button.Pressed, "#down")
    def _move_down(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        listview = self.query_one("#goals", ListView)
        index = listview.index
        if index is None:
            return
        target = index + delta
        if not (0 <= target < len(self._goals)):
            return
        self._goals[index], self._goals[target] = self._goals[target], self._goals[index]
        self._refresh_goals()
        listview.index = target
        self._recompute()

    def _refresh_goals(self) -> None:
        listview = self.query_one("#goals", ListView)
        current = listview.index
        listview.clear()
        for position, goal in enumerate(self._goals, start=1):
            count = len(goal.skills)
            listview.append(ListItem(Label(f"{position}. {goal.label}  ({count} skills)")))
        if self._goals:
            listview.index = min(current or 0, len(self._goals) - 1)

    # ---- attributes + remap ---------------------------------------------- #
    @on(Input.Changed)
    def _attr_changed(self, event: Input.Changed) -> None:
        if not (event.input.id or "").startswith("attr_"):
            return
        field = {input_id: fld for input_id, _, fld in _ATTR_FIELDS}[event.input.id or ""]
        try:
            value = int(event.value)
        except ValueError:
            return
        self._attrs = replace(self._attrs, **{field: value})
        self._advice_attrs = None
        self.query_one("#apply_remap", Button).disabled = True
        self._recompute()

    @on(Button.Pressed, "#remap")
    def action_remap(self) -> None:
        if not self._plan:
            self._status("Add a goal first.")
            return
        advice = recommend_remap(self._plan, self._attrs)
        if advice.saved_minutes <= 1:
            self.query_one("#remap_advice", Label).update(
                "Your attributes are already optimal for this plan."
            )
            self._advice_attrs = None
            self.query_one("#apply_remap", Button).disabled = True
            return
        a = advice.attributes
        summary = (
            f"Per {a.perception} / Wil {a.willpower} / Int {a.intelligence} "
            f"/ Mem {a.memory} / Cha {a.charisma}"
        )
        self.query_one("#remap_advice", Label).update(
            f"Remap to {summary}\nsaves {format_duration(advice.saved_minutes)}."
        )
        self._advice_attrs = a
        self.query_one("#apply_remap", Button).disabled = False

    @on(Button.Pressed, "#apply_remap")
    def _apply_remap(self) -> None:
        if self._advice_attrs is None:
            return
        applied = self._advice_attrs
        for input_id, _, field in _ATTR_FIELDS:
            self.query_one(f"#{input_id}", Input).value = str(getattr(applied, field))
        self._attrs = replace(self._attrs, **{
            "perception": applied.perception,
            "willpower": applied.willpower,
            "intelligence": applied.intelligence,
            "memory": applied.memory,
            "charisma": applied.charisma,
        })
        self._advice_attrs = None
        self.query_one("#apply_remap", Button).disabled = True
        self.query_one("#remap_advice", Label).update("Remap applied.")
        self._recompute()

    # ---- export ----------------------------------------------------------- #
    @on(Button.Pressed, "#export")
    def action_export(self) -> None:
        if not self._plan:
            self._status("Nothing to export yet.")
            return
        text = format_importable(self._plan)
        self.copy_to_clipboard(text)
        self.notify(f"Copied {len(self._plan)} skills to the clipboard (EVE-importable).")

    # ---- recompute + render ---------------------------------------------- #
    def _recompute(self) -> None:
        table = self.query_one("#plan", DataTable)
        table.clear()
        totals = self.query_one("#totals", Label)
        if not self._goals:
            totals.update("Add a goal to build a plan.")
            self._plan = []
            return
        try:
            self._plan = planner.build_plan(self._goals, self._skills, self._attrs, self._sort)
        except PlanError as error:
            self._plan = []
            totals.update(str(error))
            return

        cumulative = 0.0
        for index, entry in enumerate(self._plan, start=1):
            cumulative += entry.minutes
            in_range = 0 <= entry.goal_index < len(self._goals)
            goal = self._goals[entry.goal_index] if in_range else None
            label = goal.label if goal else ""
            # a step is a prerequisite when its attributed goal didn't ask for the
            # skill directly at (or above) this level.
            requested = goal.skills.get(entry.skill.type_id, 0) if goal else 0
            for_col = label if requested >= entry.level else f"{label} (pre)"
            table.add_row(
                str(index),
                entry.skill.name,
                roman(entry.level),
                f"{entry.sp:,}",
                format_duration(entry.minutes),
                format_duration(cumulative),
                for_col,
            )
        total_sp = sum(entry.sp for entry in self._plan)
        totals.update(
            f"{len(self._plan)} skills · {total_sp:,} SP · {format_duration(cumulative)} total"
        )
        # force an immediate reflow so every row shows without another interaction
        table.refresh(layout=True)

    # ---- helpers ---------------------------------------------------------- #
    def _select_value(self, selector: str) -> object:
        return self.query_one(selector, Select).value

    def _status(self, message: str) -> None:
        self.query_one("#status", Label).update(message)


def main() -> None:
    SkillPlanApp().run()


if __name__ == "__main__":
    main()
