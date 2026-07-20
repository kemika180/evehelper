"""The TUI mounts, renders a report into the table and panels, and quits."""

import asyncio
from datetime import UTC, datetime

from textual.widgets import DataTable, Static

from evetrader.advisor.source import Opportunity
from evetrader.advisor.state import CharacterState, TradeSkills
from evetrader.esi.models import SkillQueueEntry
from evetrader.market.fees import EffectiveFees
from evetrader.pipeline import AdvisorReport
from evetrader.tui.app import EveTraderApp


def _report() -> AdvisorReport:
    character = CharacterState(
        station_id=60003760,
        wallet_balance=5_000_000.0,
        fees=EffectiveFees(sales_tax=0.036, broker_fee=0.0146),
        trade_skills=TradeSkills(
            accounting=5, broker_relations=4, trade=5, retail=0, wholesale=0, tycoon=0
        ),
        free_order_slots=23,
    )
    opportunity = Opportunity(
        kind="station_trade",
        type_id=34,
        station_id=60003760,
        buy_price=100.01,
        sell_price=149.99,
        quantity=100,
        capital_required=10001.0,
        profit_per_unit=37.48,
        expected_isk_per_hour=156.0,
        reasoning="Buy 100 @ 100.01, sell @ 149.99; margin 37.5%",
    )
    queue = [SkillQueueEntry(skill_id=16622, finished_level=5, queue_position=0)]
    return AdvisorReport(
        captured_at=datetime(2020, 1, 1, tzinfo=UTC),
        character=character,
        opportunities=[opportunity],
        skill_queue=queue,
        names={34: "Tritanium", 16622: "Accounting"},
    )


def test_app_renders_report() -> None:
    async def _drive() -> None:
        app = EveTraderApp(refresh_fn=_report_fn, interval_seconds=30)
        async with app.run_test() as pilot:
            table = app.query_one("#opportunities", DataTable)
            assert table.row_count == 1
            character_text = str(app.query_one("#character", Static).render())
            assert "Wallet" in character_text and "5,000,000" in character_text
            skillqueue_text = str(app.query_one("#skillqueue", Static).render())
            assert "Accounting" in skillqueue_text
            await pilot.press("q")

    async def _report_fn() -> AdvisorReport:
        return _report()

    asyncio.run(_drive())
