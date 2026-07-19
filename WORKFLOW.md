# WORKFLOW.md

## Session start
1. Read `CLAUDE.md` and the current milestone in `ARCHITECTURE.md`.
2. Run the full check suite (see `CLAUDE.md` § Commands) to confirm the tree is
   clean before making changes.

## Making a change
1. State the plan before writing code: which module(s), what's added/changed, what
   test proves it works.
2. Implement in the smallest coherent unit — one module or one function at a time.
3. Run the relevant checks immediately (not batched at the end):
   `pytest`, `mypy --strict`, `ruff check`, `lint-imports`.
4. Do not mark a change complete with a failing or skipped check. If a check can't
   pass, say so explicitly and explain why, rather than presenting it as done.

## Adding an opportunity source
1. Implement against the `OpportunitySource` Protocol in
   `python/evetrader/advisor/source.py`. Keep it pure — it receives a
   `MarketSnapshot` + `CharacterState`, returns `list[Opportunity]`; no I/O.
2. Add a unit test with a synthetic, hand-computed expected result (an order book
   you built by hand with a known best margin), not just "runs without error".
3. Run it through the advisor engine on a small fixed snapshot before wiring it to
   live ESI data.

## Touching the ESI / I/O layer (`esi/`, `data/`)
1. Every request goes through `esi/client.py` — do not add ad-hoc HTTP calls
   elsewhere. That's where caching, error-limit backoff, and `User-Agent` live.
2. Confirm the change still honours cache headers and the error-limit budget, and
   never fires on a keystroke. If a change would push a network call into the pure
   core, stop and reconsider the boundary instead.
3. Test against recorded/fixture payloads, not the live API, so tests are
   deterministic and don't spend the error budget. Never commit real tokens or
   captured personal character data.

## Before considering a milestone done
1. All three checks green.
2. The milestone's stated deliverable in `ARCHITECTURE.md` § Milestones is met —
   not partially, not "mostly".
3. Update `ARCHITECTURE.md` if the implementation diverged from the plan; don't let
   the doc drift out of sync silently.

## Git
- Conventional commits (`feat:`, `fix:`, `test:`, `refactor:`, `docs:`).
- Write a body only when a descriptive subject line is insufficient to explain the
  change; a clear subject on its own is fine.
- Do not add a `Co-authored-by:` trailer (or any similar attribution byline).
- One logical change per commit; don't bundle an ESI-layer change with an unrelated
  analysis-core refactor.
- Never commit with a failing check, even on a WIP branch — use `git stash` or a
  local-only draft instead.
- Never commit secrets or personal character data. Keep `data/` and any token
  store gitignored.

## Escalate, don't guess
Stop and ask when a change would: alter the I/O-layer ↔ pure-core boundary, change
the ESI cache/rate-limit posture, touch OAuth/token handling, or add anything that
looks like transaction automation. Guessing here is the failure mode this file
exists to prevent.
