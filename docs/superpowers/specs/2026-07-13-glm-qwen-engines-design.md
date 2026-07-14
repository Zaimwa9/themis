# GLM and Qwen engines via Claude Code API mode

Implements [#20](https://github.com/Zaimwa9/themis/issues/20) and
[#21](https://github.com/Zaimwa9/themis/issues/21).

## Goal

Repos can set `engine: glm` or `engine: qwen` (or the instance can set
`THEMIS_ENGINE=glm|qwen`) and review with GLM / Qwen coder models through the
same `claude` binary the claude engine already uses, pointed at each
provider's Anthropic-compatible endpoint. All existing hardening
(`--safe-mode`, `--setting-sources ""`, strict MCP, config-dir isolation, env
allowlist, output redaction, process-group kill) carries over unchanged.

## Approach chosen

**Parameterized adapter by subclassing `ClaudeEngine`.** `ClaudeEngine` grows
class-level knobs (`name`, `_token_env`, `_quota_markers`) and a small
`_auth_env()` hook; a new `AnthropicApiEngine` subclass overrides the hook to
inject `ANTHROPIC_BASE_URL` (baked-in constant) and `ANTHROPIC_AUTH_TOKEN`
(mapped from the provider's key env var). `GlmEngine` and `QwenEngine` are
declarative leaf subclasses: name, token env var, base URL, quota markers.

Rejected alternatives:

- *Copy-paste engines* (`glm.py` duplicating `claude.py`): triplicates the
  security-critical CLI flag set; flag drift across copies is a sandbox
  regression waiting to happen.
- *Config-driven generic engine* (base URL from env or repo config): a
  controlled base URL is a key-exfiltration vector — a hostile value points
  the provider key at an attacker host. Both issues explicitly bake the
  endpoint into the adapter. Rejected on the same grounds.

Deviation from issue #20's sketch: default models stay in
`service.DEFAULT_MODELS` (keyed by engine name, next to codex/claude) instead
of moving into engine constructors, so model defaults keep a single home.

## Provider facts (verified 2026-07-13 against official docs)

| | glm | qwen |
|---|---|---|
| Base URL | `https://api.z.ai/api/anthropic` | `https://coding-intl.dashscope.aliyuncs.com/apps/anthropic` |
| Secret env var | `GLM_API_KEY` | `QWEN_API_KEY` |
| Default model | `glm-5.2` | `qwen3.7-plus` |
| Plan | GLM Coding Plan | Qwen Coding Plan (international) |

- Both providers document `ANTHROPIC_AUTH_TOKEN` (not `ANTHROPIC_API_KEY`) as
  the auth variable for Claude Code.
- Z.ai recommends `API_TIMEOUT_MS=3000000` for long agentic turns; set it in
  the API-mode adapter env (harmless — `run_cli` still enforces the themis
  wall clock).
- Qwen scoping: DashScope now has plan/region-specific endpoints (the 2025
  single proxy URL is deprecated and frozen on `qwen3-coder-plus`). v1 bakes
  the **Coding Plan international** endpoint — the subscription model matches
  themis's positioning (Codex / Claude Max / GLM Coding Plan). Mainland
  (`coding.dashscope.aliyuncs.com`) and pay-as-you-go (per-workspace URL)
  variants are out of scope; if demand appears, add an allowlisted region
  switch — never a free-form URL.

### Quota markers

`run_cli` matches lowercase markers against the lowered output tail on
nonzero exit; markers must catch "exhausted, don't retry" and must not catch
transient throttling.

- **glm** (Z.ai error messages; codes 1308–1310, 1316–1321):
  `"usage limit reached for"`, `"limit exhausted"`,
  `"coding plan package has expired"`.
  Safe against transients: code 1302 says "*Rate* limit reached for
  requests" which `"usage limit reached for"` does not match.
- **qwen** (DashScope Coding Plan FAQ):
  `"hour allocated quota exceeded"`, `"week allocated quota exceeded"`,
  `"month allocated quota exceeded"`.
  Deliberately excludes `"concurrency allocated quota exceeded"`, which is
  documented as retryable. Billing exhaustion (`Arrearage`, bill overdue)
  stays a plain `EngineError`: it never auto-resets, so the "mention the bot
  once it resets" quota comment would mislead.
- Known limitation (both): Claude Code classifies plan limits via
  Anthropic-native quota headers that third-party proxies don't emit, so it
  may auto-retry a hard 429 internally and surface a generic error. Markers
  are best-effort against the documented message bodies; both issues plan a
  live validation round to tune them.

## Changes

| File | Change |
|---|---|
| `src/themis/engines/claude.py` | Generalize: class attrs `_token_env`, `_quota_markers`, hook `_auth_env()`; behavior of the `claude` engine unchanged (existing tests must pass untouched). |
| `src/themis/engines/anthropic_api.py` | New. `AnthropicApiEngine(ClaudeEngine)`: `_auth_env()` returns `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (+ `API_TIMEOUT_MS`); `available()` inherited (token env var present). |
| `src/themis/engines/glm.py`, `qwen.py` | New. Declarative leaf classes per the table above, with quota-marker rationale comments. |
| `src/themis/engines/__init__.py` | `ENGINE_NAMES = ("codex", "claude", "glm", "qwen")`; `resolve()` branches. |
| `src/themis/service.py` | `DEFAULT_MODELS` + `_ENGINE_AUTH_HINTS` entries for glm/qwen. |
| `src/themis/security.py` | `GLM_API_KEY`, `QWEN_API_KEY` join `_SECRET_ENV_VARS` so provider keys are outbound-redacted like the Claude token. |
| `docker-compose.yml` | Agent service: `GLM_API_KEY: ${GLM_API_KEY:-}`, `QWEN_API_KEY: ${QWEN_API_KEY:-}`. |
| Docs | README (prereqs, Engines, repo-config table, troubleshooting), `docs/configuration.md`, `docs/security.md` (allowlist description), `examples/themis/config.yaml`. |

No changes to `base.py`, prompts, queue, or GitHub plumbing.

## Security invariants

- The provider key crosses into the child env **only** as
  `ANTHROPIC_AUTH_TOKEN`; `GLM_API_KEY`/`QWEN_API_KEY` themselves are never
  allowlisted through.
- Host `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` are
  not in any allowlist; the baked URL cannot be overridden from the
  environment, repo config, or PR content.
- `CLAUDE_CODE_OAUTH_TOKEN` is not visible to glm/qwen runs, and provider
  keys are not visible to claude runs (per-engine `_auth_env`).
- Missing token at `run()` time behaves like the claude engine today: the
  variable is simply absent and the CLI fails auth (service checks
  `available()` first and posts the auth hint instead).

## Error handling

Unchanged pipeline: nonzero exit → quota markers → `EngineQuotaError` (no
retry, courtesy comment) else `EngineError` (retry up to
`limits.max_attempts`). `effort` accepted and ignored, as the claude engine
does (`claude -p` has no effort flag).

## Testing

- `tests/engines/test_glm.py`, `test_qwen.py` (fake-CLI pattern from
  `test_claude.py`): baked base URL in child env; key mapped to
  `ANTHROPIC_AUTH_TOKEN`; raw key var and `CLAUDE_CODE_OAUTH_TOKEN` absent
  from child env; host `ANTHROPIC_BASE_URL` cannot override the baked one;
  hardening flags present in argv; quota markers → `EngineQuotaError`;
  qwen concurrency marker → plain `EngineError`; `available()` both ways.
- `test_claude.py` passes without modification (refactor invariant).
- `test_registry.py`: new names resolve; `ENGINE_NAMES` updated.
- `test_config.py` / `test_service.py`: engine validation and default-model
  lookups cover the new names where they enumerate engines.

## Validation follow-up (not in this change)

Live review round on a known PR with real GLM/Qwen keys to compare finding
quality against codex/claude baselines and tune quota markers (issues #20/#21
"Validation").

## Status update (2026-07-13)

qwen dropped post-review: Alibaba's Qwen Coding Plan ToS (and the Token Plan's)
prohibits unattended/backend use of the plan's API key — "Do not use the
plan's API key for automated scripts, application backends, or other
non-interactive scenarios… may result in subscription suspension or API key
revocation" (https://www.alibabacloud.com/help/en/model-studio/coding-plan).
Only DashScope's pay-as-you-go tier permits this usage pattern. glm is
unaffected and stays in this PR. Issue #21 will be redesigned later against
pay-as-you-go instead of the Coding Plan.
