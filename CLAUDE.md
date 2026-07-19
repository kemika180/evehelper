# CLAUDE.md

## Project
A TUI advisor for EVE Online. It reads your character state and live market data
from ESI (the official EVE REST API), analyses money-making opportunities, and
tells you what to do while you play — it never automates transactions (EVE forbids
it). Pure Python. See `ARCHITECTURE.md` for full design and current milestone,
`WORKFLOW.md` for day-to-day dev process.

## Non-negotiables
- No vibe coding: no unverified assumptions, no code presented as done before it
  has run against the linters/tests.
- Strict typing everywhere: `mypy --strict` on all Python, no `Any`, no untyped
  dicts crossing module boundaries. ESI payloads and config are pydantic models.
- Modular: one responsibility per module. If a function needs a comment explaining
  *what* it does rather than *why*, split it.
- **Analysis core (`market/`, `advisor/`) stays pure**: no network, no wall-clock
  reads, no token access. Deterministic given (market snapshot, character state,
  config). All ESI/network I/O lives only in `esi/` and `data/`.
- **Respect ESI's rules** — a ToS matter, not just etiquette:
  - Honour cache headers (`Expires`, `ETag`); use `If-None-Match`. Never re-fetch
    before `Expires`.
  - Honour the error-limit budget (`X-Esi-Error-Limit-Remain`/`-Reset`); back off
    before it reaches zero.
  - Send a descriptive `User-Agent` with contact info.
  - Never issue an ESI call in response to a keystroke — refresh is driven by
    cache expiry, not the UI.
- No transaction automation. The tool advises; the human executes. There is no
  write path to the game.
- Secrets (SSO tokens, client id) are never committed and never logged.

## Commands
```
uv run pytest tests/python -v
uv run mypy --strict python/
uv run ruff check python/
uv run lint-imports          # enforces the pure-core import boundary

# Launch the TUI (once milestone 1 lands)
uv run evetrader
```
All four checks (pytest, mypy, ruff, import-linter) must pass before any change is
presented as complete. Do not skip a check because "it's just a small change."

## Layout
See `ARCHITECTURE.md` § Repo layout. Quick pointers:
- `python/evetrader/esi/`, `data/` — all network I/O (impure)
- `python/evetrader/market/`, `advisor/` — pure analysis core
- `python/evetrader/tui/` — Textual app
- `tests/python/`

## Conventions
- Config: pydantic models only, never raw dicts, in `python/evetrader/config.py`.
- Opportunity sources: implement the `OpportunitySource` **Protocol** in
  `python/evetrader/advisor/source.py`; don't subclass a base class.
- ESI payloads: pydantic models at the I/O boundary (`esi/models.py`), never raw
  dicts crossing into the analysis core.
- Data: `polars`, not `pandas`.
- TUI: `textual`. HTTP: an async client (e.g. `httpx`), all inside `esi/client.py`.
- All network calls go through `esi/client.py` so caching/error-limit/`User-Agent`
  handling is enforced in exactly one place.

## When uncertain
If a design decision isn't covered in `ARCHITECTURE.md`, stop and ask rather than
guessing — especially anything touching the I/O-layer ↔ pure-core boundary, the
ESI cache/rate-limit posture, or OAuth/token handling.
