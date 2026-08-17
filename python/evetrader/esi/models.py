"""Pydantic models for ESI payloads — the I/O boundary.

Inbound external data: extras are ignored (ESI may add fields) but every field we
consume is strictly typed. These never cross into the analysis core as raw dicts.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class _EsiModel(BaseModel):
    # Frozen snapshots; tolerate unknown fields ESI may add over time.
    model_config = ConfigDict(frozen=True, extra="ignore")


class TokenResponse(_EsiModel):
    """SSO token endpoint response (authorization-code and refresh grants)."""

    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str


class Asset(_EsiModel):
    """One row of GET /characters/{id}/assets/."""

    item_id: int
    type_id: int
    quantity: int
    location_id: int
    location_flag: str
    location_type: str
    is_singleton: bool


class AssetName(_EsiModel):
    """One row of POST /characters/{id}/assets/names/ — a singleton item's assigned
    name (ships, containers). Unnamed items come back as the string ``"None"``."""

    item_id: int
    name: str


class Blueprint(_EsiModel):
    """One row of GET /characters/{id}/blueprints/.

    ``runs`` is -1 for a blueprint original (BPO, unlimited runs); a positive count
    is the runs remaining on a blueprint copy (BPC). ``quantity`` is -1 for a single
    original, -2 for a copy, or a positive stack size of (unresearched) originals.
    ``material_efficiency``/``time_efficiency`` are the percentage saved (0-10 / 0-20).
    """

    item_id: int
    type_id: int
    location_id: int
    location_flag: str
    quantity: int
    material_efficiency: int
    time_efficiency: int
    runs: int


class IndustryJob(_EsiModel):
    """One row of GET /characters/{id}/industry/jobs/ (a running/ready job).

    ``activity_id`` is the industry activity (1 manufacturing, 3 TE research, 4 ME
    research, 5 copying, 8 invention, 9 reactions). ``product_type_id`` is the item
    produced (manufacturing/invention/reactions); it's absent for research/copying,
    where the blueprint itself is the subject. ``status`` is one of active / paused /
    ready / delivered / cancelled / reverted."""

    job_id: int
    activity_id: int
    blueprint_type_id: int
    product_type_id: int | None = None
    facility_id: int
    runs: int
    status: str
    start_date: datetime
    end_date: datetime
    cost: float | None = None
    probability: float | None = None  # invention success chance, when applicable


class CharacterOrder(_EsiModel):
    """One row of GET /characters/{id}/orders/ (the character's open orders)."""

    order_id: int
    type_id: int
    region_id: int
    location_id: int
    # ESI omits is_buy_order on sell orders; absent means a sell order.
    is_buy_order: bool = False
    price: float
    volume_remain: int
    volume_total: int
    min_volume: int = 1
    duration: int
    issued: datetime
    range: str


class Location(_EsiModel):
    """GET /characters/{id}/location/ — exactly one of station/structure is set."""

    solar_system_id: int
    station_id: int | None = None
    structure_id: int | None = None


class MarketOrder(_EsiModel):
    """One row of GET /markets/{region_id}/orders/ (public regional market)."""

    order_id: int
    type_id: int
    location_id: int
    system_id: int
    is_buy_order: bool
    price: float
    volume_remain: int
    volume_total: int
    min_volume: int
    range: str
    duration: int
    issued: datetime


class MarketHistoryDay(_EsiModel):
    """One row of GET /markets/{region_id}/history/ (daily aggregates per type)."""

    date: date
    average: float
    highest: float
    lowest: float
    order_count: int
    volume: int


class EsiName(_EsiModel):
    """One row of POST /universe/names/ — an id resolved to a name and category."""

    id: int
    name: str
    category: str


class Skill(_EsiModel):
    """One trained skill from GET /characters/{id}/skills/."""

    skill_id: int
    active_skill_level: int
    trained_skill_level: int
    # Skill points accumulated in this skill — lets a training-time estimate start from
    # the character's actual progress through the current level, not just its floor.
    skillpoints_in_skill: int = 0


class CharacterSkills(_EsiModel):
    """GET /characters/{id}/skills/ — the character's trained skills."""

    skills: list[Skill]
    total_sp: int


class CharacterAttributes(_EsiModel):
    """GET /characters/{id}/attributes/ — the five learning attributes (post-remap,
    excluding implant bonuses, which this endpoint doesn't expose). They set the SP/min
    training rate: ``primary + secondary / 2``."""

    charisma: int
    intelligence: int
    memory: int
    perception: int
    willpower: int


class Standing(_EsiModel):
    """One row of GET /characters/{id}/standings/ (toward faction/corp/agent)."""

    from_id: int
    from_type: str
    standing: float


class SkillQueueEntry(_EsiModel):
    """One row of GET /characters/{id}/skillqueue/ (a queued skill level)."""

    skill_id: int
    finished_level: int
    queue_position: int
    start_date: datetime | None = None
    finish_date: datetime | None = None
    # SP boundaries of this level and the SP the character held when this entry
    # started training. Absent on a paused/undated queue; present otherwise.
    training_start_sp: int | None = None
    level_start_sp: int | None = None
    level_end_sp: int | None = None


class Station(_EsiModel):
    """GET /universe/stations/{station_id}/ — an NPC station's public data."""

    station_id: int
    name: str
    system_id: int
    type_id: int
    # Owning corporation id (present for NPC stations); needed for broker standings.
    owner: int | None = None


class Corporation(_EsiModel):
    """GET /corporations/{corporation_id}/ — only the fields we need."""

    name: str
    # Present for NPC corporations; the faction whose standing affects broker fees.
    faction_id: int | None = None


class Structure(_EsiModel):
    """GET /universe/structures/{structure_id}/ — a player-owned Upwell structure.

    Requires docking access (the `esi-universe.read_structures.v1` scope); an
    inaccessible structure returns 403 rather than these fields.
    """

    name: str
    solar_system_id: int
    type_id: int | None = None
    owner_id: int | None = None
