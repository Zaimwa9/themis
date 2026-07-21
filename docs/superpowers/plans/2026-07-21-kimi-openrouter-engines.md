# Kimi + OpenRouter Engines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `kimi` (Moonshot pay-as-you-go platform) and `openrouter` (Anthropic Skin gateway) review engines as declarative `AnthropicApiEngine` leaves, per `docs/superpowers/specs/2026-07-21-kimi-openrouter-engines-design.md`.

**Architecture:** Both engines subclass `AnthropicApiEngine` (which subclasses `ClaudeEngine`): the pinned `claude` binary runs against a baked-in Anthropic-compatible base URL with the provider key mapped to `ANTHROPIC_AUTH_TOKEN`. No changes to `base.py`, `claude.py`, or `anthropic_api.py`.

**Tech Stack:** Python 3.12, uv, pytest + pytest-asyncio, ruff.

## Global Constraints

- Base URLs are baked class constants — never read from env or repo config (key-exfiltration vector).
- `_quota_markers = ()` for both engines: text markers are spoofable and neither billing model auto-resets (spec "Quota markers: none, deliberately").
- Engine names: `kimi`, `openrouter`. Secret env vars: `KIMI_API_KEY`, `OPENROUTER_API_KEY`. Defaults: `kimi-k3`, `openrouter/auto`.
- Both join `NATIVE_SKILLS_ENGINES` (claude harness discovers `.claude/skills` natively).
- Every new secret env var joins `_SECRET_ENV_VARS` in `src/themis/security.py`.
- Conventional Commits. `uv run pytest -q` and `uv run ruff check src tests` must pass before every commit.
- Run all commands from the worktree root: `/Users/wadii/projects/themis/.claude/worktrees/mossy-wandering-spindle`.

---

### Task 1: kimi engine

**Files:**
- Create: `src/themis/engines/kimi.py`
- Test: `tests/engines/test_kimi.py`

**Interfaces:**
- Consumes: `themis.engines.anthropic_api.AnthropicApiEngine` (existing).
- Produces: `class KimiEngine(AnthropicApiEngine)` with `name = "kimi"`, importable as `from themis.engines.kimi import KimiEngine`; constructor takes no arguments. Task 3 registers it.

- [ ] **Step 1: Write the failing tests**

Create `tests/engines/test_kimi.py`:

```python
import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.kimi import KimiEngine

pytestmark = pytest.mark.asyncio


def _fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "claude"
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def _run(workspace: Path, **overrides) -> str:
    kwargs = dict(
        prompt="review this", workspace=workspace,
        model="kimi-k3", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await KimiEngine().run(**kwargs)


async def test_run__env__key_mapped_and_endpoint_baked(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("KIMI_API_KEY", "kimi-key-123456")
    # A hostile/misconfigured host env must not redirect the provider key,
    # and sibling engine credentials must stay invisible.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.setenv("GLM_API_KEY", "glm-key-sibling")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert "ANTHROPIC_BASE_URL=https://api.moonshot.ai/anthropic" in env_dump
    assert "ANTHROPIC_AUTH_TOKEN=kimi-key-123456" in env_dump
    assert "API_TIMEOUT_MS=3000000" in env_dump
    # The raw key var, sibling keys, and the claude subscription token
    # never cross over.
    assert "KIMI_API_KEY" not in env_dump
    assert "GLM_API_KEY" not in env_dump
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dump
    assert "attacker.example" not in env_dump
    assert "host-leak" not in env_dump


async def test_run__argv__hardening_flags_and_model(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "kimi-k3" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args
    assert '--mcp-config {"mcpServers":{}}' in args
    assert "--disallowedTools WebFetch,WebSearch" in args


@pytest.mark.parametrize(
    "message",
    [
        # Moonshot balance/limit prose: retryable by design — no text quota
        # markers (spoofable; pay-as-you-go exhaustion never auto-resets, so
        # the "retry later" quota comment would mislead). See spec.
        "Your account balance is insufficient. Please top up.",
        "Rate limit reached for requests",
        "the retry limit exhausted while calling the API",
    ],
)
async def test_run__any_failure__is_retryable_engine_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineError) as exc_info:
        await _run(workspace)
    assert not isinstance(exc_info.value, EngineQuotaError)


def test_available__key_set__true(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "kimi-key-123456")

    assert KimiEngine().available() is True


def test_available__key_missing__false(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    assert KimiEngine().available() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engines/test_kimi.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'themis.engines.kimi'`

