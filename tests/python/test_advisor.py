"""Advisor core: order-slot derivation, station-trade adaptation, engine ranking."""

from datetime import UTC, datetime

from evetrader.advisor.engine import rank
from evetrader.advisor.source import Opportunity, OpportunitySource, StationTradingSource
from evetrader.advisor.state import CharacterState, TradeSkills, total_order_slots
from evetrader.config import Config, RiskPreferences
from evetrader.data.market import build_market_snapshot
from evetrader.esi.models import MarketHistoryDay, MarketOrder
from evetrader.market.fees import EffectiveFees
from evetrader.market.snapshot import MarketSnapshot

_STATION = 60003760


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="c@e.com",
        home_region_id=10000002,
        home_station_id=_STATION,
        total_capital_isk=1_000_000.0,
        risk=RiskPreferences(
            min_margin=0.05, min_daily_isk_volume=1000.0, max_capital_per_order_isk=1_000_000.0
        ),
    )


def _character(free_slots: int = 5) -> CharacterState:
    return CharacterState(
        station_id=_STATION,
        wallet_balance=1_000_000.0,
        fees=EffectiveFees(sales_tax=0.05, broker_fee=0.02),
        trade_skills=TradeSkills(
            accounting=5, broker_relations=5, trade=1, retail=0, wholesale=0, tycoon=0
        ),
        free_order_slots=free_slots,
    )


def _snapshot() -> MarketSnapshot:
    orders = [
        MarketOrder.model_validate(
            {
                "order_id": i,
                "type_id": 34,
                "location_id": _STATION,
                "system_id": 30000142,
                "is_buy_order": is_buy,
                "price": price,
                "volume_remain": 100,
                "volume_total": 100,
                "min_volume": 1,
                "range": "station",
                "duration": 90,
                "issued": "2020-01-01T00:00:00Z",
            }
        )
        for i, (is_buy, price) in enumerate([(True, 100.0), (False, 150.0)])
    ]
    history = [
        MarketHistoryDay.model_validate(
            {
                "date": "2020-01-01",
                "average": 120.0,
                "highest": 120.0,
                "lowest": 120.0,
                "order_count": 10,
                "volume": 1000,
            }
        )
    ]
    return build_market_snapshot(
        region_id=10000002,
        captured_at=datetime(2020, 1, 1, tzinfo=UTC),
        orders=orders,
        history_by_type={34: history},
    )


def test_total_order_slots_sums_skill_bonuses() -> None:
    skills = TradeSkills(
        accounting=0, broker_relations=0, trade=5, retail=3, wholesale=0, tycoon=0
    )
    # 5 base + 4*5 + 8*3 = 5 + 20 + 24
    assert total_order_slots(skills) == 49


def test_station_source_adapts_trades_to_opportunities() -> None:
    opportunities = StationTradingSource().opportunities(_snapshot(), _character(), _config())
    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.kind == "station_trade"
    assert opp.type_id == 34
    assert opp.quantity == 100
    assert opp.buy_price == 100.01
    assert "margin" in opp.reasoning


def test_free_slots_limit_flows_through_to_selection() -> None:
    # Zero free slots -> the engine's max_orders is 0 -> nothing selected.
    opportunities = StationTradingSource().opportunities(_snapshot(), _character(0), _config())
    assert opportunities == []


class _FakeSource:
    def __init__(self, opps: list[Opportunity]) -> None:
        self._opps = opps

    def opportunities(
        self, snapshot: MarketSnapshot, character: CharacterState, config: Config
    ) -> list[Opportunity]:
        return self._opps


def _opp(type_id: int, isk_per_hour: float) -> Opportunity:
    return Opportunity(
        kind="station_trade",
        type_id=type_id,
        station_id=_STATION,
        buy_price=1.0,
        sell_price=2.0,
        margin=0.5,
        quantity=1,
        capital_required=1.0,
        profit_per_unit=1.0,
        expected_isk_per_hour=isk_per_hour,
        reasoning="",
    )


def test_engine_merges_sources_and_ranks_by_isk_per_hour() -> None:
    sources: list[OpportunitySource] = [
        _FakeSource([_opp(1, 100.0), _opp(2, 500.0)]),
        _FakeSource([_opp(3, 300.0)]),
    ]
    ranked = rank(sources, _snapshot(), _character(), _config())
    assert [o.type_id for o in ranked] == [2, 3, 1]
