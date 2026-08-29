"""Per-character wealth history, persisted to a small JSON file. Impure (file I/O).

Each time a character's holdings are valued, a sample — liquid wallet ISK plus the
reference-priced value of every asset — is appended, so the Wealth view can plot net
worth over time and export it as TSV. Recording is throttled so a periodic refresh
(every few minutes) doesn't pile up near-identical points.

Kept in ``data/`` (the I/O shell), not the pure core: it reads and writes the disk.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# Skip a new sample when the character's latest one is younger than this — one point an
# hour is plenty for a net-worth trend and keeps the history file from ballooning.
_MIN_SAMPLE_INTERVAL = timedelta(hours=1)

_TSV_HEADER = ("timestamp", "wallet_isk", "assets_isk", "total_isk")


@dataclass(frozen=True)
class WealthSample:
    """A character's estimated wealth at one moment: liquid ISK and asset value."""

    captured_at: datetime
    wallet_balance: float
    # Total value of all assets at global-average reference (Jita) prices.
    assets_value: float

    @property
    def total(self) -> float:
        """Estimated wealth — wallet plus assets — the figure the trend line tracks."""
        return self.wallet_balance + self.assets_value


def _load(path: Path) -> dict[int, list[WealthSample]]:
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    history: dict[int, list[WealthSample]] = {}
    for key, rows in stored.items():
        try:
            character_id = int(key)
        except ValueError:
            continue
        if not isinstance(rows, list):
            continue
        samples: list[WealthSample] = []
        for row in rows:
            try:
                samples.append(
                    WealthSample(
                        captured_at=datetime.fromisoformat(str(row["captured_at"])),
                        wallet_balance=float(row["wallet_balance"]),
                        assets_value=float(row["assets_value"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        history[character_id] = samples
    return history


def format_tsv(samples: Sequence[WealthSample]) -> str:
    """Render samples as a TSV document: a header row then one row per sample, oldest
    first, ISK figures fixed to two decimals. Pure — no I/O — so it's easy to test."""
    lines = ["\t".join(_TSV_HEADER)]
    for sample in sorted(samples, key=lambda s: s.captured_at):
        lines.append(
            "\t".join(
                (
                    sample.captured_at.isoformat(),
                    f"{sample.wallet_balance:.2f}",
                    f"{sample.assets_value:.2f}",
                    f"{sample.total:.2f}",
                )
            )
        )
    return "\n".join(lines) + "\n"


class WealthStore:
    """Every character's wealth history, persisted to disk and appended to over time."""

    def __init__(self, path: Path, *, min_interval: timedelta = _MIN_SAMPLE_INTERVAL) -> None:
        self._path = path
        self._min_interval = min_interval
        self._history = _load(path)

    def history(self, character_id: int) -> list[WealthSample]:
        """This character's samples, oldest first (a copy — callers can't mutate state)."""
        return list(self._history.get(character_id, []))

    def record(self, character_id: int, sample: WealthSample) -> bool:
        """Append ``sample`` unless the character's latest one is newer than the throttle
        interval. Returns whether it was stored, so a caller can refresh the plot only on
        a real change."""
        samples = self._history.setdefault(character_id, [])
        if samples and sample.captured_at - samples[-1].captured_at < self._min_interval:
            return False
        samples.append(sample)
        self._save()
        return True

    def export_tsv(self, character_id: int, dest: Path) -> None:
        """Write this character's history to ``dest`` as TSV."""
        dest.write_text(format_tsv(self.history(character_id)), encoding="utf-8")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            str(character_id): [
                {
                    "captured_at": sample.captured_at.isoformat(),
                    "wallet_balance": sample.wallet_balance,
                    "assets_value": sample.assets_value,
                }
                for sample in samples
            ]
            for character_id, samples in self._history.items()
        }
        self._path.write_text(json.dumps(payload), encoding="utf-8")
