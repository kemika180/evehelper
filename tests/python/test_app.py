"""The TUI: the picker lists set-up characters, and the trading screen renders a
report into the table and panels."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.widgets import DataTable, OptionList, Static

from evetrader.advisor.state import CharacterState, TradeSkills
from evetrader.esi.auth import CharacterIdentity
from evetrader.esi.models import MarketHistoryDay, SkillQueueEntry
from evetrader.market.fees import EffectiveFees
from evetrader.market.investment import InvestmentSignal
from evetrader.pipeline import CharacterReport, OpportunityReport
from evetrader.session import CharacterRecord, CharacterStore
from evetrader.tui.app import (
    CharacterPickerScreen,
    EveTraderApp,
    PriceHistoryScreen,
    RefreshFeed,
    TradingScreen,
    _completion,
    _current_training,
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
        holdings={34: 500},
        names={16622: "Accounting"},
        station_name="Jita IV - Moon 4 - Caldari Navy Assembly Plant",
    )


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
        names={34: "Tritanium", 35: "Pyerite"},
        history={34: _history_days(), 35: _history_days()},
    )


def _build_app(store: CharacterStore) -> EveTraderApp:
    def make_feed(character_id: int) -> RefreshFeed:
        async def character() -> CharacterReport:
            return _character_report()

        async def opportunities(state: CharacterState) -> OpportunityReport:
            return _opportunity_report()

        return RefreshFeed(character=character, opportunities=opportunities)

    async def login_fn() -> CharacterIdentity:
        return CharacterIdentity(999, "New Char")

    def remove_token_fn(character_id: int) -> None:
        pass

    return EveTraderApp(store, make_feed, login_fn, remove_token_fn, interval_seconds=30)


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
