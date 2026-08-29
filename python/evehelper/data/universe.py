"""Universe reference: resolve ids to names via ESI, cached persistently.

Universe names are immutable and the client's Expires-cache does not cover POST, so
this keeps a local id->name file (under the gitignored data/ dir): each id is
resolved over the wire at most once, ever. Only ids not already cached are fetched,
in chunks of 1000 (the /universe/names/ limit).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from evehelper.esi.client import EsiClient
from evehelper.esi.endpoints import resolve_names

_CHUNK = 1000


def _load(path: Path) -> dict[int, str]:
    if not path.exists():
        return {}
    stored = json.loads(path.read_text(encoding="utf-8"))
    return {int(key): str(value) for key, value in stored.items()}


class NameCache:
    """Persistent id->name cache backed by ESI POST /universe/names/."""

    def __init__(self, path: Path, client: EsiClient) -> None:
        self._path = path
        self._client = client
        self._names = _load(path)

    async def resolve(self, ids: Sequence[int]) -> dict[int, str]:
        """Return id->name for every id, fetching only ones not already cached."""
        missing = sorted({identifier for identifier in ids if identifier not in self._names})
        if missing:
            for start in range(0, len(missing), _CHUNK):
                for name in await resolve_names(self._client, missing[start : start + _CHUNK]):
                    self._names[name.id] = name.name
            self._save()
        return {identifier: self._names[identifier] for identifier in ids}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(key): value for key, value in self._names.items()}
        self._path.write_text(json.dumps(payload), encoding="utf-8")
