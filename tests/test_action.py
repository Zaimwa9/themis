"""GitHub Actions entrypoint: one event, one synchronous job, static token."""

import json
import stat

import pytest

from pathlib import Path

from themis.action import (
    DEFAULT_BOT_LOGIN,
    DEFAULT_MENTION,
    ActionError,
    action_settings,
    build_action_service,
    load_event,
    materialize_codex_auth,
    resolve_github_token,
    run_action,
)
from themis import action as action_module
from themis.config import SettingsError
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine
from themis.github.client import GitHubClient

REPO = "acme/widgets"

_ENV_KEYS = (
    "GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_TOKEN", "RUNNER_TEMP",
    "CODEX_HOME", "THEMIS_ENGINE", "THEMIS_MENTION", "THEMIS_BOT_LOGIN",
    "THEMIS_CODEX_SANDBOX", "THEMIS_CODEX_AUTH_JSON", "THEMIS_WORKSPACE_ROOT",
    "THEMIS_DEFAULT_REPO_CONFIG",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_event(tmp_path, monkeypatch, name: str, payload: dict) -> None:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))


def _pr_payload(action: str = "opened", draft: bool = False) -> dict:
    # Actions payloads carry no "installation" key: no GitHub App is involved.
    return {
        "action": action,
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7, "draft": draft},
    }


def _issue_comment_payload(body: str) -> dict:
    return {
        "action": "created",
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        "issue": {"number": 7, "pull_request": {"url": "https://x"}},
        "comment": {
            "id": 501, "body": body, "author_association": "OWNER",
            "user": {"login": "dev"},
        },
    }


def _review_comment_payload(body: str, in_reply_to: int | None = None) -> dict:
    comment = {"id": 601, "body": body, "user": {"login": "dev"}}
    if in_reply_to is not None:
        comment["in_reply_to_id"] = in_reply_to
    return {
        "action": "created",
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7},
        "comment": comment,
    }


# --- load_event ---------------------------------------------------------------


def test_load_event__reads_name_and_payload(tmp_path, monkeypatch):
    _write_event(tmp_path, monkeypatch, "pull_request", _pr_payload())

    name, payload = load_event()

    assert name == "pull_request"
    assert payload["repository"]["full_name"] == REPO


def test_load_event__missing_env__raises(monkeypatch):
    with pytest.raises(ActionError):
        load_event()


def test_load_event__invalid_json__raises(tmp_path, monkeypatch):
    path = tmp_path / "event.json"
    path.write_text("{not json")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))

    with pytest.raises(ActionError):
        load_event()


# --- action_settings ----------------------------------------------------------


def test_action_settings__defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))

    settings = action_settings()

    assert settings.engine == "claude"
    assert settings.workspace_root == tmp_path / "themis"
    assert settings.default_repo_config is None
    assert settings.webhook_enabled is False


def test_action_settings__engine_from_env(monkeypatch):
    monkeypatch.setenv("THEMIS_ENGINE", "codex")
    assert action_settings().engine == "codex"


def test_action_settings__invalid_engine__raises(monkeypatch):
    monkeypatch.setenv("THEMIS_ENGINE", "gpt-9000")
    with pytest.raises(SettingsError):
        action_settings()


def test_action_settings__invalid_sandbox__raises(monkeypatch):
    monkeypatch.setenv("THEMIS_CODEX_SANDBOX", "yolo")
    with pytest.raises(SettingsError):
        action_settings()


def test_action_settings__default_repo_config_parsed(monkeypatch):
    monkeypatch.setenv("THEMIS_DEFAULT_REPO_CONFIG", "engine: claude")
    assert action_settings().default_repo_config == "engine: claude"


def test_action_settings__workspace_root_without_runner_temp(monkeypatch):
    settings = action_settings()
    assert str(settings.workspace_root).endswith("themis")


# --- build_action_service -----------------------------------------------------


