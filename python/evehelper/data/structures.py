"""Resolve player-owned structure names. Impure (network), with a negative cache.

``GET /universe/structures/{id}`` needs docking access; an inaccessible structure
returns 403, and 403s count against ESI's error-limit budget — so a structure that
fails once is remembered and not asked about again this session. Successful lookups
go through the client's Expires cache like any other GET.
"""

from __future__ import annotations

from evehelper.esi.client import EsiClient, EsiError
from evehelper.esi.endpoints import fetch_structure
from evehelper.esi.models import Structure


class StructureCache:
    """Per-character structure-name resolver that skips known-inaccessible ids."""

    def __init__(self, client: EsiClient) -> None:
        self._client = client
        self._inaccessible: set[int] = set()

    async def resolve(self, token: str, structure_ids: list[int]) -> dict[int, Structure]:
        """Return id->Structure for the ones the character can reach; skip the rest."""
        resolved: dict[int, Structure] = {}
        for structure_id in structure_ids:
            if structure_id in self._inaccessible:
                continue
            try:
                resolved[structure_id] = await fetch_structure(self._client, structure_id, token)
            except EsiError:
                self._inaccessible.add(structure_id)  # 403 (no access) or 404 (gone)
        return resolved
