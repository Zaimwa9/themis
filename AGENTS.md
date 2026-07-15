# Themis — agent guide

Self-hosted GitHub PR review bot. FastAPI controller + credential-isolated
agent service, in-memory job queue, engine adapters over agent CLIs
(`codex`, `claude`, `glm`). No database, no broker.

## Commands

```bash
uv sync --locked        # install deps (Python 3.12, uv)
uv run pytest -q        # test suite
uv run ruff check src tests
uv run python -m themis # run locally (reads THEMIS_* env)
```

CI runs exactly pytest and ruff; both must pass.

## Layout

- `src/themis/app.py` — FastAPI app factory, startup (webhook self-registration)
- `src/themis/router.py` — webhook + API routes; events become queue jobs
- `src/themis/events.py` — webhook payload parsing, trigger decisions
- `src/themis/queue.py` — in-memory queue, one worker, dedup
- `src/themis/review_service.py` — review pipeline: clone, engine run, parse, post
- `src/themis/engines/` — engine adapters over a hardened subprocess runner
  (`base.py`: env allowlist, process-group kill, quota detection);
  `__init__.py` is the registry
- `src/themis/github/` — App auth (JWT, installation tokens), REST/GraphQL client
- `src/themis/security.py` — outbound redaction of secrets
- `src/themis/prompts.py` / `output.py` — prompt builders / agent output parsing
- `src/themis/workspace.py` — shallow clone, token scrub, cleanup
- `src/themis/config.py` — `THEMIS_*` env settings + per-repo `.themis/config.yaml`
- `tests/` — mirrors `src/themis/`, one test file per module, pytest-asyncio

## Rules

- Conventional Commits; release-please derives versions and the changelog
  from commit messages.
- Behavior changes ship with tests in the mirrored `tests/test_<module>.py`;
  bug fixes ship a regression test that fails without the fix.
- Every string that ends up on GitHub passes `redact_outbound`
  (`src/themis/security.py`). A new posting path without it is a blocker.
- Engine subprocesses receive `allowlisted_env` only, never `os.environ`.
- Per-repo config parsing is lenient: invalid values degrade to defaults with
  a warning, never a crash.
- User-facing config changes (env vars, `.themis/config.yaml` keys) update
  `README.md` and `docs/configuration.md` in the same PR.
- Engines are siblings: a guard or edge case added to one engine is checked
  against the others.
- Log lines use `themis_<event>` snake_case with key=value pairs, never
  secret values.
- No new runtime dependencies without a stated reason; the one-container,
  no-external-services shape is a feature.

## References

- [`README.md`](README.md) — setup paths (quick start, production, headless)
- [`docs/configuration.md`](docs/configuration.md) — full env + repo config reference
- [`docs/security.md`](docs/security.md) — trust model and guardrails
- [`docs/contributing-engines.md`](docs/contributing-engines.md) — adding an engine
- [`docs/headless.md`](docs/headless.md) — `/api/review`, `/api/discuss` contracts
- [`.themis/review.md`](.themis/review.md) — this repo's own review doctrine
