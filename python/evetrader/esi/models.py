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


class CharacterSkills(_EsiModel):
    """GET /characters/{id}/skills/ — the character's trained skills."""

    skills: list[Skill]
    total_sp: int


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
