"""Pydantic models for ESI payloads — the I/O boundary.

Inbound external data: extras are ignored (ESI may add fields) but every field we
consume is strictly typed. These never cross into the analysis core as raw dicts.
"""

from datetime import datetime

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
