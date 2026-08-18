"""SSO auth: PKCE derivation, authorize URL, JWT character id, token exchange,
and Authenticator auto-refresh with refresh-token rotation."""

import asyncio
import base64
import hashlib
import json
import urllib.parse
from collections.abc import Awaitable, Callable

import httpx
import pytest

from evetrader.config import Config
from evetrader.esi.auth import (
    AuthError,
    Authenticator,
    build_authorize_url,
    character_id_from_access_token,
    character_identity,
    exchange_code,
    generate_pkce,
)


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="contact@example.com",
    )


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_jwt(sub: str, name: str | None = None) -> str:
    header = _b64url(json.dumps({"alg": "RS256"}).encode())
    claims: dict[str, str] = {"sub": sub}
    if name is not None:
        claims["name"] = name
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.signature"


class _FakeStore:
    def __init__(self) -> None:
        self.tokens: dict[int, str] = {}

    def load(self, character_id: int) -> str | None:
        return self.tokens.get(character_id)

    def save(self, character_id: int, refresh_token: str) -> None:
        self.tokens[character_id] = refresh_token

    def delete(self, character_id: int) -> None:
        self.tokens.pop(character_id, None)


def _run(coro: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(coro())


def test_generate_pkce_uses_s256() -> None:
    pair = generate_pkce(b"\x00" * 32)
    expected = _b64url(hashlib.sha256(pair.verifier.encode("ascii")).digest())
    assert pair.challenge == expected


def test_build_authorize_url_encodes_scopes_and_method() -> None:
    url = build_authorize_url(
        client_id="cid",
        redirect_uri="http://localhost:8765/callback",
        scopes=["esi-wallet.read_character_wallet.v1", "esi-assets.read_assets.v1"],
        challenge="chal",
        state="xyz",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["esi-wallet.read_character_wallet.v1 esi-assets.read_assets.v1"]
    assert query["redirect_uri"] == ["http://localhost:8765/callback"]


def test_character_id_extracted_from_jwt_sub() -> None:
    token = _make_jwt("CHARACTER:EVE:2112625428")
    assert character_id_from_access_token(token) == 2112625428


def test_non_jwt_access_token_raises() -> None:
    with pytest.raises(AuthError):
        character_id_from_access_token("not-a-jwt")


def test_character_identity_reads_id_and_name() -> None:
    identity = character_identity(_make_jwt("CHARACTER:EVE:2112625428", name="Jane Doe"))
    assert identity.character_id == 2112625428
    assert identity.name == "Jane Doe"


def test_character_identity_falls_back_when_name_absent() -> None:
    identity = character_identity(_make_jwt("CHARACTER:EVE:42"))
    assert identity.name == "Character 42"


def test_exchange_code_posts_pkce_verifier() -> None:
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(urllib.parse.parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": "a1",
                "token_type": "Bearer",
                "expires_in": 1199,
                "refresh_token": "r1",
            },
        )

    async def body() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            token = await exchange_code(
                http, client_id="cid", contact="c@e.com", code="thecode", verifier="theverifier"
            )
            assert token.refresh_token == "r1"

    _run(body)
    assert seen["grant_type"] == ["authorization_code"]
    assert seen["code_verifier"] == ["theverifier"]
    assert seen["code"] == ["thecode"]


def test_authenticator_refreshes_rotates_and_caches() -> None:
    calls = 0
    sent_refresh: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        form = urllib.parse.parse_qs(request.content.decode())
        sent_refresh.append(form["refresh_token"][0])
        return httpx.Response(
            200,
            json={
                "access_token": f"a{calls}",
                "token_type": "Bearer",
                "expires_in": 1200,
                "refresh_token": f"r{calls}",
            },
        )

    store = _FakeStore()
    store.save(42, "r0")
    now = [1000.0]
    from datetime import UTC, datetime, timedelta

    def clock() -> datetime:
        return datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=now[0])

    async def body() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            auth = Authenticator(_config(), http, store, now=clock)

            first = await auth.access_token(42)
            assert first == "a1"
            assert store.tokens[42] == "r1"  # rotated

            second = await auth.access_token(42)
            assert second == "a1"  # cached, no refresh

            now[0] += 1200  # past expiry (minus skew)
            third = await auth.access_token(42)
            assert third == "a2"
            assert store.tokens[42] == "r2"

    _run(body)
    assert calls == 2
    assert sent_refresh == ["r0", "r1"]  # each refresh uses the latest rotated token


def test_authenticator_keeps_tokens_separate_per_character() -> None:
    # Each character's refresh must use ITS OWN stored token and cache its own
    # access token — not leak one character's token to another.
    def handler(request: httpx.Request) -> httpx.Response:
        form = urllib.parse.parse_qs(request.content.decode())
        incoming = form["refresh_token"][0]
        # Echo which character's token was presented so we can assert isolation.
        return httpx.Response(
            200,
            json={
                "access_token": f"access-for-{incoming}",
                "token_type": "Bearer",
                "expires_in": 1200,
                "refresh_token": incoming,
            },
        )

    store = _FakeStore()
    store.save(1, "refresh-1")
    store.save(2, "refresh-2")

    async def body() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            auth = Authenticator(_config(), http, store)
            assert await auth.access_token(1) == "access-for-refresh-1"
            # With the bug, this returned character 1's cached token.
            assert await auth.access_token(2) == "access-for-refresh-2"
            # Selecting 1 again must still return 1's token, from its own cache.
            assert await auth.access_token(1) == "access-for-refresh-1"

    _run(body)


def test_authenticator_without_stored_token_raises() -> None:
    async def body() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        async with httpx.AsyncClient(transport=transport) as http:
            auth = Authenticator(_config(), http, _FakeStore())
            with pytest.raises(AuthError):
                await auth.access_token(999)

    _run(body)
