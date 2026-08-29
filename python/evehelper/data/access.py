"""Per-character "last accessed" timestamps, persisted to a small JSON file. Impure
(file I/O).

The Overview digest lists completed trades since the previous time the character was
opened, so each session records "now" and reads back the prior value as the cutoff.
Stored separately from the character store (id + name) because it is written on every
session start, not just on add/remove.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _load(path: Path) -> dict[int, datetime]:
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    times: dict[int, datetime] = {}
    for key, value in stored.items():
        try:
            times[int(key)] = datetime.fromisoformat(str(value))
        except ValueError:
            continue
    return times


class LastAccessStore:
    """When each character was last opened, persisted to disk."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._times = _load(path)

    def record_access(self, character_id: int, now: datetime) -> datetime | None:
        """Persist ``now`` as this character's latest access and return the previous
        value — the cutoff for "what's happened since your last visit", or None the
        first time the character is opened."""
        previous = self._times.get(character_id)
        self._times[character_id] = now
        self._save()
        return previous

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(cid): when.isoformat() for cid, when in self._times.items()}
        self._path.write_text(json.dumps(payload), encoding="utf-8")