- [ ] **Step 3: Write the engine**

Create `src/themis/engines/kimi.py`:

```python
"""kimi engine: Claude Code harness on Moonshot's pay-as-you-go platform
endpoint."""

from themis.engines.anthropic_api import AnthropicApiEngine

# Deliberately NOT the Kimi Code subscription endpoint
# (https://api.kimi.com/coding/): its guidelines restrict subscriptions to
# "personal interactive use only" and name scripted/non-interactive use as
# a violation — exactly Themis's usage pattern. The platform key is
# pay-as-you-go and carries no such restriction.
#
# No text quota markers, same rationale as glm: markers match the
# agent-visible output tail and can be echoed by a prompt-steered agent,
# and pay-as-you-go exhaustion (insufficient balance) never auto-resets,
# so the "mention me later to retry" quota comment would mislead.
# Structured classification is #28.


class KimiEngine(AnthropicApiEngine):
    name = "kimi"
    _token_env = "KIMI_API_KEY"
    _base_url = "https://api.moonshot.ai/anthropic"
    _quota_markers = ()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engines/test_kimi.py -q`
Expected: all pass

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/themis/engines/kimi.py tests/engines/test_kimi.py
git commit -m "feat(engines): kimi engine on Moonshot's pay-as-you-go endpoint (#69)"
```

---

### Task 2: openrouter engine

**Files:**
- Create: `src/themis/engines/openrouter.py`
- Test: `tests/engines/test_openrouter.py`

**Interfaces:**
- Consumes: `themis.engines.anthropic_api.AnthropicApiEngine` (existing).
- Produces: `class OpenRouterEngine(AnthropicApiEngine)` with `name = "openrouter"`, importable as `from themis.engines.openrouter import OpenRouterEngine`; constructor takes no arguments. Task 3 registers it.

- [ ] **Step 1: Write the failing tests**

Create `tests/engines/test_openrouter.py`:

```python
import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.openrouter import OpenRouterEngine

pytestmark = pytest.mark.asyncio


