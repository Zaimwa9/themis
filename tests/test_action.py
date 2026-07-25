"""GitHub Actions entrypoint: one event, one synchronous job, static token."""

import json
import stat

import pytest

from themis.action import (
    DEFAULT_BOT_LOGIN,
    DEFAULT_MENTION,
    ActionError,
    action_settings,
    build_action_service,
    load_event,
    materialize_codex_auth,
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


# --- run_action ---------------------------------------------------------------


class FakeService:
    def __init__(self):
        self.calls: list[tuple] = []

    async def review(
        self, repo, pr_number, installation_id, auto,
        trigger_comment_id=None, extra_context=None,
    ):
        self.calls.append(
            ("review", repo, pr_number, installation_id, auto, trigger_comment_id)
        )

    async def discuss(self, **kwargs):
        self.calls.append(("discuss", kwargs))


@pytest.fixture
def fake_service(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(
        action_module, "build_action_service", lambda *args, **kwargs: service
    )
    return service


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


async def test_run_action__missing_token__raises(tmp_path, monkeypatch, fake_service):
    _write_event(tmp_path, monkeypatch, "pull_request", _pr_payload())

    with pytest.raises(ActionError):
        await run_action()
    assert fake_service.calls == []
