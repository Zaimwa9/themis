# GLM and Qwen Engines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `glm` and `qwen` review engines that reuse the hardened Claude Code harness in API mode against Z.ai's and DashScope's Anthropic-compatible endpoints (issues #20, #21).

**Architecture:** `ClaudeEngine` is generalized in place with class attributes (`name`, `_token_env`, `_quota_markers`) and an `_auth_env()` hook. A new `AnthropicApiEngine` subclass injects a baked-in `ANTHROPIC_BASE_URL` and maps the provider key to `ANTHROPIC_AUTH_TOKEN`. `GlmEngine` and `QwenEngine` are declarative leaf subclasses. Registry, service maps, redaction list, compose, and docs pick up the two new names.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio (fake-CLI shell scripts on PATH), uv, ruff.

**Spec:** `docs/superpowers/specs/2026-07-13-glm-qwen-engines-design.md`

## Global Constraints

- Work from the repo root of this worktree; branch `feat/glm-qwen-engines`. **Never push.**
- Run tests with `uv run pytest`, lint with `uv run ruff check .`.
- The `claude` engine's observable behavior must not change: `tests/engines/test_claude.py` passes **without modification** (the only allowed diff there is none).
- Baked endpoints (never env/config-controlled): glm `https://api.z.ai/api/anthropic`, qwen `https://coding-intl.dashscope.aliyuncs.com/apps/anthropic`.
- Secret env vars: `GLM_API_KEY`, `QWEN_API_KEY`. They cross into the child env **only** as `ANTHROPIC_AUTH_TOKEN`.
- Default models: glm `glm-5.2`, qwen `qwen3.7-plus`.
- Commit after each task with the trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Generalize ClaudeEngine in place

**Files:**
- Modify: `src/themis/engines/claude.py`
- Test: `tests/engines/test_claude.py` (run only, no edits)

**Interfaces:**
- Produces: `ClaudeEngine` with class attrs `name: str`, `_token_env: str`, `_quota_markers: tuple[str, ...]` and overridable method `_auth_env(self) -> dict[str, str]`. `available()` returns `bool(os.environ.get(self._token_env))`. `run()` signature unchanged.

- [ ] **Step 1: Refactor `claude.py`**

Replace the `ClaudeEngine` class (keep module docstring, imports, `_QUOTA_MARKERS`, `_HYGIENE_ENV`, and `build_command` exactly as they are; delete the now-unused `_EXTRA_ENV` line):

```python
class ClaudeEngine:
    """Also the base for API-mode engines (glm, qwen): subclasses override
    the class attributes and _auth_env() to target an Anthropic-compatible
    endpoint while keeping the hardened harness identical."""

    name = "claude"
    _token_env = "CLAUDE_CODE_OAUTH_TOKEN"
    _quota_markers = _QUOTA_MARKERS

    def available(self) -> bool:
        return bool(os.environ.get(self._token_env))

    def _auth_env(self) -> dict[str, str]:
        # Subscription mode: the OAuth token passes through under its own name.
        token = os.environ.get(self._token_env)
        return {self._token_env: token} if token else {}

    async def run(
        self, *, prompt: str, workspace: Path, model: str, effort: str,
        timeout: float, web_access: bool = False,
    ) -> str:
        # effort is accepted for protocol parity; the claude CLI has no
        # reasoning-effort flag.
        # CLAUDE_CONFIG_DIR also isolates ~/.claude.json and user plugins,
        # which setting-sources intentionally does not cover.
        with tempfile.TemporaryDirectory(prefix=f"themis-{self.name}-") as config_dir:
            env = allowlisted_env(frozenset()) | _HYGIENE_ENV | self._auth_env() | {
                "CLAUDE_CONFIG_DIR": config_dir,
            }
            return await run_cli(
                name=self.name,
                command=build_command(prompt, model, web_access),
                workspace=workspace,
                env=env,
                timeout=timeout,
                quota_markers=self._quota_markers,
            )
```

