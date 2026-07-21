"""The bundled skills reference: static skill facts read from a checked-in file.

Skill names, training ranks, primary/secondary attributes, and descriptions are
static game data, so they ship inside the package (`skills.json`) rather than being
fetched at runtime — the app reads them offline, no ESI call. Regenerate the file
with `scripts/build_skills_reference.py` when CCP adds or rebalances skills.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files

from pydantic import BaseModel, ConfigDict


class SkillReference(BaseModel):
    """Static facts about one skill (independent of any character)."""

    model_config = ConfigDict(frozen=True)

    name: str
    group: str  # in-game skill category, e.g. "Trade", "Gunnery"
    rank: int
    primary: str
    secondary: str
    description: str


@cache
def load_skills() -> dict[int, SkillReference]:
    """Skill id -> reference, loaded once from the bundled file."""
    raw = files("evetrader.data").joinpath("skills.json").read_text(encoding="utf-8")
    stored: dict[str, dict[str, object]] = json.loads(raw)
    return {int(key): SkillReference.model_validate(value) for key, value in stored.items()}
