# Contributing a new engine

An engine is the adapter between Themis and an agent CLI: it builds the
command line, shapes a hardened environment, runs the process through the
shared harness, and translates the provider's failure modes into Themis's
retry semantics. This guide walks through adding one.

## Two paths

**Anthropic-compatible provider (the common case).** If the provider exposes
an Anthropic-compatible API endpoint that works with the Claude Code CLI —
as Z.ai (glm) and DashScope (qwen) do — you subclass `AnthropicApiEngine`
and write ~15 lines. Start from `src/themis/engines/glm.py`.

**New CLI (the rare case).** If the provider ships its own agent CLI, you
implement the `Engine` protocol directly on top of `run_cli`, the way
`src/themis/engines/codex.py` does. Everything below still applies, plus you
own the CLI's isolation flags: the equivalents of codex's
`--ignore-user-config --ignore-rules` or claude's `--setting-sources ""`.
Expect the review of that isolation surface to be the bulk of your PR.

## The contract

Every engine satisfies the `Engine` protocol (`src/themis/engines/base.py`):

| Member | Meaning |
|---|---|
| `name` | the string users put in `THEMIS_ENGINE` and `engine:`; lowercase, stable |
| `available()` | cheap, local, no network: is the credential present? |
| `run(...)` | one attempt: run the CLI in the workspace, return stdout, raise `EngineError` (retryable) or `EngineQuotaError` (not retryable) |

`effort` is part of the `run()` signature for protocol parity; accept it even
if your CLI has no matching flag (claude-harness engines ignore it).

## Anthropic-compatible walkthrough

Create `src/themis/engines/<name>.py`:

```python
from themis.engines.anthropic_api import AnthropicApiEngine

_QUOTA_MARKERS = (
    # lowercase substrings of the provider's *plan exhausted* diagnostics
)


class FooEngine(AnthropicApiEngine):
    name = "foo"
    _token_env = "FOO_API_KEY"
    _base_url = "https://api.foo.example/anthropic"
    _quota_markers = _QUOTA_MARKERS
```

Then register it everywhere an engine name lives. The map-coverage test
(`tests/test_service.py::test_engine_maps_cover_all_engine_names`) enforces
only the registry and service maps (items 1-2 below); it fails until those
two are done. Items 3-5 (redaction, compose/deployment, docs) are **not**
test-enforced — you must verify them by hand:

1. `src/themis/engines/__init__.py`: import, `ENGINE_NAMES`, `resolve()`.
2. `src/themis/service.py`: `DEFAULT_MODELS` (the provider's current
   flagship — repos can override with `model.name`) and
   `_ENGINE_AUTH_HINTS` (what the courtesy comment tells users to set).
3. `src/themis/security.py`: add the key env var to `_SECRET_ENV_VARS` so
   it is redacted from anything posted to GitHub.
4. `docker-compose.yml`, the README Quickstart's inline compose sample,
   **and** `.env.example`: pass `FOO_API_KEY: ${FOO_API_KEY:-}` to the
   **agent** service only, never the controller, and add a commented
   `#FOO_API_KEY=` stanza to `.env.example`. Compose does not forward
   variables the file doesn't reference, so forgetting the Quickstart
   sample ships a silently dead engine.
5. Docs: README (prereqs, Engines table, repo-config table,
   troubleshooting), `docs/configuration.md`, `docs/security.md`,
   `examples/themis/config.yaml`.

## Security requirements (non-negotiable)

Engines run on untrusted PR content. A hostile PR can steer the agent's
output and behavior, so the adapter's job is to make sure there is nothing
interesting to steal and nowhere to send it:

- **Bake the endpoint.** The base URL is a class constant, never an env var,
  setting, or repo-config key. A controllable base URL redirects the
  provider key to an attacker's host.
- **Map, don't leak, the credential.** The provider key enters the child
  environment only as `ANTHROPIC_AUTH_TOKEN` (or your CLI's auth mechanism);
  the raw `FOO_API_KEY` name never crosses. The allowlist in `base.py`
  guarantees no other engine's credential — and none of Themis's own
  secrets — is visible.
- **One credential per engine.** Sibling engines must not see yours; write
  the env-dump test proving it (see `tests/engines/test_qwen.py`).
- **Redact outbound.** `_SECRET_ENV_VARS` (step 3 above), with a test.
- **Don't weaken the harness.** For claude-harness engines the flag set in
  `build_command` (`--safe-mode`, `--setting-sources ""`, strict empty MCP
  config, isolated `CLAUDE_CONFIG_DIR`, web tools off by default) is shared
  on purpose; if your provider needs a different flag, raise it in the PR
  rather than forking the command builder.

## Quota markers

`run_cli` lowercases the last 2 KB of output on a nonzero exit and checks
your markers. Match means `EngineQuotaError`: Themis stops retrying and
posts "usage limit reached, mention the bot once it resets".

- Use only *plan/window exhausted* diagnostics with a documented reset
  (session, hour, week, month). Quote the provider's error-code reference in
  a comment above the tuple.
- Transient throttling (429s, "try again later", burst/concurrency limits)
  must **not** match — it has to stay a retryable `EngineError`. Watch for
  substring traps: qwen's markers are window-qualified precisely because
  `"allocated quota exceeded"` alone would also catch the retryable
  concurrency variant.
- Billing arrears don't match either: they never auto-reset, so the
  "once it resets" comment would mislead.
- Markers are best-effort until validated live; expect to tune them after a
  real limit-hit (that's fine — say so in the PR).

## Tests

Copy the fake-CLI pattern from `tests/engines/test_glm.py`: a shell script
named after the binary, prepended to `PATH`, dumping `"$@"` or `env` into
the workspace. Cover at least:

- env shaping: endpoint baked (host `ANTHROPIC_BASE_URL` ignored), key
  mapped, sibling credentials absent, config-dir isolated
- argv: hardening flags and model passthrough
- each quota marker → `EngineQuotaError`; a transient throttle message and
  a billing message → plain `EngineError`
- `available()` with and without the credential

`uv run pytest -q` and `uv run ruff check .` must be green.

## PR checklist

- [ ] Engine module + tests
- [ ] Registry, `DEFAULT_MODELS`, `_ENGINE_AUTH_HINTS`, `_SECRET_ENV_VARS`
- [ ] `docker-compose.yml` + README Quickstart compose sample (agent only) + `.env.example`
- [ ] README, `docs/configuration.md`, `docs/security.md`, example config
- [ ] Quota-marker sources cited; transient vs. exhausted distinction argued
- [ ] Live validation round done or explicitly flagged as pending
