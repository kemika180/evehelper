"""EVE SSO (OAuth2, PKCE, native app) login, token storage, and auto-refresh.

The pure pieces (PKCE derivation, authorize-URL building, character-id extraction)
and the token HTTP exchanges are unit-tested. The one interactive piece — the
loopback ``login`` that opens a browser and catches the redirect — is exercised
manually once an ESI app is registered; it has no automated test.

Refresh tokens are held only in the OS keyring (never in our files, never logged).
ESI rotates the refresh token on every refresh, so each refresh persists the new
one immediately.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import urllib.parse
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
import keyring
from pydantic import BaseModel, ConfigDict

from evetrader import __version__
from evetrader.config import Config
from evetrader.esi.models import TokenResponse

_AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize/"
_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
_KEYRING_SERVICE = "evetrader"
# Refresh a little before the access token actually expires.
_EXPIRY_SKEW = timedelta(seconds=30)

SCOPES: tuple[str, ...] = (
    "esi-wallet.read_character_wallet.v1",
    "esi-assets.read_assets.v1",
    "esi-markets.read_character_orders.v1",
    "esi-location.read_location.v1",
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-location.read_ship_type.v1",  # current ship shown in the header
    "esi-location.read_online.v1",  # online status (character picker "logged in now")
    # Kept in the requested set to preserve existing tokens/app-registration even though
    # the code no longer reads standings (fees were removed 2026-08-17).
    "esi-characters.read_standings.v1",
    "esi-characters.read_loyalty.v1",  # LP balances per NPC corp
    # Name player-owned Upwell structures the character can dock at (asset browser).
    "esi-universe.read_structures.v1",
    # Pre-provisioned for upcoming modules so they need no further re-login (added
    # 2026-07-21). Requested now; the features that consume them land later.
    "esi-industry.read_character_jobs.v1",  # crafting / industry jobs
    "esi-characters.read_blueprints.v1",  # blueprints (ME/TE, runs)
    "esi-planets.manage_planets.v1",  # PI colonies (read setups/extractors)
    "esi-industry.read_character_mining.v1",  # mining ledger
    "esi-contracts.read_character_contracts.v1",  # courier / hauling contracts
    "esi-markets.structure_markets.v1",  # order books at private structures
)


class AuthError(Exception):
    """SSO login, token exchange, or refresh failed."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _user_agent(contact: str) -> str:
    return f"evetrader/{__version__} ({contact})"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str


