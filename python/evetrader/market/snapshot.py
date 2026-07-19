"""MarketSnapshot: the plain-data hand-off from the I/O layer to the pure core.

Lives in the pure core (polars only, no I/O) so ``market``/``advisor`` can consume
it without importing the I/O layer. ``data/market.py`` builds instances; the core
reads them. ``captured_at`` is data passed in by the builder — the core never reads
the wall clock itself.
"""

from dataclasses import dataclass
from datetime import datetime

import polars as pl


@dataclass(frozen=True)
class MarketSnapshot:
    """Normalized market state for one region at one capture time."""

    region_id: int
    captured_at: datetime
    # One row per public market order (see data.market for the schema).
    orders: pl.DataFrame
    # One row per (type, day) of market history; may be empty.
    history: pl.DataFrame
