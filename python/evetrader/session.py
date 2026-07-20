"""Persistent record of the characters set up on this machine. Impure (file I/O).

Stores only id + display name in a small JSON file (gitignored data dir); the SSO
refresh tokens live in the OS keyring, keyed by the same character id. The picker
screen reads this to list characters and add/remove them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CharacterRecord:
    character_id: int
    name: str


def _load(path: Path) -> list[CharacterRecord]:
    if not path.exists():
        return []
    stored = json.loads(path.read_text(encoding="utf-8"))
    return [
        CharacterRecord(character_id=int(item["character_id"]), name=str(item["name"]))
        for item in stored
    ]


class CharacterStore:
    """The set of logged-in characters, persisted to disk."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records = _load(path)

    def records(self) -> list[CharacterRecord]:
        return list(self._records)

    def add(self, record: CharacterRecord) -> None:
        """Add or update a character (dedup by id), keeping insertion order."""
        self._records = [r for r in self._records if r.character_id != record.character_id]
        self._records.append(record)
        self._save()

    def remove(self, character_id: int) -> None:
        self._records = [r for r in self._records if r.character_id != character_id]
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"character_id": r.character_id, "name": r.name} for r in self._records]
        self._path.write_text(json.dumps(payload), encoding="utf-8")