def _fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "claude"
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def _run(workspace: Path, **overrides) -> str:
    kwargs = dict(
        prompt="review this", workspace=workspace,
        model="openrouter/auto", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await OpenRouterEngine().run(**kwargs)


async def test_run__env__key_mapped_and_endpoint_baked(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-123456")
    # A hostile/misconfigured host env must not redirect the provider key,
    # and sibling engine credentials must stay invisible.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.setenv("KIMI_API_KEY", "kimi-key-sibling")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert "ANTHROPIC_BASE_URL=https://openrouter.ai/api" in env_dump
    assert "ANTHROPIC_AUTH_TOKEN=or-key-123456" in env_dump
    assert "API_TIMEOUT_MS=3000000" in env_dump
    # The raw key var, sibling keys, and the claude subscription token
    # never cross over.
    assert "OPENROUTER_API_KEY" not in env_dump
    assert "KIMI_API_KEY" not in env_dump
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dump
    assert "attacker.example" not in env_dump
    assert "host-leak" not in env_dump


async def test_run__argv__hardening_flags_and_model(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "openrouter/auto" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args
    assert '--mcp-config {"mcpServers":{}}' in args
    assert "--disallowedTools WebFetch,WebSearch" in args


@pytest.mark.parametrize(
    "message",
    [
        # OpenRouter credit/limit prose: retryable by design — no text quota
        # markers (spoofable; a 402 out-of-credits never auto-resets, so the
        # "retry later" quota comment would mislead). See spec.
        "This request requires more credits. Please add credits.",
        "Rate limit exceeded, please slow down",
        "the retry limit exhausted while calling the API",
    ],
)
async def test_run__any_failure__is_retryable_engine_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineError) as exc_info:
        await _run(workspace)
    assert not isinstance(exc_info.value, EngineQuotaError)


def test_available__key_set__true(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-123456")

    assert OpenRouterEngine().available() is True


def test_available__key_missing__false(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert OpenRouterEngine().available() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engines/test_openrouter.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'themis.engines.openrouter'`

- [ ] **Step 3: Write the engine**

Create `src/themis/engines/openrouter.py`:

```python
"""openrouter engine: Claude Code harness on OpenRouter's Anthropic-protocol
gateway ("Anthropic Skin") — one prepaid key, many models via OpenRouter
slugs (moonshotai/kimi-k3, z-ai/glm-5.2, ...)."""

from themis.engines.anthropic_api import AnthropicApiEngine

# No text quota markers, same rationale as glm: markers match the
# agent-visible output tail and can be echoed by a prompt-steered agent,
# and running out of prepaid credits (402) never auto-resets, so the
# "mention me later to retry" quota comment would mislead. Structured
# classification is #28.


class OpenRouterEngine(AnthropicApiEngine):
    name = "openrouter"
    _token_env = "OPENROUTER_API_KEY"
    _base_url = "https://openrouter.ai/api"
    _quota_markers = ()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engines/test_openrouter.py -q`
Expected: all pass

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/themis/engines/openrouter.py tests/engines/test_openrouter.py
git commit -m "feat(engines): openrouter engine on the Anthropic Skin gateway (#69)"
```

---

### Task 3: registry + service wiring

**Files:**
- Modify: `src/themis/engines/__init__.py`
- Modify: `src/themis/review_service.py:62-66` (`DEFAULT_MODELS`), `:84-88` (`_ENGINE_AUTH_HINTS`)
- Test: `tests/engines/test_registry.py`, `tests/test_review_service.py`

**Interfaces:**
- Consumes: `KimiEngine` (Task 1), `OpenRouterEngine` (Task 2).
- Produces: `ENGINE_NAMES == ("codex", "claude", "glm", "kimi", "openrouter")`; `resolve("kimi")` / `resolve("openrouter")`; `NATIVE_SKILLS_ENGINES` including both; `DEFAULT_MODELS["kimi"] == "kimi-k3"`, `DEFAULT_MODELS["openrouter"] == "openrouter/auto"`; `_ENGINE_AUTH_HINTS` entries. Config validation (`config.py`) reads `ENGINE_NAMES` and needs no change.

- [ ] **Step 1: Update the registry tests (failing)**

In `tests/engines/test_registry.py`, add imports and replace/extend tests:

```python
from themis.engines.kimi import KimiEngine
from themis.engines.openrouter import OpenRouterEngine
```

Change `test_engine_names` to:

```python
def test_engine_names():
    assert ENGINE_NAMES == ("codex", "claude", "glm", "kimi", "openrouter")
```

Add after `test_resolve_glm`:

```python
def test_resolve_kimi():
    assert isinstance(resolve("kimi"), KimiEngine)


def test_resolve_openrouter():
    assert isinstance(resolve("openrouter"), OpenRouterEngine)
```

Change the assertion in `test_native_skills_engines_are_claude_harness_only` to:

```python
    assert NATIVE_SKILLS_ENGINES == frozenset({"claude", "glm", "kimi", "openrouter"})
```

Add to `tests/test_review_service.py` (top-level test, near the other module-level tests):

```python
def test_engine_tables_cover_all_engines():
    # Every registered engine must have a default model and an auth hint;
    # a gap surfaces as a KeyError mid-review otherwise.
    from themis.engines import ENGINE_NAMES
    from themis.review_service import _ENGINE_AUTH_HINTS, DEFAULT_MODELS

    assert set(DEFAULT_MODELS) == set(ENGINE_NAMES)
    assert set(_ENGINE_AUTH_HINTS) == set(ENGINE_NAMES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engines/test_registry.py tests/test_review_service.py::test_engine_tables_cover_all_engines -q`
Expected: FAIL — `ENGINE_NAMES` tuple mismatch, `resolve("kimi")` raises `ValueError`, table sets mismatch.

- [ ] **Step 3: Wire the registry**

In `src/themis/engines/__init__.py`:

```python
from themis.engines.base import Engine, EngineError, EngineQuotaError, EngineUnavailableError
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine
from themis.engines.glm import GlmEngine
from themis.engines.kimi import KimiEngine
from themis.engines.openrouter import OpenRouterEngine

ENGINE_NAMES = ("codex", "claude", "glm", "kimi", "openrouter")
# Engines with native skill discovery (the claude harness reads
# .claude/skills itself). Anything outside this set gets the skills
# bridge: a synthesized index of the base-revision skills (issue #49).
NATIVE_SKILLS_ENGINES = frozenset({"claude", "glm", "kimi", "openrouter"})
```

and extend `resolve()`:

```python
    if name == "kimi":
        return KimiEngine()
    if name == "openrouter":
        return OpenRouterEngine()
```

(placed after the `glm` branch, before the `raise`).

In `src/themis/review_service.py`, extend the tables:

```python
DEFAULT_MODELS = {
    "codex": "gpt-5.4",
    "claude": "claude-opus-4-6[1m]",
    "glm": "glm-5.2",
    "kimi": "kimi-k3",
    "openrouter": "openrouter/auto",
}
```

```python
_ENGINE_AUTH_HINTS = {
    "codex": "auth.json in CODEX_HOME",
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "glm": "GLM_API_KEY",
    "kimi": "KIMI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (config validation picks the names up from `ENGINE_NAMES` automatically).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/themis/engines/__init__.py src/themis/review_service.py tests/engines/test_registry.py tests/test_review_service.py
git commit -m "feat(engines): register kimi and openrouter engines (#69)"
```

---

### Task 4: outbound redaction for the new keys

**Files:**
- Modify: `src/themis/security.py:18-25` (`_SECRET_ENV_VARS`)
- Test: `tests/test_security.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent).
- Produces: `KIMI_API_KEY` and `OPENROUTER_API_KEY` values are scrubbed by `redact_outbound`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_security.py`, after `test_redact__glm_api_key`:

```python
def test_redact__kimi_api_key(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "kimi-key-abcdef123456")

    text = redact_outbound("keys: kimi-key-abcdef123456")

    assert "kimi-key-abcdef123456" not in text


def test_redact__openrouter_api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-abcdef123456")

    text = redact_outbound("keys: or-key-abcdef123456")

    assert "or-key-abcdef123456" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_security.py -q`
Expected: the two new tests FAIL (value not redacted).

- [ ] **Step 3: Extend the secret list**

In `src/themis/security.py`, `_SECRET_ENV_VARS` becomes:

```python
_SECRET_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "OPENROUTER_API_KEY",
    "THEMIS_GH_WEBHOOK_SECRET",
    "THEMIS_API_TOKEN",
    "THEMIS_GH_APP_PRIVATE_KEY",
    "THEMIS_AGENT_TOKEN",
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_security.py -q`
Expected: all pass

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/themis/security.py tests/test_security.py
git commit -m "feat(security): redact kimi and openrouter keys outbound (#69)"
```

---

### Task 5: bootstrap deployment templates

**Files:**
- Modify: `src/themis/bootstrap.py:224-225` (compose template, agent env), `:304-305` (generated `.env` lines)
- Test: `tests/test_bootstrap.py` (the `write_deployment` test asserting env/compose contents, ~lines 150-195)

**Interfaces:**
- Consumes: nothing from earlier tasks (independent; `--engine` already uses `choices=ENGINE_NAMES` from the registry, so Task 3 alone made the flag accept the new names).
- Produces: bootstrap-generated `.env` and `compose.yaml` carry `KIMI_API_KEY` / `OPENROUTER_API_KEY` passthroughs.

- [ ] **Step 1: Update the failing test**

In `tests/test_bootstrap.py`, in the test that asserts generated deployment contents:

After `assert "GLM_API_KEY=''" in env_text` add:

```python
    assert "KIMI_API_KEY=''" in env_text
    assert "OPENROUTER_API_KEY=''" in env_text
```

In the `compose["services"]["agent"]["environment"] == {...}` dict, after the `"GLM_API_KEY": "${GLM_API_KEY:-}",` entry add:

```python
        "KIMI_API_KEY": "${KIMI_API_KEY:-}",
        "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY:-}",
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bootstrap.py -q`
Expected: FAIL — generated `.env` lacks `KIMI_API_KEY=''`, compose env dict mismatch.

- [ ] **Step 3: Extend the templates**

In `src/themis/bootstrap.py` compose template (agent service environment, after the `GLM_API_KEY` line — note the doubled braces, it is an f-string template):

```python
      KIMI_API_KEY: ${{KIMI_API_KEY:-}}
      OPENROUTER_API_KEY: ${{OPENROUTER_API_KEY:-}}
```

In the `.env` lines list, after `"GLM_API_KEY=''",`:

```python
        "KIMI_API_KEY=''",
        "OPENROUTER_API_KEY=''",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bootstrap.py -q`
Expected: all pass

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/themis/bootstrap.py tests/test_bootstrap.py
git commit -m "feat(bootstrap): template kimi and openrouter keys into deployments (#69)"
```

---

### Task 6: deployment env + docs

**Files:**
- Modify: `docker-compose.yml` (agent service `environment:` block)
- Modify: `README.md`, `docs/configuration.md`, `docs/security.md`, `examples/themis/config.yaml`

No test cycle — docs and compose only. Keep wording consistent with the existing glm phrasing; the spec's billing-model note (OpenRouter = prepaid credits, not a subscription; kimi = pay-as-you-go, deliberately not the Kimi Code subscription per its ToS) must land in `docs/configuration.md`.

- [ ] **Step 1: docker-compose.yml**

In the `agent:` service `environment:` block, after `GLM_API_KEY: ${GLM_API_KEY:-}` add:

```yaml
      KIMI_API_KEY: ${KIMI_API_KEY:-}
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}
```

- [ ] **Step 2: docs/configuration.md**

- Env table row `THEMIS_ENGINE` (line ~20): value list becomes `` `codex`, `claude`, `glm`, `kimi`, or `openrouter` ``.
- After the `GLM_API_KEY` row (line ~25) add:

```markdown
| `KIMI_API_KEY` | agent only | unset | Moonshot pay-as-you-go platform key for the kimi engine; never set it on the controller. Deliberately not the Kimi Code subscription key — that plan's guidelines restrict it to personal interactive use, which excludes review bots |
| `OPENROUTER_API_KEY` | agent only | unset | OpenRouter key for the openrouter engine (prepaid credits, pay-per-token — not a subscription); never set it on the controller |
```

- Line ~38 (`CLAUDE_CODE_OAUTH_TOKEN` and `GLM_API_KEY` read directly by ...): extend the enumeration with `KIMI_API_KEY` and `OPENROUTER_API_KEY`.
- Sample config comment (line ~53): `# engine: codex            # codex | claude | glm | kimi | openrouter; unset = instance default (THEMIS_ENGINE)`.
- Sample config comment (line ~56): `# name: gpt-5.4          # unset = engine default (codex: gpt-5.4, claude: claude-opus-4-6[1m], glm: glm-5.2, kimi: kimi-k3, openrouter: openrouter/auto)`.
- Repo-config table `engine` row (~85): `` `codex`, `claude`, `glm`, `kimi`, or `openrouter`; an invalid value warns and falls back to the instance default ``.
- `web_access` row (~86): change "glm behaves like claude" to "glm/kimi/openrouter behave like claude" (keep the Bash-egress caveat sentence).
- `model.name` row (~87): append `, kimi-k3 for kimi, openrouter/auto for openrouter (any OpenRouter model slug, e.g. moonshotai/kimi-k3, works here)`.
- `model.reasoning_effort` row (~88): "ignored by claude/glm" → "ignored by the claude-harness engines (claude/glm/kimi/openrouter)".
- `agent.skills` row (~97) and line ~191: "claude/glm" → "claude/glm/kimi/openrouter" (native discovery set).

- [ ] **Step 3: README.md**

- Line ~24 (prereqs): after the glm sentence add: `The kimi and openrouter engines are the same: just KIMI_API_KEY (Moonshot platform, pay-as-you-go) or OPENROUTER_API_KEY (OpenRouter credits) in .env.`
- Lines ~83-85 (bootstrap): extend "Using glm?" to "Using glm, kimi, or openrouter? No CLI login needed: pass `--engine <name>` to the bootstrap and put the provider key in `.env`." (`bootstrap.py` already takes `choices=ENGINE_NAMES`, so the flag accepts the new names as of Task 3; Task 5 handled its templates).
- Line ~219-221 (env sample): `THEMIS_ENGINE=codex                    # or claude / glm / kimi / openrouter` and add `KIMI_API_KEY=<key>                     # kimi engine only` / `OPENROUTER_API_KEY=<key>               # openrouter engine only` after the GLM line.
- Repo-config table (~292-294): mirror the configuration.md wording changes (engine list, web_access, model.name defaults).
- Line ~305 (`agent.skills`): "natively on claude/glm" → "natively on claude/glm/kimi/openrouter".
- Engines table (~335), after the glm row:

```markdown
| `kimi` | one env var | set `KIMI_API_KEY` in `.env` (Moonshot pay-as-you-go platform key — not a Kimi Code subscription, whose terms exclude non-interactive use); reviews run through the claude CLI against Moonshot's Anthropic-compatible endpoint |
| `openrouter` | one env var | set `OPENROUTER_API_KEY` in `.env` (prepaid credits); reviews run through the claude CLI against OpenRouter's Anthropic-protocol gateway — `model.name` takes any OpenRouter slug |
```

- Line ~340: "The claude and glm paths need no volume" → "The claude, glm, kimi, and openrouter paths need no volume".
- Troubleshooting row (~352): extend the credential list with `KIMI_API_KEY` (kimi) and `OPENROUTER_API_KEY` (openrouter).

- [ ] **Step 4: docs/security.md**

Single-tenant paragraph (~line 147): extend the enumeration — after "`GLM_API_KEY` is one coding-plan subscription" add ", one `KIMI_API_KEY` / `OPENROUTER_API_KEY` is one provider account". Keep the rest of the paragraph unchanged.

- [ ] **Step 5: examples/themis/config.yaml**

- `# engine: codex            # codex | claude | glm | kimi | openrouter; unset = the instance's THEMIS_ENGINE`
- `# name: gpt-5.4          # unset = engine default (codex: gpt-5.4, claude: claude-opus-4-6[1m], glm: glm-5.2, kimi: kimi-k3, openrouter: openrouter/auto)`
- `reasoning_effort` comment: `low | medium | high (codex only; claude-harness engines ignore it)`
- `agent.skills` comment: `(claude/glm/kimi/openrouter engines; codex has no skills surface)` — adjust the existing parenthetical.

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest -q
uv run ruff check src tests
git add docker-compose.yml README.md docs/configuration.md docs/security.md examples/themis/config.yaml
git commit -m "docs: kimi and openrouter engine setup and reference (#69)"
```

---

### Task 7: final verification

- [ ] **Step 1: Full suite + lint**

```bash
uv run pytest -q
uv run ruff check src tests
```

Expected: both clean.

- [ ] **Step 2: Grep for missed glm-only enumerations**

```bash
grep -rn "claude, or glm\|claude / glm\|claude/glm" README.md docs/ examples/ src/ | grep -v "kimi"
```

Expected: no hits that enumerate engines without the new names (hits inside `docs/superpowers/` history files are fine).

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/kimi-openrouter-engines
gh pr create --title "feat: kimi and openrouter engines (#69)" --body "$(cat <<'EOF'
Adds two claude-harness engines per docs/superpowers/specs/2026-07-21-kimi-openrouter-engines-design.md:

- `kimi` — Moonshot pay-as-you-go platform endpoint (`KIMI_API_KEY`, default `kimi-k3`). Deliberately not the Kimi Code subscription: its guidelines restrict subscriptions to personal interactive use, which excludes review bots (same reason qwen was dropped in #20's round).
- `openrouter` — OpenRouter's Anthropic-protocol gateway (`OPENROUTER_API_KEY`, default `openrouter/auto`, any OpenRouter slug via `model.name`).

Both are declarative `AnthropicApiEngine` leaves: baked base URLs, key mapped to `ANTHROPIC_AUTH_TOKEN`, no quota markers (spoofable; neither billing model auto-resets), native skills discovery, outbound key redaction, compose + docs updated.

Closes #69.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01UfGLjjrzAeNgt3bELnFCvt
EOF
)"
```