- [ ] **Step 2: Verify the refactor is behavior-neutral**

Run: `uv run pytest tests/engines/test_claude.py -q`
Expected: all pass, zero test-file changes (`git status tests/` clean).

- [ ] **Step 3: Commit**

```bash
git add src/themis/engines/claude.py
git commit -m "refactor: parameterize ClaudeEngine for API-mode subclasses (#20, #21)"
```

---

### Task 2: AnthropicApiEngine base + GlmEngine

**Files:**
- Create: `src/themis/engines/anthropic_api.py`
- Create: `src/themis/engines/glm.py`
- Create: `tests/engines/test_glm.py`

**Interfaces:**
- Consumes: `ClaudeEngine` from Task 1 (`_token_env`, `_quota_markers`, `_auth_env()` hook).
- Produces: `AnthropicApiEngine(ClaudeEngine)` in `anthropic_api.py` with class attr `_base_url: str`; `GlmEngine(AnthropicApiEngine)` in `glm.py` with `name = "glm"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/engines/test_glm.py`:

```python
import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.glm import GlmEngine

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
        model="glm-5.2", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await GlmEngine().run(**kwargs)


async def test_run__env__key_mapped_and_endpoint_baked(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("GLM_API_KEY", "glm-key-123456")
    # A hostile/misconfigured host env must not redirect the provider key.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert "ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic" in env_dump
    assert "ANTHROPIC_AUTH_TOKEN=glm-key-123456" in env_dump
    assert "API_TIMEOUT_MS=3000000" in env_dump
    # The raw key var and the claude subscription token never cross over.
    assert "GLM_API_KEY" not in env_dump
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dump
    assert "attacker.example" not in env_dump
    assert "host-leak" not in env_dump


async def test_run__argv__hardening_flags_and_model(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "glm-5.2" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args
    assert '--mcp-config {"mcpServers":{}}' in args
    assert "--disallowedTools WebFetch,WebSearch" in args


async def test_run__config_dir__isolated(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    config_dir = next(
        line.removeprefix("CLAUDE_CONFIG_DIR=")
        for line in env_dump.splitlines()
        if line.startswith("CLAUDE_CONFIG_DIR=")
    )
    assert config_dir != os.path.expanduser("~/.claude")


@pytest.mark.parametrize(
    "message",
    [
        "Usage limit reached for the past 5 hours. Resets at 18:00.",
        "Weekly/Monthly Limit Exhausted. Your limit will reset at Monday.",
        "Your GLM Coding Plan package has expired.",
    ],
)
async def test_run__plan_exhausted__raises_quota_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineQuotaError):
        await _run(workspace)


async def test_run__transient_rate_limit__is_retryable_engine_error(
    tmp_path, monkeypatch, workspace
):
    # Z.ai code 1302 wording; must stay retryable.
    _fake_cli(tmp_path, monkeypatch, 'echo "Rate limit reached for requests"; exit 1')

    with pytest.raises(EngineError) as exc_info:
        await _run(workspace)
    assert not isinstance(exc_info.value, EngineQuotaError)


def test_available__key_set__true(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "glm-key-123456")

    assert GlmEngine().available() is True


def test_available__key_missing__false(monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    assert GlmEngine().available() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engines/test_glm.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'themis.engines.glm'`

- [ ] **Step 3: Implement**

Create `src/themis/engines/anthropic_api.py`:

```python
"""Claude Code harness in API mode against third-party Anthropic-compatible
endpoints (Z.ai, DashScope). Same binary and hardening as the claude engine;
only the auth env differs."""

import os

from themis.engines.claude import ClaudeEngine


class AnthropicApiEngine(ClaudeEngine):
    """The endpoint is baked into the subclass, never read from env or repo
    config: a controllable base URL would redirect the provider key to an
    attacker host."""

    _base_url: str

    def _auth_env(self) -> dict[str, str]:
        env = {
            "ANTHROPIC_BASE_URL": self._base_url,
            # Providers recommend a generous request timeout for long agentic
            # turns; run_cli's wall clock still bounds the whole attempt.
            "API_TIMEOUT_MS": "3000000",
        }
        token = os.environ.get(self._token_env)
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
        return env
```

