"""The pure reprocessing-yield model: base station rate raised by skills, capped at 100%."""

from pytest import approx

from evehelper.market.refining import mineral_commonness, reprocessing_yield, security_target_rank


def test_yield_is_the_base_rate_with_no_skills() -> None:
    assert reprocessing_yield(0.5, 0, 0, 0) == 0.5


def test_skills_multiply_the_base_rate() -> None:
    # 0.5 * (1 + .03*5) * (1 + .02*5) * (1 + .02*4) = 0.5 * 1.15 * 1.10 * 1.08
    assert reprocessing_yield(0.5, 5, 5, 4) == approx(0.5 * 1.15 * 1.10 * 1.08)


def test_yield_is_capped_at_one() -> None:
    # A high base rate plus maxed skills can't recover more than the ore holds.
    assert reprocessing_yield(0.9, 5, 5, 5) == 1.0


def test_mineral_commonness_orders_tritanium_common_and_morphite_rare() -> None:
    # Rarest-first mining sorts by -commonness, so Morphite leads and Tritanium trails.
    minerals = [34, 37, 40, 11399]  # Tritanium, Isogen, Megacyte, Morphite
    order = sorted(minerals, key=lambda m: (-mineral_commonness(m), m))
    assert order == [11399, 40, 37, 34]
    # A non-core mineral (moon material) sorts ahead of the core minerals.
    assert mineral_commonness(16633) > mineral_commonness(11399)


def test_security_target_rank_bands() -> None:
    assert security_target_rank(0.9) == 0  # highsec -> commons
    assert security_target_rank(0.3) == 2  # lowsec
    assert security_target_rank(-0.1) == 3  # null
