# Kimi and OpenRouter engines via Claude Code API mode

Implements [#69](https://github.com/Zaimwa9/themis/issues/69).

## Goal

Repos can set `engine: kimi` or `engine: openrouter` (or the instance can set
`THEMIS_ENGINE=kimi|openrouter`) and review with Kimi models — directly
through Moonshot's platform, or through OpenRouter's many-model gateway —
using the same `claude` binary and hardening the claude/glm engines already
use, pointed at each provider's Anthropic-compatible endpoint.

## Scope decision: no subscription endpoint

Issue #69 asks for "Kimi subscriptions". The Kimi Code subscription's
[community guidelines](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html)
(verified 2026-07-21) prohibit exactly themis's usage pattern: "Kimi Code
subscriptions are for personal interactive use only. Using it for
non-interactive purposes — such as scripted batch execution or data
annotation pipelines — goes beyond normal use", enforced by account
suspension ("Access terminated"). This mirrors the Qwen Coding Plan ToS that
dropped qwen from the glm PR (see 2026-07-13 spec, status update).

Shipped instead, both ToS-compliant for backend use:

- **kimi** — Moonshot's pay-as-you-go platform endpoint (per-token billing).
- **openrouter** — OpenRouter's Anthropic-protocol gateway ("Anthropic
  Skin"), prepaid credits, one key to many models including
  `moonshotai/kimi-*`. This is the path the issue itself suggested ("Could
  be via OpenRouter").

The subscription endpoint (`https://api.kimi.com/coding/`) is deliberately
not an engine. If Moonshot later permits unattended use, it can join as a
separate engine without touching these two.

## Approach

Declarative leaf subclasses of `AnthropicApiEngine`, exactly the glm shape:
baked-in base URL (never env- or config-controlled — a controllable URL is a
key-exfiltration vector), provider key mapped to `ANTHROPIC_AUTH_TOKEN`,
`API_TIMEOUT_MS` for long agentic turns, all claude-engine hardening
inherited (`--safe-mode`, strict/empty MCP, env allowlist, isolated
`CLAUDE_CONFIG_DIR`, process-group kill, output redaction).

## Provider facts (verified 2026-07-21 against official docs)

| | kimi | openrouter |
|---|---|---|
| Base URL | `https://api.moonshot.ai/anthropic` | `https://openrouter.ai/api` |
| Secret env var | `KIMI_API_KEY` | `OPENROUTER_API_KEY` |
| Default model | `kimi-k3` | `openrouter/auto` |
| Billing | Pay-as-you-go platform key | Prepaid credits, pay-per-token |

- Moonshot documents `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` for
  Claude Code; `kimi-k3` is its documented default model for the harness
  (256k–1M context depending on account tier; `kimi-k2.7-code` and
  `kimi-k2.6` also selectable via `model.name`).
- OpenRouter's Anthropic Skin speaks the native Anthropic Messages protocol
  at `https://openrouter.ai/api` — no local proxy. Model names are OpenRouter
  slugs (`moonshotai/kimi-k3`, `z-ai/glm-5.2`, …); `openrouter/auto` lets
  OpenRouter route. Docs suggest blanking `ANTHROPIC_API_KEY`; irrelevant
  here — it is not in the env allowlist, so the child never sees it.
- Both engines join `NATIVE_SKILLS_ENGINES` (claude harness discovers
  `.claude/skills` natively), like glm.

### Quota markers: none, deliberately

Same rationale as glm (see `glm.py` comment): text markers matched against
agent-visible output are spoofable by a prompt-steered agent, and neither
provider has a "plan exhausted, resets later" state the quota comment
describes — Moonshot insufficient balance and OpenRouter 402 (out of
credits) never auto-reset, so the "mention me later to retry" comment would
mislead. Ambiguous failures stay retryable plain `EngineError`s until #28's
structured quota classification.

## Changes

| File | Change |
|---|---|
| `src/themis/engines/kimi.py`, `openrouter.py` | New declarative leaves per the table, with rationale comments. |
| `src/themis/engines/__init__.py` | `ENGINE_NAMES += ("kimi", "openrouter")`; both join `NATIVE_SKILLS_ENGINES`; `resolve()` branches. |
| `src/themis/review_service.py` | `DEFAULT_MODELS`: `kimi-k3`, `openrouter/auto`; `_ENGINE_AUTH_HINTS`: `KIMI_API_KEY`, `OPENROUTER_API_KEY`. |
| `src/themis/security.py` | Both key vars join the secret-env list for outbound redaction. |
| `docker-compose.yml` | Agent service: `KIMI_API_KEY: ${KIMI_API_KEY:-}`, `OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}`. |
| Docs | README (prereqs, Engines, repo-config table), `docs/configuration.md` (env vars, engine values, OpenRouter slug note, billing-model note), `docs/security.md` (credential-isolation paragraph naming `GLM_API_KEY`), `examples/themis/config.yaml`. |

No changes to `base.py`, `claude.py`, `anthropic_api.py`, prompts, queue, or
GitHub plumbing.

## Security invariants

Identical to glm's:

- Provider keys cross into the child env only as `ANTHROPIC_AUTH_TOKEN`;
  the raw vars are never allowlisted through.
- Host `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` are
  not in any allowlist; baked URLs cannot be overridden by env, repo config,
  or PR content.
- Per-engine `_auth_env()` keeps credentials mutually invisible across
  engines.
- Missing key: `available()` is False, service posts the auth hint.

## Error handling

Unchanged pipeline: nonzero exit → (no quota markers) → `EngineError`,
retried up to `limits.max_attempts`, then the generic failure comment.
`effort` accepted and ignored (claude harness has no effort flag).

## Testing

- `tests/engines/test_kimi.py`, `test_openrouter.py` (fake-CLI pattern from
  `test_glm.py`): baked base URL in child env; key mapped to
  `ANTHROPIC_AUTH_TOKEN`; raw key var, sibling keys, and
  `CLAUDE_CODE_OAUTH_TOKEN` absent from child env; host `ANTHROPIC_BASE_URL`
  cannot override; hardening flags in argv; `available()` both ways; no
  quota markers → nonzero exit is plain `EngineError`.
- `test_registry.py`: new names resolve; `ENGINE_NAMES` and
  `NATIVE_SKILLS_ENGINES` updated.
- `test_security.py`: both key values redacted outbound.
- `test_config.py` / `test_review_service.py`: engine enumeration and
  default-model lookups cover the new names.
- Existing engine tests pass untouched (no shared-code changes).

## Validation follow-up (not in this change)

Live review round on a known PR with real Moonshot and OpenRouter keys to
confirm the Anthropic-protocol endpoints behave under the pinned claude CLI
(path handling, tool use, thinking blocks) and compare finding quality
against codex/claude baselines.