Create `src/themis/engines/glm.py`:

```python
"""glm engine: Claude Code harness on Z.ai's GLM Coding Plan endpoint."""

from themis.engines.anthropic_api import AnthropicApiEngine

# Z.ai exhausted-plan diagnostics (error codes 1308-1310, 1316-1321):
# "Usage limit reached for ...", "Weekly/Monthly Limit Exhausted",
# "Your GLM Coding Plan package has expired". Transient throttling (1302)
# says "Rate limit reached for requests", which none of these match; it
# must remain a retryable EngineError.
_QUOTA_MARKERS = (
    "usage limit reached for",
    "limit exhausted",
    "coding plan package has expired",
)


class GlmEngine(AnthropicApiEngine):
    name = "glm"
    _token_env = "GLM_API_KEY"
    _base_url = "https://api.z.ai/api/anthropic"
    _quota_markers = _QUOTA_MARKERS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engines/ -q`
Expected: all pass (glm + claude + codex + registry).

- [ ] **Step 5: Commit**

```bash
git add src/themis/engines/anthropic_api.py src/themis/engines/glm.py tests/engines/test_glm.py
git commit -m "feat: glm engine via Claude Code API mode (#20)"
```

---

### Task 3: QwenEngine

**Files:**
- Create: `src/themis/engines/qwen.py`
- Create: `tests/engines/test_qwen.py`

**Interfaces:**
- Consumes: `AnthropicApiEngine` from Task 2.
- Produces: `QwenEngine(AnthropicApiEngine)` with `name = "qwen"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/engines/test_qwen.py`:

```python
import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.qwen import QwenEngine

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
        model="qwen3.7-plus", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await QwenEngine().run(**kwargs)


async def test_run__env__key_mapped_and_endpoint_baked(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("QWEN_API_KEY", "sk-sp-fake123456")
    monkeypatch.setenv("GLM_API_KEY", "glm-key-123456")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert (
        "ANTHROPIC_BASE_URL=https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"
        in env_dump
    )
    assert "ANTHROPIC_AUTH_TOKEN=sk-sp-fake123456" in env_dump
    # Sibling engines' credentials never cross over.
    assert "QWEN_API_KEY" not in env_dump
    assert "GLM_API_KEY" not in env_dump
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dump


async def test_run__argv__hardening_flags_and_model(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "qwen3.7-plus" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args


@pytest.mark.parametrize(
    "message",
    [
        "hour allocated quota exceeded",
        "week allocated quota exceeded",
        "month allocated quota exceeded",
    ],
)
async def test_run__plan_exhausted__raises_quota_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineQuotaError):
        await _run(workspace)


@pytest.mark.parametrize(
    "message",
    [
        # Documented as retryable: the platform adjusts concurrency dynamically.
        "concurrency allocated quota exceeded",
        # Throttling.* family resolves in ~60s.
        "Requests rate limit exceeded, please try again later.",
        # Billing arrears never auto-reset; the quota comment ("mention the
        # bot once it resets") would mislead, so it stays a plain failure.
        "Access denied, please make sure your account is in good standing.",
    ],
)
async def test_run__transient_or_billing__is_plain_engine_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineError) as exc_info:
        await _run(workspace)
    assert not isinstance(exc_info.value, EngineQuotaError)


def test_available__key_set__true(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-sp-fake123456")

    assert QwenEngine().available() is True


def test_available__key_missing__false(monkeypatch):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)

    assert QwenEngine().available() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engines/test_qwen.py -q`
Expected: `ModuleNotFoundError: No module named 'themis.engines.qwen'`

- [ ] **Step 3: Implement**

Create `src/themis/engines/qwen.py`:

