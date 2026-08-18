"""User configuration for a trading session.

Pydantic models only, never raw dicts. This module is pure data — no I/O — so the
analysis core may import it freely. Loading config from disk lives elsewhere.
"""

from pydantic import BaseModel, ConfigDict, Field


class RefiningParams(BaseModel):
    """Reprocessing model for the craft-cost self-source comparison.

    ``base_rate`` is the station/structure's *base* reprocessing yield — the part the
    character's reprocessing skills multiply on top of (``market/refining.py`` applies the
    skill bonuses per ore from live skill levels). It's a user-owned input, not a code
    constant: ~0.50 at a raw NPC station, higher in an upgraded
    structure (rig/implant bonuses this tool can't detect fold in here too). It should be
    CONFIRMED against the live game. (Before skill-based refining this field was the *total*
    effective yield, ~0.70; it is now just the base, so with skills untrained refine costs
    read higher until the skills are entered.)
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_rate: float = Field(default=0.50, gt=0.0, le=1.0)


class TrainingParams(BaseModel):
    """Policy for the Crafting tab's quick-train tips — which skill level-ups to surface as
    cheap wins for lowering a build's self-source cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Only suggest a skill level that trains within this many hours (the "trained quickly"
    # cutoff); tips are ranked by ISK saved on the craft.
    quick_horizon_hours: float = Field(default=3.0, gt=0.0)


class IndustryParams(BaseModel):
    """Industry-job cost inputs — user-owned, game/location-dependent values to confirm
    against the live game, not code constants."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # System *copying* cost index — used to estimate a component BPC's copy-job cost
    # (EIV x index x runs). Varies per system (a few percent); the default is a typical highsec
    # value and should be CONFIRMED against `GET /industry/systems/` for your home system.
    copy_cost_index: float = Field(default=0.02, ge=0.0)


class Config(BaseModel):
    """Top-level user configuration for a trading session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # SSO application client id. PUBLIC, not a secret — a PKCE native app has no client
    # secret, and the id is visible in the browser auth URL anyway. Defaults to the
    # project's shared registration so a fresh install needs no developer-portal setup;
    # override in config.toml to point at your own app.
    esi_client_id: str = Field(default="9e7b63317b6f4cf78ab0a3cd458a564a", min_length=1)
    # Contact sent in the ESI User-Agent, as ESI's rules require. ESI accepts a URL or an
    # email; the shared app uses its project URL so no personal address is exposed. Override
    # to your own contact if you self-host with your own client id.
    contact: str = Field(default="https://github.com/kemika180/eve_trading", min_length=1)
    # Loopback port for the PKCE SSO callback; must match the registered redirect. The
    # shared registration uses 8765, so leave this alone unless using your own app.
    callback_port: int = Field(default=8765, gt=0, lt=65536)
    # How often the TUI re-runs the pipeline; the client cache gates real fetches, and
    # renders skip when their data is unchanged, so this can be relaxed. Market orders
    # cache ~5 min and assets ~1h, so a few minutes keeps advice fresh without churn.
    refresh_interval_seconds: int = Field(default=300, gt=0)
    # TUI colour theme: "kemika-purple" (default) or any built-in Textual theme.
    theme: str = "kemika-purple"
    refining: RefiningParams = Field(default_factory=RefiningParams)
    training: TrainingParams = Field(default_factory=TrainingParams)
    industry: IndustryParams = Field(default_factory=IndustryParams)
