"""WealthStore records (throttled), persists across instances, and exports TSV."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from evehelper.data.wealth import WealthSample, WealthStore, format_tsv


def _sample(when: datetime, wallet: float = 1_000.0, assets: float = 4_000.0) -> WealthSample:
    return WealthSample(captured_at=when, wallet_balance=wallet, assets_value=assets)


def test_total_is_wallet_plus_assets() -> None:
    assert _sample(datetime(2026, 8, 29, tzinfo=UTC), 1_500.0, 2_500.0).total == 4_000.0


def test_record_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "wealth.json"
    store = WealthStore(path)
    assert store.record(7, _sample(datetime(2026, 8, 29, 12, 0, tzinfo=UTC)))

    reloaded = WealthStore(path)
    history = reloaded.history(7)
    assert len(history) == 1
    assert history[0].total == 5_000.0
    assert reloaded.history(999) == []  # unknown character


def test_record_is_throttled(tmp_path: Path) -> None:
    store = WealthStore(path=tmp_path / "wealth.json", min_interval=timedelta(hours=1))
    base = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    assert store.record(1, _sample(base))
    assert not store.record(1, _sample(base + timedelta(minutes=30)))  # too soon
    assert store.record(1, _sample(base + timedelta(hours=2)))  # enough elapsed
    assert [s.captured_at for s in store.history(1)] == [base, base + timedelta(hours=2)]


def test_format_tsv_sorts_and_formats() -> None:
    later = _sample(datetime(2026, 8, 29, 15, 0, tzinfo=UTC), 100.0, 200.0)
    earlier = _sample(datetime(2026, 8, 29, 9, 0, tzinfo=UTC), 10.0, 20.0)

    lines = format_tsv([later, earlier]).splitlines()
    assert lines[0] == "timestamp\twallet_isk\tassets_isk\ttotal_isk"
    assert lines[1] == "2026-08-29T09:00:00+00:00\t10.00\t20.00\t30.00"  # earliest first
    assert lines[2] == "2026-08-29T15:00:00+00:00\t100.00\t200.00\t300.00"


def test_export_tsv_writes_file(tmp_path: Path) -> None:
    store = WealthStore(tmp_path / "wealth.json")
    store.record(3, _sample(datetime(2026, 8, 29, 12, 0, tzinfo=UTC)))
    dest = tmp_path / "out.tsv"

    store.export_tsv(3, dest)

    assert dest.read_text(encoding="utf-8").startswith("timestamp\twallet_isk")
    assert "5000.00" in dest.read_text(encoding="utf-8")
