"""compute_fees applies the reduction formula, the broker-fee floor, and clamps."""

from evetrader.config import FeeRates
from evetrader.market.fees import compute_fees


def test_max_skills_reduce_sales_tax_and_broker_fee() -> None:
    rates = FeeRates()  # documented defaults
    fees = compute_fees(
        accounting_level=5,
        broker_relations_level=5,
        faction_standing=0.0,
        corp_standing=0.0,
        rates=rates,
    )
    # sales_tax = 0.08 * (1 - 0.11*5) = 0.08 * 0.45 = 0.036
    assert fees.sales_tax == 0.08 * (1.0 - 0.11 * 5)
    # broker_fee = 0.03 - 0.003*5 = 0.015
    assert fees.broker_fee == 0.03 - 0.003 * 5


def test_no_skills_gives_base_rates() -> None:
    rates = FeeRates()
    fees = compute_fees(
        accounting_level=0,
        broker_relations_level=0,
        faction_standing=0.0,
        corp_standing=0.0,
        rates=rates,
    )
    assert fees.sales_tax == 0.08
    assert fees.broker_fee == 0.03


def test_standings_reduce_broker_fee() -> None:
    rates = FeeRates()
    fees = compute_fees(
        accounting_level=0,
        broker_relations_level=0,
        faction_standing=10.0,
        corp_standing=10.0,
        rates=rates,
    )
    expected = 0.03 - 0.0003 * 10.0 - 0.0002 * 10.0
    assert fees.broker_fee == expected


def test_broker_fee_never_below_floor() -> None:
    rates = FeeRates(min_broker_fee=0.02)
    fees = compute_fees(
        accounting_level=0,
        broker_relations_level=5,  # would push below the floor
        faction_standing=10.0,
        corp_standing=10.0,
        rates=rates,
    )
    assert fees.broker_fee == 0.02


def test_custom_rates_are_used() -> None:
    rates = FeeRates(base_sales_tax=0.05, accounting_reduction_per_level=0.10)
    fees = compute_fees(
        accounting_level=2,
        broker_relations_level=0,
        faction_standing=0.0,
        corp_standing=0.0,
        rates=rates,
    )
    assert fees.sales_tax == 0.05 * (1.0 - 0.10 * 2)