```python
"""qwen engine: Claude Code harness on DashScope's Qwen Coding Plan endpoint
(international). Mainland and pay-as-you-go endpoints are out of scope; if
needed later, add an allowlisted region switch, never a free-form URL."""

from themis.engines.anthropic_api import AnthropicApiEngine

# DashScope Coding Plan exhaustion strings (hour/week/month windows). The
# similarly worded "concurrency allocated quota exceeded" is documented as
# retryable and must NOT match, hence the window-qualified markers. Billing
# arrears ("Arrearage", bill overdue) never auto-reset, so they stay plain
# EngineErrors rather than quota errors.
_QUOTA_MARKERS = (
    "hour allocated quota exceeded",
    "week allocated quota exceeded",
    "month allocated quota exceeded",
)


class QwenEngine(AnthropicApiEngine):
    name = "qwen"
    _token_env = "QWEN_API_KEY"
    _base_url = "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"
    _quota_markers = _QUOTA_MARKERS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engines/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/themis/engines/qwen.py tests/engines/test_qwen.py
git commit -m "feat: qwen engine via Claude Code API mode (#21)"
```

---

### Task 4: Registry, service maps, redaction

**Files:**
- Modify: `src/themis/engines/__init__.py`
- Modify: `src/themis/service.py:49` (`DEFAULT_MODELS`) and `:67-70` (`_ENGINE_AUTH_HINTS`)
- Modify: `src/themis/security.py:18-24` (`_SECRET_ENV_VARS`)
- Modify: `tests/engines/test_registry.py`
- Modify: `tests/test_service.py` (append one test)
- Modify: `tests/test_security.py` (append one test)

**Interfaces:**
- Consumes: `GlmEngine`, `QwenEngine` from Tasks 2–3.
- Produces: `ENGINE_NAMES == ("codex", "claude", "glm", "qwen")`; `resolve("glm")` / `resolve("qwen")` return the new engines. `config.py` needs no change (it validates against `ENGINE_NAMES`).

- [ ] **Step 1: Write the failing tests**

In `tests/engines/test_registry.py`, update the imports and the names test, and add resolve tests:

```python
from themis.engines.glm import GlmEngine
from themis.engines.qwen import QwenEngine


def test_engine_names():
    assert ENGINE_NAMES == ("codex", "claude", "glm", "qwen")


def test_resolve_glm():
    assert isinstance(resolve("glm"), GlmEngine)


def test_resolve_qwen():
    assert isinstance(resolve("qwen"), QwenEngine)
```