def generate_pkce(entropy: bytes | None = None) -> PkcePair:
    """Derive a PKCE verifier/challenge pair (S256). ``entropy`` is for tests."""
    verifier = _b64url(entropy if entropy is not None else secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(verifier=verifier, challenge=challenge)


def build_authorize_url(
    *, client_id: str, redirect_uri: str, scopes: Sequence[str], challenge: str, state: str
) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{_AUTHORIZE_URL}?{query}"


@dataclass(frozen=True)
class CharacterIdentity:
    """A logged-in character: id and display name, from the SSO token."""

    character_id: int
    name: str


class _JwtPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sub: str
    name: str | None = None


def _decode_payload(access_token: str) -> _JwtPayload:
    """Decode the SSO JWT payload.

    The token comes straight from the SSO endpoint over TLS, so we read its claims
    without verifying the signature. Signature verification against ESI's JWKS is a
    possible hardening follow-up.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        raise AuthError("access token is not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return _JwtPayload.model_validate_json(base64.urlsafe_b64decode(padded))


def character_id_from_access_token(access_token: str) -> int:
    """Read the character id from the SSO JWT's ``sub`` (``CHARACTER:EVE:<id>``)."""
    return int(_decode_payload(access_token).sub.rsplit(":", 1)[-1])


def character_identity(access_token: str) -> CharacterIdentity:
    """Read the character id and name from the SSO JWT."""
    payload = _decode_payload(access_token)
    character_id = int(payload.sub.rsplit(":", 1)[-1])
    name = payload.name if payload.name is not None else f"Character {character_id}"
    return CharacterIdentity(character_id=character_id, name=name)


class RefreshTokenStore(Protocol):
    """Persistence for rotating refresh tokens, keyed by character id."""

    def load(self, character_id: int) -> str | None: ...
    def save(self, character_id: int, refresh_token: str) -> None: ...
    def delete(self, character_id: int) -> None: ...


class KeyringTokenStore:
    """Refresh-token store backed by the OS keyring."""

    def load(self, character_id: int) -> str | None:
        return keyring.get_password(_KEYRING_SERVICE, str(character_id))

    def save(self, character_id: int, refresh_token: str) -> None:
        keyring.set_password(_KEYRING_SERVICE, str(character_id), refresh_token)

    def delete(self, character_id: int) -> None:
        keyring.delete_password(_KEYRING_SERVICE, str(character_id))


async def _post_token(http: httpx.AsyncClient, contact: str, form: dict[str, str]) -> TokenResponse:
    response = await http.post(
        _TOKEN_URL,
        data=form,
        headers={
            "User-Agent": _user_agent(contact),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    if response.status_code != httpx.codes.OK:
        raise AuthError(f"token endpoint returned {response.status_code}")
    return TokenResponse.model_validate_json(response.content)


async def exchange_code(
    http: httpx.AsyncClient, *, client_id: str, contact: str, code: str, verifier: str
) -> TokenResponse:
    """Exchange an authorization code (+ PKCE verifier) for tokens."""
    return await _post_token(
        http,
        contact,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


async def refresh_access(
    http: httpx.AsyncClient, *, client_id: str, contact: str, refresh_token: str
) -> TokenResponse:
    """Exchange a refresh token for a fresh access token (and rotated refresh token)."""
    return await _post_token(
        http,
        contact,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )


@dataclass
class _ActiveToken:
    value: str
    expires_at: datetime


class Authenticator:
    """Supplies a valid access token, refreshing (and re-persisting) as needed."""

    def __init__(
        self,
        config: Config,
        http: httpx.AsyncClient,
        store: RefreshTokenStore,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._http = http
        self._store = store
        self._now = now
        # Cached access token PER character — sharing one slot hands the wrong
        # character's token out when multiple characters are set up.
        self._active: dict[int, _ActiveToken] = {}

    async def access_token(self, character_id: int) -> str:
        active = self._active.get(character_id)
        if active is not None and self._now() < active.expires_at:
            return active.value

        refresh_token = self._store.load(character_id)
        if refresh_token is None:
            raise AuthError(f"no stored refresh token for character {character_id}; log in first")

        token = await refresh_access(
            self._http,
            client_id=self._config.esi_client_id,
            contact=self._config.contact,
            refresh_token=refresh_token,
        )
        self._store.save(character_id, token.refresh_token)  # ESI rotates refresh tokens
        expires_at = self._now() + timedelta(seconds=token.expires_in) - _EXPIRY_SKEW
        self._active[character_id] = _ActiveToken(token.access_token, expires_at)
        return token.access_token


async def _receive_code(port: int, expected_state: str) -> str:
    """Serve one loopback request, returning the ``code`` if ``state`` matches."""
    captured: dict[str, str] = {}
    done = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        target = request_line.decode("latin-1").split(" ")[1] if request_line else ""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            b"evetrader: authorization received. You may close this tab."
        )
        await writer.drain()
        writer.close()
        code = params.get("code", [""])[0]
        state = params.get("state", [""])[0]
        if code and state == expected_state:
            captured["code"] = code
        done.set()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    async with server:
        await done.wait()
    code = captured.get("code")
    if code is None:
        raise AuthError("authorization callback missing code or state mismatch")
    return code


async def login(
    config: Config,
    http: httpx.AsyncClient,
    store: RefreshTokenStore,
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
    entropy: bytes | None = None,
) -> CharacterIdentity:
    """Run the interactive PKCE login; persist the refresh token; return the identity."""
    pkce = generate_pkce(entropy)
    state = _b64url(secrets.token_bytes(16))
    redirect_uri = f"http://localhost:{config.callback_port}/callback"
    open_browser(
        build_authorize_url(
            client_id=config.esi_client_id,
            redirect_uri=redirect_uri,
            scopes=SCOPES,
            challenge=pkce.challenge,
            state=state,
        )
    )
    code = await _receive_code(config.callback_port, state)
    token = await exchange_code(
        http,
        client_id=config.esi_client_id,
        contact=config.contact,
        code=code,
        verifier=pkce.verifier,
    )
    identity = character_identity(token.access_token)
    store.save(identity.character_id, token.refresh_token)
    return identity