async def test_build_action_service__static_token_for_any_installation():
    service = build_action_service(
        action_settings(), "tok-123", DEFAULT_BOT_LOGIN, DEFAULT_MENTION
    )

    assert await service.get_token(0) == "tok-123"
    assert await service.get_token(424242) == "tok-123"


def test_build_action_service__in_process_engines():
    settings = action_settings()
    service = build_action_service(settings, "tok", DEFAULT_BOT_LOGIN, DEFAULT_MENTION)

    assert isinstance(service.resolve_engine("claude"), ClaudeEngine)
    assert isinstance(service.resolve_engine("codex"), CodexEngine)


def test_build_action_service__no_learning_service():
    service = build_action_service(
        action_settings(), "tok", DEFAULT_BOT_LOGIN, DEFAULT_MENTION
    )
    assert service.learning_service is None


def test_build_action_service__identity_and_client():
    service = build_action_service(action_settings(), "tok", "robo[bot]", "@robo")

    assert service.bot_login == "robo[bot]"
    assert service.mention == "@robo"
    assert service.make_client is GitHubClient


# --- materialize_codex_auth ---------------------------------------------------


def test_materialize_codex_auth__writes_private_auth_json(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("THEMIS_CODEX_AUTH_JSON", '{"OPENAI_API_KEY": "sk-test"}')

    materialize_codex_auth()

    import os
    codex_home = os.environ["CODEX_HOME"]
    auth = tmp_path / "codex-home" / "auth.json"
    assert str(auth.parent) == codex_home
    assert json.loads(auth.read_text()) == {"OPENAI_API_KEY": "sk-test"}
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    # The raw secret must not linger in the process env once on disk.
    assert "THEMIS_CODEX_AUTH_JSON" not in os.environ


def test_materialize_codex_auth__absent__noop(monkeypatch):
    import os
    materialize_codex_auth()
    assert "CODEX_HOME" not in os.environ


def test_materialize_codex_auth__existing_codex_home_wins(tmp_path, monkeypatch):
    home = tmp_path / "my-codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("THEMIS_CODEX_AUTH_JSON", '{"OPENAI_API_KEY": "sk-test"}')

    materialize_codex_auth()

    assert (home / "auth.json").is_file()


# --- resolve_github_token -----------------------------------------------------


def test_resolve_github_token__file_wins_and_is_consumed(tmp_path, monkeypatch):
    # The token reaches the entrypoint via a file so it never appears in the
    # exec-time environment of any live process the engine child could read
    # through /proc/<pid>/environ.
    token_file = tmp_path / "token"
    token_file.write_text("file-token-123456\n")
    monkeypatch.setenv("THEMIS_GITHUB_TOKEN_FILE", str(token_file))

    token = resolve_github_token()

    import os
    assert token == "file-token-123456"
    assert not token_file.exists()
    assert "THEMIS_GITHUB_TOKEN_FILE" not in os.environ
    assert "GITHUB_TOKEN" not in os.environ


def test_resolve_github_token__file_token_registered_for_redaction(
    tmp_path, monkeypatch
):
    from themis.security import redact_outbound
    token_file = tmp_path / "token"
    token_file.write_text("registered-token-9876")
    monkeypatch.setenv("THEMIS_GITHUB_TOKEN_FILE", str(token_file))

    resolve_github_token()

    assert "registered-token-9876" not in redact_outbound(
        "leak: registered-token-9876"
    )


def test_resolve_github_token__env_fallback_popped(monkeypatch):
    # Direct (non-action.yml) invocation: accept GITHUB_TOKEN but strip it
    # from this process's env before any engine can be spawned.
    import os
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-123456")

    token = resolve_github_token()

    assert token == "env-token-123456"
    assert "GITHUB_TOKEN" not in os.environ


def test_resolve_github_token__unreadable_file__raises(tmp_path, monkeypatch):
    monkeypatch.setenv("THEMIS_GITHUB_TOKEN_FILE", str(tmp_path / "absent"))
    with pytest.raises(ActionError):
        resolve_github_token()


def test_resolve_github_token__missing__raises():
    with pytest.raises(ActionError):
        resolve_github_token()


def test_action_yml__installs_both_engine_cli_families():
    # Regression for the round-3 review major: `.themis/config.yaml` may
    # override `engine:` per repo at runtime, so installing only the input
    # engine's CLI would leave a valid cross-family override without its
    # executable. Both families ship; credentials gate which engines are
    # actually available, exactly like server mode.
    import yaml
    spec = yaml.safe_load(
        (Path(__file__).parent.parent / "action.yml").read_text()
    )
    install_steps = [
        step for step in spec["runs"]["steps"]
        if "npm install" in (step.get("run") or "")
    ]
    assert len(install_steps) == 1
    run = install_steps[0]["run"]
    assert "@openai/codex" in run
    assert "@anthropic-ai/claude-code" in run
    assert "case" not in run  # no per-engine branching: both always install


def test_action_yml__engine_cli_installs_are_cached_by_resolved_version():
    # The npm installs are the biggest fixed cost of a run (~40-90s). They
    # are cached keyed on the *resolved* versions — never the raw input tag:
    # `latest` as a cache key would pin the first cached release forever,
    # while a resolved version naturally misses the cache when a new release
    # ships. The install step must be skipped entirely on a cache hit.
    import yaml
    spec = yaml.safe_load(
        (Path(__file__).parent.parent / "action.yml").read_text()
    )
    steps = spec["runs"]["steps"]

    [resolve] = [s for s in steps if "npm view" in (s.get("run") or "")]
    assert "GITHUB_OUTPUT" in resolve["run"]  # concrete versions as outputs
    assert "GITHUB_PATH" in resolve["run"]  # cached bin dir reaches engines

    [cache] = [s for s in steps if (s.get("uses") or "").startswith("actions/cache@")]
    key = cache["with"]["key"]
    assert "steps." in key and "outputs" in key  # keyed on resolved versions
    assert "inputs.engine-cli-version" not in key

    [install] = [s for s in steps if "npm install" in (s.get("run") or "")]
    assert "cache-hit" in install.get("if", "")


def test_action_yml__nested_actions_pinned_to_commit_shas():
    # Round-8 review blocker: the composite action runs with the posting
    # token and an engine credential in reach, so every third-party action
    # it pulls in must be pinned to an immutable commit SHA — a mutable tag
    # would let an upstream compromise ship straight into adopters' runs.
    # (The example workflow's own Zaimwa9/themis reference is pinned by a
    # follow-up once a released commit containing action.yml exists; no SHA
    # that predates the action can be used.)
    import re
    import yaml
    spec = yaml.safe_load(
        (Path(__file__).parent.parent / "action.yml").read_text()
    )
    uses = [s["uses"] for s in spec["runs"]["steps"] if s.get("uses")]
    assert uses  # contract is vacuous if the steps stop using actions
    for ref in uses:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), (
            f"{ref} is not pinned to a full commit SHA"
        )