Append to `tests/test_service.py` (import `ENGINE_NAMES` from `themis.engines`, plus `DEFAULT_MODELS` and `_ENGINE_AUTH_HINTS` from `themis.service`, following the file's existing import style):

```python
def test_engine_maps_cover_all_engine_names():
    # A registered engine without a default model or auth hint is a KeyError
    # at review time; keep the three maps in lockstep.
    assert set(DEFAULT_MODELS) == set(ENGINE_NAMES)
    assert set(_ENGINE_AUTH_HINTS) == set(ENGINE_NAMES)
```

Append to `tests/test_security.py` (match its existing redact tests' style):

```python
def test_redact_outbound__provider_api_keys(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "glm-key-abcdef123456")
    monkeypatch.setenv("QWEN_API_KEY", "sk-sp-abcdef123456")

    text = redact_outbound("keys: glm-key-abcdef123456 sk-sp-abcdef123456")

    assert "glm-key-abcdef123456" not in text
    assert "sk-sp-abcdef123456" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engines/test_registry.py tests/test_service.py tests/test_security.py -q`
Expected: the new tests fail (`ENGINE_NAMES` mismatch, `ValueError: unknown engine`, map-coverage assertion, unredacted keys).

- [ ] **Step 3: Implement**

`src/themis/engines/__init__.py` — add imports and branches:

```python
from themis.engines.glm import GlmEngine
from themis.engines.qwen import QwenEngine

ENGINE_NAMES = ("codex", "claude", "glm", "qwen")
```

and in `resolve()`:

```python
    if name == "glm":
        return GlmEngine()
    if name == "qwen":
        return QwenEngine()
```

`src/themis/service.py`:

```python
DEFAULT_MODELS = {
    "codex": "gpt-5.4",
    "claude": "claude-opus-4-6[1m]",
    "glm": "glm-5.2",
    "qwen": "qwen3.7-plus",
}
```

```python
_ENGINE_AUTH_HINTS = {
    "codex": "auth.json in CODEX_HOME",
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "glm": "GLM_API_KEY",
    "qwen": "QWEN_API_KEY",
}
```

`src/themis/security.py` — extend `_SECRET_ENV_VARS`:

```python
_SECRET_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GLM_API_KEY",
    "QWEN_API_KEY",
    "THEMIS_GH_WEBHOOK_SECRET",
    "THEMIS_API_TOKEN",
    "THEMIS_GH_APP_PRIVATE_KEY",
    "THEMIS_AGENT_TOKEN",
)
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (config engine-validation tests pick the new names up from `ENGINE_NAMES` automatically).

- [ ] **Step 5: Commit**

```bash
git add src/themis/engines/__init__.py src/themis/service.py src/themis/security.py tests/engines/test_registry.py tests/test_service.py tests/test_security.py
git commit -m "feat: register glm and qwen engines; redact provider keys (#20, #21)"
```

---

### Task 5: Deployment surface + docs

**Files:**
- Modify: `docker-compose.yml` (agent service env)
- Modify: `README.md` (intro line 15, prereqs line 23, Engines section, repo-config table, troubleshooting rows)
- Modify: `docs/configuration.md` (env table, yaml sample, key table)
- Modify: `docs/security.md` (allowlist bullet, secret-reachability paragraph, single-tenant note)
- Modify: `examples/themis/config.yaml` (comments)

**Interfaces:** none (docs/config only). Keep terminology: engines are `codex`, `claude`, `glm`, `qwen`; secrets are `GLM_API_KEY` / `QWEN_API_KEY`.

- [ ] **Step 1: docker-compose.yml** — in the `agent` service environment, after `CLAUDE_CODE_OAUTH_TOKEN`:

```yaml
      GLM_API_KEY: ${GLM_API_KEY:-}
      QWEN_API_KEY: ${QWEN_API_KEY:-}
```

- [ ] **Step 2: README.md**

Intro (line ~15): change "runs the configured engine (`codex exec` or `claude -p`)" to "runs the configured engine (`codex exec`, or `claude -p` — natively or in API mode for GLM/Qwen)".

Prereqs (line ~23): after the Codex/Claude sentence, add: "GLM and Qwen engines need no local CLI login: just a `GLM_API_KEY` (Z.ai GLM Coding Plan) or `QWEN_API_KEY` (Alibaba Model Studio Qwen Coding Plan, international) in `.env`."

Engines section: change "one of two agent CLIs, using your Codex or Claude Max subscription" to "an agent CLI, using your Codex, Claude Max, GLM or Qwen Coding Plan subscription" and extend the table:

```markdown
| `glm` | one env var | set `GLM_API_KEY` in `.env` (Z.ai GLM Coding Plan key); reviews run through the claude CLI against Z.ai's Anthropic-compatible endpoint |
| `qwen` | one env var | set `QWEN_API_KEY` in `.env` (Qwen Coding Plan key, international endpoint); reviews run through the claude CLI against DashScope's Anthropic-compatible endpoint |
```

Below the table, update "with `engine: claude` or `engine: codex`" to "with `engine:` set to any of them".

Repo-config table: `model.name` row becomes "engine default: `gpt-5.4` (codex), `claude-opus-4-6[1m]` (claude), `glm-5.2` (glm), `qwen3.7-plus` (qwen)"; `engine` row's value list becomes "`codex`, `claude`, `glm`, or `qwen`".

Troubleshooting: in the credentials row, change the fix to "Set `CLAUDE_CODE_OAUTH_TOKEN` (claude), `GLM_API_KEY` (glm), `QWEN_API_KEY` (qwen), or seed the codex auth volume (codex)…"; in the usage-limit row change "Your Codex or Claude subscription" to "The subscription of whichever engine ran the job".

- [ ] **Step 3: docs/configuration.md**

Env table: `THEMIS_ENGINE` row value list becomes "`codex`, `claude`, `glm`, or `qwen`". After the `CLAUDE_CODE_OAUTH_TOKEN` row add:

```markdown
| `GLM_API_KEY` | agent only | unset | Z.ai GLM Coding Plan key for the glm engine; never set it on the controller |
| `QWEN_API_KEY` | agent only | unset | Qwen Coding Plan key (international) for the qwen engine; never set it on the controller |
```

Update the paragraph under the table: "`CLAUDE_CODE_OAUTH_TOKEN` (read directly by …)" becomes "`CLAUDE_CODE_OAUTH_TOKEN`, `GLM_API_KEY`, and `QWEN_API_KEY` (read directly by the engine adapters in `../src/themis/engines/`, not part of `Settings`)".

YAML sample comments: `# engine: codex` line becomes `# engine: codex            # codex | claude | glm | qwen; unset = instance default (THEMIS_ENGINE)`; `# name:` line becomes `# name: gpt-5.4          # unset = engine default (codex: gpt-5.4, claude: claude-opus-4-6[1m], glm: glm-5.2, qwen: qwen3.7-plus)`; `reasoning_effort` comment becomes `low | medium | high (codex only; claude-harness engines ignore it)`.

Key table: `engine` row values "`codex`, `claude`, `glm`, or `qwen`"; `model.name` row lists all four defaults; `model.reasoning_effort` row "codex only, ignored by claude/glm/qwen"; `web_access` row: note glm/qwen behave like claude (`WebFetch`/`WebSearch`).

- [ ] **Step 4: docs/security.md**

Allowlist bullet: "…plus `CODEX_HOME` for codex or `CLAUDE_CODE_OAUTH_TOKEN` for claude" becomes "…plus `CODEX_HOME` for codex, `CLAUDE_CODE_OAUTH_TOKEN` for claude, or the provider key for glm/qwen (crossing over only as `ANTHROPIC_AUTH_TOKEN`, with the endpoint baked into the adapter so no env or repo config can redirect it)".

Secret-reachability paragraph: after "claude sees `CLAUDE_CODE_OAUTH_TOKEN` plus non-secret hygiene flags", add "; glm and qwen see their provider key as `ANTHROPIC_AUTH_TOKEN` plus the same hygiene flags".

"Claude engine sandbox posture": rename heading to "Claude-harness engine sandbox posture (claude, glm, qwen)" and note the posture applies to all three; the reachable secret is the engine's own key, all on the outbound-redaction list.

Single-tenant section: extend "one `CLAUDE_CODE_OAUTH_TOKEN` value is one Claude token" with "and one `GLM_API_KEY`/`QWEN_API_KEY` is one coding-plan subscription".

- [ ] **Step 5: examples/themis/config.yaml** — update the two comment lines:

```yaml
# engine: codex            # codex | claude | glm | qwen; unset = the instance's THEMIS_ENGINE
```

```yaml
  # name: gpt-5.4          # unset = engine default (codex: gpt-5.4, claude: claude-opus-4-6[1m], glm: glm-5.2, qwen: qwen3.7-plus)
  reasoning_effort: high   # low | medium | high (codex only; claude/glm/qwen ignore it)
```

- [ ] **Step 6: Verify and commit**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass, no lint errors.

```bash
git add docker-compose.yml README.md docs/configuration.md docs/security.md examples/themis/config.yaml
git commit -m "docs: glm and qwen engine setup, config, and security posture (#20, #21)"
```

---

### Task 6: Final verification

- [ ] **Step 1:** `uv run pytest -q` — full suite green.
- [ ] **Step 2:** `uv run ruff check .` — clean.
- [ ] **Step 3:** `git log --oneline origin/main..HEAD` — commits present, nothing pushed.
