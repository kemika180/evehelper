"""The TUI: the picker lists set-up characters, and the trading screen renders a
report into the table and panels."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text
from textual.widgets import Button, DataTable, Input, OptionList, Static, TabbedContent, Tree

from evetrader.advisor.state import CharacterState, TradeSkills
from evetrader.data.assets import AssetLocation, AssetNode
from evetrader.data.skills import SkillReference
from evetrader.esi.auth import CharacterIdentity
from evetrader.esi.models import (
    Blueprint,
    IndustryJob,
    MarketHistoryDay,
    Skill,
    SkillQueueEntry,
)
from evetrader.market.fees import EffectiveFees
from evetrader.market.investment import InvestmentSignal
from evetrader.market.production import BuildAnalysis, MaterialLine
from evetrader.pipeline import BuildOpportunity, CharacterReport, OpportunityReport
from evetrader.session import CharacterRecord, CharacterStore
from evetrader.tui.app import (
    BlueprintInfoScreen,
    CharacterPickerScreen,
    DownloadSdeFn,
    EveTraderApp,
    IndustryJobScreen,
    MaterialsScreen,
    PriceHistoryScreen,
    RefreshFeed,
    SkillInfoScreen,
    TradingScreen,
    _completion,
    _current_training,
    _job_state,
    _job_subject_type,
    _skill_progress,
    _skill_queue_pips,
    _train_time,
)


def _queue_entry(skill_id: int, position: int, start: datetime | None, finish: datetime | None) -> SkillQueueEntry:
    return SkillQueueEntry(
        skill_id=skill_id,
        finished_level=5,
        queue_position=position,
        start_date=start,
        finish_date=finish,
    )


def test_current_training_skips_completed_entry_at_the_front() -> None:
    reference = datetime(2026, 1, 1, tzinfo=UTC)
    completed = _queue_entry(
        1, 0, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 6, 1, tzinfo=UTC)
    )
    active = _queue_entry(
        2, 1, datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    )
    result = _current_training([completed, active], reference)
    assert result is not None and result.skill_id == 2


def test_current_training_none_when_paused() -> None:
    reference = datetime(2026, 1, 1, tzinfo=UTC)
    paused = _queue_entry(1, 0, None, None)  # paused queue: ESI returns no dates
    assert _current_training([paused], reference) is None


def test_current_training_none_when_all_completed() -> None:
    reference = datetime(2026, 1, 1, tzinfo=UTC)
    done = _queue_entry(1, 0, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC))
    assert _current_training([done], reference) is None


def test_completion_hidden_for_completed_skills() -> None:
    reference = datetime(2026, 1, 1, tzinfo=UTC)
    done = _queue_entry(1, 0, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC))
    future = _queue_entry(2, 1, datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC))
    assert _completion(done, reference) == "—"
    assert _completion(future, reference) != "—"  # still shown for not-yet-complete


def _sp_entry(
    start: datetime | None,
    finish: datetime | None,
    *,
    level_start: int | None = 100_000,
    level_end: int | None = 200_000,
    training_start: int | None = 100_000,
) -> SkillQueueEntry:
    return SkillQueueEntry(
        skill_id=1,
        finished_level=5,
        queue_position=0,
        start_date=start,
        finish_date=finish,
        level_start_sp=level_start,
        level_end_sp=level_end,
        training_start_sp=training_start,
    )


def test_skill_progress_interpolates_the_training_skill_by_time() -> None:
    entry = _sp_entry(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC))
    progress = _skill_progress(entry, datetime(2026, 1, 2, tzinfo=UTC))  # halfway
    assert progress.status == "training"
    assert progress.level_sp == 100_000
    assert progress.trained_sp == 50_000
    assert progress.fraction == 0.5


def test_skill_progress_queued_reflects_pre_training() -> None:
    # Not started yet, but 20k SP into the level from earlier training.
    entry = _sp_entry(
        datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 3, tzinfo=UTC), training_start=120_000
    )
    progress = _skill_progress(entry, datetime(2026, 1, 1, tzinfo=UTC))
    assert progress.status == "queued"
    assert progress.trained_sp == 20_000
    assert progress.fraction == 0.2


def test_skill_progress_completed_is_full() -> None:
    entry = _sp_entry(datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC))
    progress = _skill_progress(entry, datetime(2026, 1, 1, tzinfo=UTC))
    assert progress.status == "completed"
    assert progress.trained_sp == 100_000
    assert progress.fraction == 1.0


def test_skill_progress_paused_without_sp_has_no_fraction() -> None:
    entry = _sp_entry(None, None, level_start=None, level_end=None, training_start=None)
    progress = _skill_progress(entry, datetime(2026, 1, 1, tzinfo=UTC))
    assert progress.status == "paused"
    assert progress.level_sp is None
    assert progress.fraction is None


def test_train_time_is_per_skill_not_cumulative() -> None:
    reference = datetime(2026, 1, 1, tzinfo=UTC)
    # A queued skill: its own duration (5 days), not the ~36-day wait before it.
    queued = _queue_entry(
        1, 1, datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 6, tzinfo=UTC)
    )
    assert _train_time(queued, reference) == "5d 0h"
    # The training skill: the remaining time, from now to its completion (2 days).
    active = _queue_entry(
        2, 0, datetime(2025, 12, 30, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
    )
    assert _train_time(active, reference) == "2d 0h"


def _character_state() -> CharacterState:
    return CharacterState(
        station_id=60003760,
        wallet_balance=5_000_000.0,
        fees=EffectiveFees(sales_tax=0.036, broker_fee=0.0146),
        trade_skills=TradeSkills(
            accounting=5, broker_relations=4, trade=5, retail=0, wholesale=0, tycoon=0
        ),
        free_order_slots=23,
    )


def _character_report() -> CharacterReport:
    return CharacterReport(
        captured_at=datetime(2020, 1, 1, tzinfo=UTC),
        character=_character_state(),
        skill_queue=[
            SkillQueueEntry(
                skill_id=16622,
                finished_level=5,
                queue_position=0,
                start_date=datetime(2019, 12, 1, tzinfo=UTC),
                finish_date=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            )
        ],
        skills=[
            Skill(skill_id=16622, active_skill_level=5, trained_skill_level=5),  # Accounting
            Skill(skill_id=3443, active_skill_level=3, trained_skill_level=3),  # Trade
        ],
        holdings={34: 500},
        names={
            16622: "Accounting",
            3443: "Trade",
            34: "Tritanium",
            35: "Pyerite",
            17363: "Giant Secure Container",
            938: "Rifter Blueprint",
            587: "Rifter",
            60003760: "Jita IV - Moon 4 - CNAP",
        },
        station_name="Jita IV - Moon 4 - Caldari Navy Assembly Plant",
        skill_reference={
            16622: SkillReference(
                name="Accounting",
                group="Trade",
                rank=3,
                primary="Charisma",
                secondary="Memory",
                description="Reduces sales tax.",
            ),
            3443: SkillReference(
                name="Trade",
                group="Trade",
                rank=1,
                primary="Charisma",
                secondary="Memory",
                description="Basic trading.",
            ),
        },
        assets=_asset_tree(),
        asset_names={11: "Ammo Box"},  # the Giant Secure Container has a player name
        blueprints={
            13: Blueprint(
                item_id=13, type_id=938, location_id=60003760, location_flag="Hangar",
                quantity=-2, material_efficiency=10, time_efficiency=20, runs=42,
            )
        },
        industry_jobs=[
            IndustryJob(  # manufacturing, still running -> "active"
                job_id=1, activity_id=1, blueprint_type_id=938, product_type_id=587,
                facility_id=60003760, runs=10, status="active", cost=1_000_000.0,
                start_date=datetime(2019, 12, 31, tzinfo=UTC),
                end_date=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            IndustryJob(  # ME research, finished -> "ready" (names the blueprint)
                job_id=2, activity_id=4, blueprint_type_id=938, facility_id=60003760,
                runs=1, status="active",
                start_date=datetime(2019, 12, 1, tzinfo=UTC),
                end_date=datetime(2019, 12, 20, tzinfo=UTC),
            ),
        ],
    )


def _asset_tree() -> list[AssetLocation]:
    # A hangar with a stack and a container holding another stack.
    return [
        AssetLocation(
            location_id=60003760,
            items=(
                AssetNode(
                    item_id=10, type_id=34, quantity=1_000, location_flag="Hangar",
                    is_singleton=False, children=(),
                ),
                AssetNode(
                    item_id=11, type_id=17363, quantity=1, location_flag="Hangar",
                    is_singleton=True,
                    children=(
                        AssetNode(
                            item_id=12, type_id=35, quantity=500, location_flag="Hangar",
                            is_singleton=False, children=(),
                        ),
                    ),
                ),
                AssetNode(
                    item_id=13, type_id=938, quantity=1, location_flag="Hangar",
                    is_singleton=True, children=(),  # a researched blueprint copy
                ),
            ),
        )
    ]


def _signal(type_id: int, action: str, current: float, fair: float, position: float) -> InvestmentSignal:
    return InvestmentSignal(
        type_id=type_id,
        action=action,
        current_price=current,
        fair_value=fair,
        low_band=fair * 0.8,
        high_band=fair * 1.2,
        channel_position=position,
        quantity=100,
        expected_profit=1_000_000.0,
        reasoning="test",
    )


def _history_days() -> list[MarketHistoryDay]:
    return [
        MarketHistoryDay.model_validate(
            {
                "date": f"2020-01-0{day}",
                "average": 1000.0,
                "highest": 1100.0,
                "lowest": 900.0,
                "order_count": 10,
                "volume": 1000,
            }
        )
        for day in range(1, 5)
    ]


def _opportunity_report() -> OpportunityReport:
    return OpportunityReport(
        buys=[_signal(34, "BUY", current=700.0, fair=1000.0, position=0.05)],
        sells=[_signal(35, "SELL", current=1300.0, fair=1000.0, position=0.95)],
        names={34: "Tritanium", 35: "Pyerite", 587: "Rifter"},
        history={34: _history_days(), 35: _history_days()},
        builds=[
            BuildOpportunity(  # the owned Rifter Blueprint (item 13) builds at a profit
                blueprint_item_id=13,
                blueprint_type_id=938,
                product_type_id=587,
                material_efficiency=10,
                analysis=BuildAnalysis(
                    runs=1,
                    material_cost=1_200_000.0,
                    product_value=1_850_000.0,
                    net_product_value=1_800_000.0,
                    missing_material_prices=(),
                ),
            )
        ],
        sde_available=True,
    )


def _build(
    item_id: int,
    bp_type: int,
    product: int,
    me: int,
    margin: float,
    materials: tuple[MaterialLine, ...] = (),
) -> BuildOpportunity:
    return BuildOpportunity(
        blueprint_item_id=item_id,
        blueprint_type_id=bp_type,
        product_type_id=product,
        material_efficiency=me,
        analysis=BuildAnalysis(
            runs=1,
            material_cost=1_000_000.0,
            product_value=1_000_000.0 + margin,
            net_product_value=1_000_000.0 + margin,
            missing_material_prices=(),
            materials=materials,
        ),
    )


def _manufacturing_report() -> OpportunityReport:
    return OpportunityReport(
        buys=[],
        sells=[],
        names={587: "Rifter", 588: "Breacher", 34: "Tritanium", 35: "Pyerite"},
        history={},
        builds=[
            _build(
                13, 900, 587, 10, 600_000.0,
                materials=(MaterialLine(34, 90, 5.0), MaterialLine(35, 45, 10.0)),
            ),
            _build(14, 900, 587, 10, 600_000.0),  # a second identical copy -> collapses
            _build(15, 901, 588, 2, 100_000.0),
        ],
        sde_available=True,
    )


def _build_app(
    store: CharacterStore,
    opportunity: OpportunityReport | None = None,
    download_sde_fn: DownloadSdeFn | None = None,
) -> EveTraderApp:
    report = opportunity if opportunity is not None else _opportunity_report()

    def make_feed(character_id: int) -> RefreshFeed:
        async def character() -> CharacterReport:
            return _character_report()

        async def opportunities(state: CharacterState) -> OpportunityReport:
            return report

        return RefreshFeed(character=character, opportunities=opportunities)

    async def login_fn() -> CharacterIdentity:
        return CharacterIdentity(999, "New Char")

    def remove_token_fn(character_id: int) -> None:
        pass

    return EveTraderApp(
        store,
        make_feed,
        login_fn,
        remove_token_fn,
        interval_seconds=30,
        download_sde_fn=download_sde_fn,
    )


def test_picker_lists_set_up_characters(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))
    store.add(CharacterRecord(2, "Bob"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.theme == "kemika-purple"  # custom theme registered and applied
            option_list = app.query_one("#characters", OptionList)
            assert option_list.option_count == 2
            await pilot.press("q")

    asyncio.run(_drive())


def test_selecting_a_character_opens_the_rendered_trading_screen(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)  # the same path OptionSelected takes
            for _ in range(4):  # let both refresh phases run
                await pilot.pause()

            trading = app.screen  # top of the stack
            assert trading.query_one("#buys", DataTable).row_count == 1
            assert trading.query_one("#sells", DataTable).row_count == 1
            wallet_text = str(trading.query_one("#stat-wallet", Static).render())
            assert "WALLET" in wallet_text and "5.00m" in wallet_text
            # The station shows its resolved name in the location row, not the id.
            location_text = str(trading.query_one("#location", Static).render())
            assert "Jita IV - Moon 4 - Caldari Navy Assembly Plant" in location_text
            # Completion is shown in local time (same conversion, TZ-independent here).
            expected_local = (
                datetime(2026, 8, 1, 12, 0, tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            )
            # Main tab shows only the current training skill, with completion time.
            training_text = str(trading.query_one("#training", Static).render())
            assert "Accounting" in training_text and expected_local in training_text
            # Full queue lives on the Skill Queue tab as a 3-column table.
            queue = trading.query_one("#skillqueue", DataTable)
            assert queue.row_count == 1
            row_text = " ".join(str(cell) for cell in queue.get_row_at(0))
            assert "Accounting" in row_text and expected_local in row_text
            await pilot.press("q")

    asyncio.run(_drive())


def test_escape_on_picker_resumes_the_last_character(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(3):
                await pilot.pause()
            assert isinstance(app.screen, TradingScreen)

            await pilot.press("escape")  # trading -> picker
            await pilot.pause()
            assert isinstance(app.screen, CharacterPickerScreen)

            await pilot.press("escape")  # picker -> back to the character
            for _ in range(3):
                await pilot.pause()
            assert isinstance(app.screen, TradingScreen)

    asyncio.run(_drive())


def test_selecting_a_buy_row_opens_the_price_chart(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            buys = trading.query_one("#buys", DataTable)
            buys.focus()
            await pilot.pause()
            await pilot.press("enter")  # select the highlighted row
            await pilot.pause()

            assert isinstance(app.screen, PriceHistoryScreen)
            await pilot.press("escape")

    asyncio.run(_drive())


def test_selecting_a_skill_row_opens_the_skill_info_popup(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            trading.query_one(TabbedContent).active = "queue"
            await pilot.pause()
            queue = trading.query_one("#skillqueue", DataTable)
            queue.focus()
            await pilot.pause()
            await pilot.press("enter")  # select the highlighted skill row
            await pilot.pause()

            assert isinstance(app.screen, SkillInfoScreen)
            popup_text = str(app.screen.query_one("#skillbody", Static).render())
            assert "Accounting" in popup_text
            assert "rank 3" in popup_text  # static reference facts
            assert "Charisma / Memory" in popup_text
            assert "Reduces sales tax." in popup_text  # bundled description
            await pilot.click("#skillbody")  # clicking the popup dismisses it
            await pilot.pause()
            assert isinstance(app.screen, TradingScreen)

    asyncio.run(_drive())


def _pip_styles(pips: Text) -> list[str]:
    """Per-character style string of a pip Text (each pip is appended individually)."""
    styles = ["" for _ in pips.plain]
    for span in pips.spans:
        for index in range(span.start, span.end):
            styles[index] = str(span.style)
    return styles


def test_skill_queue_pips_leave_unqueued_skills_plain() -> None:
    pips = _skill_queue_pips(999, 3, [], datetime(2020, 1, 6, tzinfo=UTC))
    assert pips.plain == "■■■□□"
    styles = _pip_styles(pips)
    assert styles[:3] == ["cyan", "cyan", "cyan"]  # trained
    assert styles[3:] == ["dim", "dim"]  # untrained, not queued


def test_skill_queue_pips_colour_queued_levels() -> None:
    reference = datetime(2020, 1, 6, tzinfo=UTC)
    queue = [
        SkillQueueEntry(  # training into level 4, halfway
            skill_id=100,
            finished_level=4,
            queue_position=0,
            start_date=datetime(2020, 1, 1, tzinfo=UTC),
            finish_date=datetime(2020, 1, 11, tzinfo=UTC),
            training_start_sp=1000,
            level_start_sp=1000,
            level_end_sp=2000,
        ),
        SkillQueueEntry(skill_id=100, finished_level=5, queue_position=1),  # queued behind it
    ]
    pips = _skill_queue_pips(100, 3, queue, reference)
    assert pips.plain == "■■■◪□"  # 1-3 trained, 4 part-trained (◪), 5 queued
    styles = _pip_styles(pips)
    assert styles[:3] == ["cyan", "cyan", "cyan"]  # trained levels keep their colour
    assert styles[3] == "magenta"  # the part-trained box
    assert styles[4] == "magenta"  # the queued-but-not-started box


def test_skill_queue_pips_count_finished_levels_as_trained() -> None:
    reference = datetime(2020, 1, 6, tzinfo=UTC)
    completed = SkillQueueEntry(
        skill_id=100,
        finished_level=4,
        queue_position=0,
        start_date=datetime(2019, 12, 1, tzinfo=UTC),
        finish_date=datetime(2019, 12, 20, tzinfo=UTC),  # already finished
    )
    # trained_skill_level still lags at 3, but level 4 finished in the queue -> it
    # shows as a full ■ (trained), not a queued/part-trained box.
    pips = _skill_queue_pips(100, 3, [completed], reference)
    assert pips.plain == "■■■■□"
    assert _pip_styles(pips)[3] == "cyan"


def _bpc_node() -> AssetNode:
    return AssetNode(
        item_id=13, type_id=938, quantity=1, location_flag="Hangar",
        is_singleton=True, children=(),
    )


def test_blueprint_info_screen_shows_copy_research() -> None:
    blueprint = Blueprint(
        item_id=13, type_id=938, location_id=1, location_flag="Hangar",
        quantity=-2, material_efficiency=10, time_efficiency=20, runs=42,
    )
    body = BlueprintInfoScreen("Rifter Blueprint", _bpc_node(), blueprint)._body().plain
    assert "Rifter Blueprint" in body
    assert "Blueprint Copy (BPC)" in body
    assert "-10% materials" in body and "-20% time" in body
    assert "42 runs remaining" in body


def test_blueprint_info_screen_marks_an_original_unlimited() -> None:
    blueprint = Blueprint(
        item_id=13, type_id=938, location_id=1, location_flag="Hangar",
        quantity=-1, material_efficiency=0, time_efficiency=0, runs=-1,
    )
    body = BlueprintInfoScreen("Rifter Blueprint", _bpc_node(), blueprint)._body().plain
    assert "Blueprint Original (BPO)" in body
    assert "unlimited runs" in body


def _job(**overrides: object) -> IndustryJob:
    payload: dict[str, object] = {
        "job_id": 1, "activity_id": 1, "blueprint_type_id": 938, "product_type_id": 587,
        "facility_id": 60003760, "runs": 10, "status": "active",
        "start_date": datetime(2020, 1, 1, tzinfo=UTC),
        "end_date": datetime(2020, 1, 2, tzinfo=UTC),
    }
    payload.update(overrides)
    return IndustryJob.model_validate(payload)


def test_job_state_ready_when_finished() -> None:
    reference = datetime(2020, 1, 3, tzinfo=UTC)  # past the end date
    assert _job_state(_job(), reference) == "ready"
    # Still running at an earlier reference.
    assert _job_state(_job(), datetime(2020, 1, 1, 12, tzinfo=UTC)) == "active"
    # A paused job keeps its status regardless of the clock.
    assert _job_state(_job(status="paused"), reference) == "paused"


def test_job_subject_type_prefers_product_then_blueprint() -> None:
    assert _job_subject_type(_job(product_type_id=587)) == 587  # manufacturing -> product
    assert _job_subject_type(_job(product_type_id=None)) == 938  # research -> blueprint


def test_skills_tab_groups_trained_skills(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            tree = app.screen.query_one("#skilltree", Tree)
            groups = {str(node.label): node for node in tree.root.children}
            assert "Trade" in groups  # both fixture skills live under Trade
            leaves = [str(leaf.label) for leaf in groups["Trade"].children]
            assert any("Accounting" in text and "■■■■■" in text for text in leaves)
            assert any("Trade" in text and "■■■□□" in text for text in leaves)
            await pilot.press("q")

    asyncio.run(_drive())


def test_skill_tree_survives_a_refresh_when_skills_unchanged(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            tree = trading.query_one("#skilltree", Tree)
            trade = next(node for node in tree.root.children if str(node.label) == "Trade")
            assert trade.is_expanded  # groups start open
            trade.collapse()
            await pilot.pause()

            await trading._refresh()  # a periodic refresh with the same skills
            await pilot.pause()

            # Same node, still collapsed — the tree was not rebuilt.
            same = next(node for node in tree.root.children if str(node.label) == "Trade")
            assert same is trade
            assert not same.is_expanded
            await pilot.press("q")

    asyncio.run(_drive())


def test_assets_tab_shows_places_and_nested_containers(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            tree = app.screen.query_one("#assettree", Tree)
            place = tree.root.children[0]
            assert "Jita" in str(place.label)  # the home place is named, not an id
            labels = [str(node.label) for node in place.children]
            assert any("Tritanium" in text and "1,000" in text for text in labels)

            container = next(n for n in place.children if "Giant Secure Container" in str(n.label))
            # The container shows its player-assigned name alongside its type.
            assert "Ammo Box" in str(container.label)
            inside = [str(child.label) for child in container.children]
            assert any("Pyerite" in text for text in inside)  # look inside the container
            await pilot.press("q")

    asyncio.run(_drive())


def test_asset_search_filters_to_matching_items(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            tree = trading.query_one("#assettree", Tree)
            trading.query_one("#assetsearch", Input).value = "pyerite"
            await pilot.pause()

            # Only the path to Pyerite (inside the container) survives; the hangar
            # Tritanium stack is filtered out.
            top = [str(node.label) for node in tree.root.children[0].children]
            assert not any("Tritanium" in text for text in top)
            container = next(
                n for n in tree.root.children[0].children if "Giant Secure Container" in str(n.label)
            )
            assert any("Pyerite" in str(child.label) for child in container.children)

            # Search also matches a container's player-assigned name.
            trading.query_one("#assetsearch", Input).value = "ammo box"
            await pilot.pause()
            top = [str(node.label) for node in tree.root.children[0].children]
            assert any("Ammo Box" in text for text in top)

            trading.query_one("#assetsearch", Input).value = ""  # cleared -> full tree
            await pilot.pause()
            top = [str(node.label) for node in tree.root.children[0].children]
            assert any("Tritanium" in text for text in top)
            await pilot.press("q")

    asyncio.run(_drive())


def _app_with_assets(store: CharacterStore, locations: list[AssetLocation], names: dict[int, str]):
    base = _character_report()
    report = CharacterReport(
        base.captured_at,
        base.character,
        base.skill_queue,
        base.skills,
        base.holdings,
        {**base.names, **names},
        base.station_name,
        base.skill_reference,
        locations,
        {},
        base.blueprints,
        base.industry_jobs,
    )

    async def character() -> CharacterReport:
        return report

    async def opportunities(state: CharacterState) -> OpportunityReport:
        return _opportunity_report()

    async def login_fn() -> CharacterIdentity:
        return CharacterIdentity(999, "New Char")

    return EveTraderApp(
        store,
        lambda cid: RefreshFeed(character=character, opportunities=opportunities),
        login_fn,
        lambda cid: None,
        interval_seconds=30,
    )


def test_ship_contents_group_into_sections_with_slot_names(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    ship = AssetNode(
        2,
        24696,  # Harbinger
        1,
        "Hangar",
        True,
        (
            AssetNode(3, 34, 1, "HiSlot0", True, ()),  # a high-slot module
            AssetNode(4, 35, 1, "LoSlot2", True, ()),  # a low-slot module
            AssetNode(5, 36, 500, "Cargo", False, ()),  # ammo in cargo
            AssetNode(6, 37, 3, "DroneBay", False, ()),  # drones
        ),
    )
    names = {24696: "Harbinger", 34: "Smartbomb", 35: "Plate", 36: "Ammo", 37: "Hobgoblin"}
    app = _app_with_assets(store, [AssetLocation(60003760, (ship,))], names)

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.select_character(1)
            for _ in range(4):
                await pilot.pause()
            tree = app.screen.query_one("#assettree", Tree)
            ship_node = tree.root.children[0].children[0]
            assert "Harbinger" in str(ship_node.label)

            sections = [str(node.label) for node in ship_node.children]
            # Fit first, then Cargo, then Drone Bay; each with a count.
            assert sections[0].startswith("Fit")
            assert any(s.startswith("Cargo") for s in sections)
            assert any(s.startswith("Drone Bay") for s in sections)

            fit = ship_node.children[0]
            fit_items = [str(node.label) for node in fit.children]
            # Slot name first, then the item; raw flag / slot number not shown.
            assert any(
                t.startswith("High Slot") and "Smartbomb" in t and "HiSlot0" not in t
                for t in fit_items
            )
            assert any(t.startswith("Low Slot") and "Plate" in t for t in fit_items)
            await pilot.press("q")

    asyncio.run(_drive())


def test_deeply_nested_assets_render_without_crashing(tmp_path: Path) -> None:
    # A fitted ship inside a container reaches depth 4+, where the depth-colour palette
    # once indexed a theme field the theme left unset (None) and crashed on render.
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    def _leaf(item_id: int, type_id: int, flag: str) -> AssetNode:
        return AssetNode(item_id, type_id, 1, flag, True, ())

    ship = AssetNode(
        3, 24696, 1, "Hangar", True, (_leaf(4, 34, "HiSlot6"), _leaf(5, 35, "MedSlot0"))
    )
    container = AssetNode(2, 17363, 1, "Hangar", True, (ship,))
    base = _character_report()
    report = CharacterReport(
        base.captured_at,
        base.character,
        base.skill_queue,
        base.skills,
        base.holdings,
        {**base.names, 24696: "Harbinger", 17363: "Container"},
        base.station_name,
        base.skill_reference,
        [AssetLocation(60003760, (container,))],
        {},
        base.blueprints,
        base.industry_jobs,
    )

    async def _drive() -> None:
        async def character() -> CharacterReport:
            return report

        async def opportunities(state: CharacterState) -> OpportunityReport:
            return _opportunity_report()

        async def login_fn() -> CharacterIdentity:
            return CharacterIdentity(999, "New Char")

        app = EveTraderApp(
            store,
            lambda cid: RefreshFeed(character=character, opportunities=opportunities),
            login_fn,
            lambda cid: None,
            interval_seconds=30,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()
            tree = app.screen.query_one("#assettree", Tree)
            tree.root.expand_all()  # make every depth visible -> render_label runs at each
            for _ in range(3):
                await pilot.pause()
            assert len(tree._tree_lines) >= 5  # built through depth 4, no crash

    asyncio.run(_drive())


def test_selecting_a_trained_skill_opens_its_detail(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            trading.query_one(TabbedContent).active = "skills"
            await pilot.pause()
            tree = trading.query_one("#skilltree", Tree)
            tree.focus()
            # Trade (3443) isn't in the queue -> the trained-skill detail path.
            leaf = next(
                lf for grp in tree.root.children for lf in grp.children if lf.data == 3443
            )
            leaf.parent.expand()  # a group is collapsed until opened
            await pilot.pause()
            tree.move_cursor(leaf)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, SkillInfoScreen)
            body = str(app.screen.query_one("#skillbody", Static).render())
            assert "Trade" in body and "Level 3 trained" in body
            assert "Basic trading." in body  # bundled description
            await pilot.press("escape")

    asyncio.run(_drive())


def test_selecting_a_blueprint_asset_opens_its_detail(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            trading.query_one(TabbedContent).active = "assets"
            await pilot.pause()
            tree = trading.query_one("#assettree", Tree)
            tree.focus()
            leaves = [lf for place in tree.root.children for lf in place.children]
            # Only the blueprint (item_id 13) carries data; plain items are inert.
            blueprint_leaf = next(
                lf for lf in leaves if isinstance(lf.data, AssetNode) and lf.data.item_id == 13
            )
            assert "BPC" in str(blueprint_leaf.label)  # tagged in the tree
            tritanium_leaf = next(lf for lf in leaves if "Tritanium" in str(lf.label))
            assert tritanium_leaf.data is None  # a non-blueprint item opens nothing

            tree.move_cursor(blueprint_leaf)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, BlueprintInfoScreen)
            body = str(app.screen.query_one("#bpbody", Static).render())
            assert "Rifter Blueprint" in body
            assert "Blueprint Copy (BPC)" in body
            assert "42 runs remaining" in body
            # The market phase has produced a build-vs-buy analysis for this blueprint.
            assert "Build vs buy" in body
            assert "BUILD" in body
            await pilot.press("escape")

    asyncio.run(_drive())


def test_manufacturing_tab_ranks_owned_blueprints(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "manufacturing-tab"
            await pilot.pause()
            table = trading.query_one("#manufacturing", DataTable)
            assert table.row_count == 1
            row = table.get_row_at(0)
            assert "Rifter" in str(row[0])  # product name
            assert "BUILD" in str(row[5])  # profitable -> BUILD
            await pilot.press("q")

    asyncio.run(_drive())


def test_manufacturing_tab_explains_a_missing_sde(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))
    empty = OpportunityReport(
        buys=[], sells=[], names={}, history={}, builds=[], sde_available=False
    )

    async def _drive() -> None:
        app = _build_app(store, empty)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "manufacturing-tab"
            await pilot.pause()
            assert trading.query_one("#manufacturing", DataTable).row_count == 0
            hint = str(trading.query_one("#manufacturing-hint", Static).render())
            assert "evetrader sde" in hint  # tells the user how to enable it
            # No download callable wired -> no in-UI download button.
            assert not trading.query("#download-sde")
            await pilot.press("q")

    asyncio.run(_drive())


def test_manufacturing_dedupes_counts_searches_and_sorts(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store, _manufacturing_report())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "manufacturing-tab"
            await pilot.pause()
            table = trading.query_one("#manufacturing", DataTable)

            # Two identical Rifter copies collapse to one row that shows the count.
            assert table.row_count == 2
            assert "Rifter" in str(table.get_row_at(0)[0])  # highest margin, default sort
            assert "×2" in str(table.get_row_at(0)[0])  # two copies owned
            assert "Breacher" in str(table.get_row_at(1)[0])
            assert "×" not in str(table.get_row_at(1)[0])  # a single copy shows no count

            # Search filters by product name.
            trading.query_one("#manufacturing-search", Input).value = "brea"
            await pilot.pause()
            assert table.row_count == 1
            assert "Breacher" in str(table.get_row_at(0)[0])
            trading.query_one("#manufacturing-search", Input).value = ""
            await pilot.pause()
            assert table.row_count == 2

            # Sorting: click the ME header (column 1) — numeric, so descending first.
            keys = list(table.columns.keys())
            trading.on_data_table_header_selected(
                DataTable.HeaderSelected(table, keys[1], 1, table.columns[keys[1]].label)
            )
            await pilot.pause()
            assert trading._mfg_sort == (1, True)
            assert "Rifter" in str(table.get_row_at(0)[0])  # ME 10 before ME 2
            # Click again to reverse.
            trading.on_data_table_header_selected(
                DataTable.HeaderSelected(table, keys[1], 1, table.columns[keys[1]].label)
            )
            await pilot.pause()
            assert trading._mfg_sort == (1, False)
            assert "Breacher" in str(table.get_row_at(0)[0])  # ME 2 first
            await pilot.press("q")

    asyncio.run(_drive())


def test_manufacturing_row_opens_material_breakdown(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store, _manufacturing_report())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "manufacturing-tab"
            await pilot.pause()
            table = trading.query_one("#manufacturing", DataTable)
            table.focus()  # default sort puts the Rifter (with materials) at row 0
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, MaterialsScreen)
            body = str(app.screen.query_one("#matbody", Static).render())
            assert "Rifter" in body
            assert "Tritanium" in body and "Pyerite" in body  # the required materials
            await pilot.press("escape")

    asyncio.run(_drive())


def test_manufacturing_refresh_preserves_cursor_when_unchanged(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store, _manufacturing_report())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "manufacturing-tab"
            await pilot.pause()
            table = trading.query_one("#manufacturing", DataTable)
            table.move_cursor(row=1)
            cursor = table.cursor_coordinate

            assert trading._report is not None
            trading._render_builds(trading._report)  # a refresh with identical data
            # The guard skips the rebuild, so the cursor isn't reset to the top.
            assert table.cursor_coordinate == cursor
            await pilot.press("q")

    asyncio.run(_drive())


def test_manufacturing_download_button_hidden_once_sde_present(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def fake_download() -> bool:
        return True

    async def _drive() -> None:
        # SDE present (builds available) -> the download button hides itself.
        app = _build_app(store, _manufacturing_report(), download_sde_fn=fake_download)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "manufacturing-tab"
            await pilot.pause()
            assert trading.query_one("#download-sde", Button).display is False
            await pilot.press("q")

    asyncio.run(_drive())


def test_manufacturing_download_button_invokes_the_downloader(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))
    empty = OpportunityReport(
        buys=[], sells=[], names={}, history={}, builds=[], sde_available=False
    )
    calls: list[bool] = []

    async def fake_download() -> bool:
        calls.append(True)
        return True

    async def _drive() -> None:
        app = _build_app(store, empty, download_sde_fn=fake_download)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "manufacturing-tab"
            await pilot.pause()
            assert trading.query_one("#download-sde", Button)  # wired -> button present
            await pilot.click("#download-sde")
            for _ in range(4):
                await pilot.pause()
            assert calls == [True]  # the button ran the download callable

    asyncio.run(_drive())


def test_industry_tab_lists_jobs_and_opens_detail(tmp_path: Path) -> None:
    store = CharacterStore(tmp_path / "characters.json")
    store.add(CharacterRecord(1, "Alice"))

    async def _drive() -> None:
        app = _build_app(store)
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, CharacterPickerScreen)
            picker.select_character(1)
            for _ in range(4):
                await pilot.pause()

            trading = app.screen
            assert isinstance(trading, TradingScreen)
            trading.query_one(TabbedContent).active = "industry-tab"
            await pilot.pause()
            table = trading.query_one("#industry", DataTable)
            assert table.row_count == 2
            # The finished ME-research job (job 2) sorts to the top as ready-to-deliver;
            # with no product it's named by its blueprint.
            first = table.get_row_at(0)
            assert "ME Research" in str(first[0]) and "ready" in str(first[3])
            assert "Rifter Blueprint" in str(first[1])

            table.focus()
            await pilot.pause()
            await pilot.press("down")  # move to the manufacturing job (product-named)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, IndustryJobScreen)
            body = str(app.screen.query_one("#jobbody", Static).render())
            assert "Rifter" in body and "Manufacturing" in body
            assert "In progress" in body
            await pilot.press("escape")

    asyncio.run(_drive())