def test_action_yml__run_step_never_receives_the_token_as_env():
    # Regression for the round-1 review blocker: GITHUB_TOKEN as env on the
    # run step would sit in the exec-time environment of the step's bash,
    # uv, and python ancestry for the whole engine run — readable via
    # /proc/<pid>/environ by the same-UID engine child.
    import yaml
    spec = yaml.safe_load(
        (Path(__file__).parent.parent / "action.yml").read_text()
    )
    run_steps = [
        step for step in spec["runs"]["steps"]
        if "python -m themis action" in (step.get("run") or "")
    ]
    assert len(run_steps) == 1
    env = run_steps[0].get("env") or {}
    assert "GITHUB_TOKEN" not in env
    assert "THEMIS_GITHUB_TOKEN_FILE" in env


# --- run_action ---------------------------------------------------------------


class FakeClient:
    def __init__(self, head_repo: dict | None):
        self._head_repo = head_repo
        self.comments: list[tuple[str, int, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def get_pr(self, repo, number):
        return {"number": number, "head": {"repo": self._head_repo}}

    async def post_issue_comment(self, repo, number, body):
        self.comments.append((repo, number, body))


class FakeService:
    def __init__(self, head_repo: dict | None):
        self.calls: list[tuple] = []
        self.client = FakeClient(head_repo)
        self.make_client = lambda token: self.client

    async def review(
        self, repo, pr_number, installation_id, auto,
        trigger_comment_id=None, extra_context=None,
    ):
        self.calls.append(
            ("review", repo, pr_number, installation_id, auto, trigger_comment_id)
        )

    async def discuss(self, **kwargs):
        self.calls.append(("discuss", kwargs))


def _install_fake_service(monkeypatch, head_repo: dict | None) -> FakeService:
    service = FakeService(head_repo)
    monkeypatch.setattr(
        action_module, "build_action_service", lambda *args, **kwargs: service
    )
    return service


@pytest.fixture
def fake_service(monkeypatch):
    return _install_fake_service(monkeypatch, {"full_name": REPO})


async def test_run_action__pr_opened__runs_auto_review(
    tmp_path, monkeypatch, fake_service
):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(tmp_path, monkeypatch, "pull_request", _pr_payload())

    outcome = await run_action()

    assert outcome == "review"
    assert fake_service.calls == [("review", REPO, 7, 0, True, None)]


async def test_run_action__draft_pr__skipped(tmp_path, monkeypatch, fake_service):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(tmp_path, monkeypatch, "pull_request", _pr_payload(draft=True))

    assert await run_action() == "skipped"
    assert fake_service.calls == []


async def test_run_action__unsupported_event__skipped(
    tmp_path, monkeypatch, fake_service
):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(tmp_path, monkeypatch, "push", {"repository": {"full_name": REPO}})

    assert await run_action() == "skipped"
    assert fake_service.calls == []


async def test_run_action__mention_review_command__explicit_review(
    tmp_path, monkeypatch, fake_service
):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(
        tmp_path, monkeypatch, "issue_comment",
        _issue_comment_payload(f"{DEFAULT_MENTION} review"),
    )

    outcome = await run_action()

    assert outcome == "review"
    assert fake_service.calls == [("review", REPO, 7, 0, False, 501)]


async def test_run_action__custom_mention__honored(
    tmp_path, monkeypatch, fake_service
):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("THEMIS_MENTION", "@robo")
    _write_event(
        tmp_path, monkeypatch, "issue_comment", _issue_comment_payload("@robo review")
    )

    assert await run_action() == "review"


async def test_run_action__thread_reply__discussion(
    tmp_path, monkeypatch, fake_service
):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(
        tmp_path, monkeypatch, "pull_request_review_comment",
        _review_comment_payload("are you sure about this?", in_reply_to=11),
    )

    outcome = await run_action()

    assert outcome == "discuss"
    [(kind, kwargs)] = fake_service.calls
    assert kind == "discuss"
    assert kwargs["kind"] == "thread"
    assert kwargs["comment_id"] == 601
    assert kwargs["in_reply_to_id"] == 11
    assert kwargs["mentions_bot"] is False


async def test_run_action__fork_pr_review_command__refused(tmp_path, monkeypatch):
    # issue_comment workflows run in the base-repo context WITH secrets even
    # for fork PRs, so the guard must reject them before any clone or engine
    # start: the engine would otherwise run attacker-controlled code with the
    # engine credential in reach.
    service = _install_fake_service(
        monkeypatch, {"full_name": "attacker/widgets"}
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(
        tmp_path, monkeypatch, "issue_comment",
        _issue_comment_payload(f"{DEFAULT_MENTION} review"),
    )

    outcome = await run_action()

    assert outcome == "skipped"
    assert service.calls == []
    [(repo, number, body)] = service.client.comments
    assert (repo, number) == (REPO, 7)
    assert "fork" in body


async def test_run_action__deleted_fork_head__refused(tmp_path, monkeypatch):
    # head.repo is null once a fork is deleted; provenance unknowable, so
    # the guard fails closed.
    service = _install_fake_service(monkeypatch, None)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(tmp_path, monkeypatch, "pull_request", _pr_payload())

    assert await run_action() == "skipped"
    assert service.calls == []


async def test_run_action__fork_thread_reply__refused_without_engine(
    tmp_path, monkeypatch
):
    service = _install_fake_service(
        monkeypatch, {"full_name": "attacker/widgets"}
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(
        tmp_path, monkeypatch, "pull_request_review_comment",
        _review_comment_payload("thoughts?", in_reply_to=11),
    )

    assert await run_action() == "skipped"
    assert service.calls == []


async def test_run_action__fork_refusal_comment_is_redacted(tmp_path, monkeypatch):
    # Every string that reaches GitHub passes redact_outbound — including
    # this courtesy comment, should it ever interpolate hostile or secret
    # content.
    service = _install_fake_service(
        monkeypatch, {"full_name": "attacker/widgets"}
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fork-secret")
    monkeypatch.setattr(
        action_module, "FORK_SKIPPED_COMMENT",
        "No review. Diagnostics: sk-ant-oat01-fork-secret",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    _write_event(
        tmp_path, monkeypatch, "issue_comment",
        _issue_comment_payload(f"{DEFAULT_MENTION} review"),
    )

    await run_action()

    [(_, _, body)] = service.client.comments
    assert "sk-ant-oat01-fork-secret" not in body
    assert "[redacted]" in body


def _example_workflow() -> dict:
    import yaml
    return yaml.safe_load(
        (
            Path(__file__).parent.parent
            / "examples" / "github-actions" / "themis-review.yml"
        ).read_text()
    )


def test_example_workflow__serializes_runs_per_pr():
    # Round-4 review major: without a per-PR concurrency group, an auto
    # review and a mention command (or two quick comments) run two engines
    # against the same PR in parallel.
    spec = _example_workflow()
    concurrency = spec.get("concurrency") or spec["jobs"]["themis"].get("concurrency")
    assert concurrency, "example workflow must declare a concurrency group"
    group = concurrency["group"]
    assert "number" in group  # keyed per PR, not per workflow
    assert concurrency.get("cancel-in-progress") is False  # queue, don't kill
    # Default queue depth is one pending run: a third event would silently
    # replace an earlier pending review request. queue: max keeps every
    # request, FIFO (round-5 review major).
    assert concurrency.get("queue") == "max"


def test_example_workflow__timeout_covers_default_retry_budget():
    # Derived from LimitsConfig so the sample (and the docs guidance that
    # quotes it) can never drift from the real defaults: the full retry
    # ladder plus setup/clone/posting headroom. An undersized cap kills the
    # job mid-retry and the pipeline's failure comment never posts.
    from themis.config import LimitsConfig
    limits = LimitsConfig()
    engine_budget_minutes = limits.max_attempts * limits.timeout_seconds / 60
    spec = _example_workflow()
    assert spec["jobs"]["themis"]["timeout-minutes"] >= engine_budget_minutes + 15


async def test_run_action__missing_token__raises(tmp_path, monkeypatch, fake_service):
    _write_event(tmp_path, monkeypatch, "pull_request", _pr_payload())

    with pytest.raises(ActionError):
        await run_action()
    assert fake_service.calls == []


def test_main__config_error__clean_nonzero_exit(monkeypatch):
    # A missing env or bad setting is an operator mistake: the workflow log
    # should show one message, not a traceback, and the run must still fail.
    with pytest.raises(SystemExit) as excinfo:
        action_module.main()
    assert excinfo.value.code not in (0, None)
    assert "GITHUB_EVENT_NAME" in str(excinfo.value.code)
