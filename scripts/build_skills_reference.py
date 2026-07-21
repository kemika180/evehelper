"""Regenerate the bundled skills reference (`evetrader/data/skills.json`).

A one-time, dev-only build step — NOT part of the running app, which reads only the
bundled file offline. Walks the public ESI universe endpoints (skill category ->
groups -> types) and records each skill's name, training rank, primary/secondary
attribute, and description. Run with `uv run python scripts/build_skills_reference.py`.

Skills are static game data, so the output is checked in and refreshed only when CCP
adds or rebalances skills.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

_BASE = "https://esi.evetech.net/latest"
_PARAMS = {"datasource": "tranquility"}
_USER_AGENT = "evetrader skills-reference build (jkgurchiek@gmail.com)"
_SKILL_CATEGORY = 16
_RANK = 275  # skillTimeConstant
_PRIMARY = 180
_SECONDARY = 181
_ATTRIBUTE_NAMES = {
    164: "Charisma",
    165: "Intelligence",
    166: "Memory",
    167: "Perception",
    168: "Willpower",
}
_OUT = Path(__file__).resolve().parent.parent / "python" / "evetrader" / "data" / "skills.json"


async def _get(client: httpx.AsyncClient, path: str) -> dict | list:
    response = await client.get(f"{_BASE}{path}", params=_PARAMS)
    response.raise_for_status()
    return response.json()


async def _skill_type_ids(client: httpx.AsyncClient) -> list[int]:
    category = await _get(client, f"/universe/categories/{_SKILL_CATEGORY}/")
    assert isinstance(category, dict)
    type_ids: list[int] = []
    for group_id in category["groups"]:
        group = await _get(client, f"/universe/groups/{group_id}/")
        assert isinstance(group, dict)
        if group.get("published"):
            type_ids.extend(group["types"])
    return sorted(set(type_ids))


async def _skill(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, type_id: int
) -> tuple[int, dict] | None:
    async with sem:
        payload = await _get(client, f"/universe/types/{type_id}/")
    assert isinstance(payload, dict)
    if not payload.get("published"):
        return None
    attrs = {a["attribute_id"]: a["value"] for a in payload.get("dogma_attributes", [])}
    if _RANK not in attrs:  # not actually a trainable skill
        return None
    return type_id, {
        "name": payload["name"],
        "rank": int(attrs[_RANK]),
        "primary": _ATTRIBUTE_NAMES.get(int(attrs.get(_PRIMARY, 0)), "?"),
        "secondary": _ATTRIBUTE_NAMES.get(int(attrs.get(_SECONDARY, 0)), "?"),
        "description": payload.get("description", "").strip(),
    }


async def main() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=30.0) as client:
        type_ids = await _skill_type_ids(client)
        print(f"Fetching {len(type_ids)} skill types…")
        sem = asyncio.Semaphore(16)
        results = await asyncio.gather(*(_skill(client, sem, tid) for tid in type_ids))
    skills = {str(tid): info for pair in results if pair is not None for tid, info in [pair]}
    payload = json.dumps(skills, ensure_ascii=False, indent=0, sort_keys=True)
    _OUT.write_text(payload, encoding="utf-8")
    print(f"Wrote {len(skills)} skills to {_OUT}")


if __name__ == "__main__":
    asyncio.run(main())
