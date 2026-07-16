"""Instance Settings (env) and per-repo RepoConfig parsing."""

import base64
import logging

import pytest
import yaml

from themis.config import (
    MODULE_NAMES,
    RepoConfig,
    SettingsError,
    load_settings,
    parse_repo_config,
    resolve_modules,
    skip_title_match,
)

REQUIRED = {
    "THEMIS_GH_APP_CLIENT_ID": "Iv1.abc",
    "THEMIS_GH_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    "THEMIS_GH_WEBHOOK_SECRET": "hush",
    "THEMIS_AGENT_TOKEN": "agent-secret",
}


def _set_env(monkeypatch, extra=None, omit=()):
    for key in (
        "THEMIS_GH_APP_CLIENT_ID", "THEMIS_GH_APP_PRIVATE_KEY", "THEMIS_GH_WEBHOOK_SECRET",
        "THEMIS_CODEX_SANDBOX", "THEMIS_PUBLIC_URL", "THEMIS_TUNNEL_API",
        "THEMIS_WEBHOOK_ENABLED", "THEMIS_API_TOKEN", "THEMIS_WORKSPACE_ROOT", "THEMIS_ENGINE",
        "THEMIS_AGENT_URL", "THEMIS_AGENT_TOKEN", "THEMIS_DATA_ROOT",
        "THEMIS_DEFAULT_REPO_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REQUIRED, **(extra or {})}.items():
        if key not in omit:
            monkeypatch.setenv(key, value)


def test_load_settings_happy_path(monkeypatch):
    _set_env(monkeypatch)
    settings = load_settings()
    assert settings.gh_app_client_id == "Iv1.abc"
    assert settings.gh_app_private_key_pem.startswith("-----BEGIN")
    assert settings.webhook_enabled is True
    assert settings.api_token is None
    assert settings.codex_sandbox == "workspace-write"
    assert str(settings.workspace_root) == "/tmp/themis"
    assert settings.agent_url == "http://agent:8001"


def test_load_settings_missing_required_names_them(monkeypatch):
    _set_env(monkeypatch, omit=("THEMIS_GH_APP_CLIENT_ID",))
    with pytest.raises(SettingsError, match="THEMIS_GH_APP_CLIENT_ID"):
        load_settings()


def test_load_settings_base64_key_decoded(monkeypatch):
    pem = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
    _set_env(monkeypatch, extra={
        "THEMIS_GH_APP_PRIVATE_KEY": base64.b64encode(pem.encode()).decode(),
    })
    assert load_settings().gh_app_private_key_pem == pem


def test_load_settings_garbage_key_rejected(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_GH_APP_PRIVATE_KEY": "not pem not base64 !!!"})
    with pytest.raises(SettingsError, match="PEM"):
        load_settings()


def test_webhook_disabled_needs_no_secret_but_needs_api_token(monkeypatch):
    _set_env(monkeypatch, extra={
        "THEMIS_WEBHOOK_ENABLED": "false", "THEMIS_API_TOKEN": "tok",
    }, omit=("THEMIS_GH_WEBHOOK_SECRET",))
    settings = load_settings()
    assert settings.webhook_enabled is False
    assert settings.gh_webhook_secret is None
    assert settings.api_token == "tok"


def test_no_entrypoint_at_all_is_an_error(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_WEBHOOK_ENABLED": "false"},
             omit=("THEMIS_GH_WEBHOOK_SECRET",))
    with pytest.raises(SettingsError, match="entrypoint"):
        load_settings()


def test_webhook_enabled_requires_secret(monkeypatch):
    _set_env(monkeypatch, omit=("THEMIS_GH_WEBHOOK_SECRET",))
    with pytest.raises(SettingsError, match="THEMIS_GH_WEBHOOK_SECRET"):
        load_settings()


def test_blank_workspace_root_env_falls_back_to_default(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_WORKSPACE_ROOT": ""})
    assert str(load_settings().workspace_root) == "/tmp/themis"


def test_invalid_sandbox_rejected(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_CODEX_SANDBOX": "yolo"})
    with pytest.raises(SettingsError, match="sandbox"):
        load_settings()


def test_public_url_trailing_slash_stripped(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_PUBLIC_URL": "https://x.example.com/"})
    assert load_settings().public_url == "https://x.example.com"


# --- RepoConfig ------------------------------------------------------------

def test_repo_config_defaults():
    config = parse_repo_config(None)
    assert config.model.name is None
    assert config.model.reasoning_effort == "high"
    assert config.limits.timeout_seconds == 1200
    assert config.limits.max_attempts == 2
    assert config.limits.clone_depth == 50
    assert config.triggers.auto_review is True


def test_repo_config_partial_deep_merges():
    config = parse_repo_config("model:\n  name: gpt-6\nlimits:\n  clone_depth: 10\n")
    assert config.model.name == "gpt-6"
    assert config.model.reasoning_effort == "high"   # untouched default
    assert config.limits.clone_depth == 10
    assert config.limits.timeout_seconds == 1200     # untouched default


def test_repo_config_malformed_yaml_falls_back_to_defaults():
    assert parse_repo_config("model: [unclosed") == RepoConfig()


def test_repo_config_wrong_shape_falls_back_to_defaults():
    assert parse_repo_config("- just\n- a list\n") == RepoConfig()
    assert parse_repo_config("model:\n  name: [1, 2]\n") == RepoConfig()


def test_repo_config_empty_file_is_defaults():
    assert parse_repo_config("") == RepoConfig()


# --- triggers.skip_titles ----------------------------------------------------


def test_repo_config__skip_titles_default_empty():
    assert parse_repo_config(None).triggers.skip_titles == ()


def test_repo_config__skip_titles_parsed():
    text = "triggers:\n  skip_titles:\n    - 'ci: *'\n    - 'chore: *'\n"
    assert parse_repo_config(text).triggers.skip_titles == ("ci: *", "chore: *")


def test_repo_config__skip_titles_single_string_coerced():
    text = "triggers:\n  skip_titles: 'ci: *'\n"
    assert parse_repo_config(text).triggers.skip_titles == ("ci: *",)


def test_repo_config__skip_titles_empty_entries_dropped(caplog):
    """An empty (or whitespace-only) pattern can never be intent; keeping it
    would be at best noise, and must not survive validation silently."""
    text = "triggers:\n  skip_titles:\n    - ''\n    - '   '\n    - 'ci: *'\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.triggers.skip_titles == ("ci: *",)
    assert "themis_invalid_skip_title" in caplog.text


def test_repo_config__skip_titles_non_string_entries_dropped(caplog):
    text = "triggers:\n  skip_titles:\n    - 3\n    - 'ci: *'\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.triggers.skip_titles == ("ci: *",)
    assert "themis_invalid_skip_title" in caplog.text


def test_repo_config__skip_titles_overlong_entry_dropped(caplog):
    text = f"triggers:\n  skip_titles:\n    - '{'x' * 300}'\n    - 'ci: *'\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.triggers.skip_titles == ("ci: *",)
    assert "themis_invalid_skip_title" in caplog.text


def test_repo_config__skip_titles_surrounding_whitespace_trimmed():
    """yaml flow style and block scalars easily smuggle stray spaces or a
    trailing newline into a pattern; under whole-title matching those would
    make the filter silently never fire."""
    text = "triggers:\n  skip_titles:\n    - ' ci: * '\n"
    assert parse_repo_config(text).triggers.skip_titles == ("ci: *",)


def test_repo_config__skip_titles_cap_counts_only_usable_entries(caplog):
    """Invalid entries must not consume cap slots: filtering runs before the
    50-pattern cap, so valid patterns past a run of garbage survive."""
    entries = ["", 42] + [f"p{i}: *" for i in range(51)]
    text = yaml.safe_dump({"triggers": {"skip_titles": entries}})
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert len(config.triggers.skip_titles) == 50
    assert config.triggers.skip_titles[0] == "p0: *"
    assert config.triggers.skip_titles[-1] == "p49: *"
    assert "themis_skip_titles_truncated" in caplog.text


def test_repo_config__skip_titles_wrong_shape_keeps_rest_of_triggers(caplog):
    text = "triggers:\n  skip_titles:\n    nested: mapping\n  auto_review: false\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.triggers.skip_titles == ()
    assert config.triggers.auto_review is False
    assert "themis_invalid_skip_title" in caplog.text


def test_repo_config__triggers_wrong_shape_keeps_rest_of_config(caplog):
    text = "triggers: nope\nlimits:\n  max_attempts: 5\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.triggers.auto_review is True
    assert config.limits.max_attempts == 5
    assert "themis_invalid_triggers_config" in caplog.text


def test_repo_config__auto_review_invalid_degrades_to_default(caplog):
    text = "triggers:\n  auto_review: banana\n  skip_titles:\n    - 'ci: *'\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.triggers.auto_review is True
    assert config.triggers.skip_titles == ("ci: *",)
    assert "themis_invalid_auto_review" in caplog.text


def test_repo_config__auto_review_bare_key_is_default_without_warning(caplog):
    """`auto_review:` with no value is the same commented-out idiom the
    section validators accept silently; it must not be reported as garbage."""
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config("triggers:\n  auto_review:\n")
    assert config.triggers.auto_review is True
    assert "themis_invalid_auto_review" not in caplog.text


def test_repo_config__auto_review_yaml_spellings_still_coerce():
    """Quoted booleans that pydantic's lax mode accepted before the lenient
    validator must keep working: a repo that opted out with `'false'` or `0`
    must not silently get auto-reviews re-enabled."""
    for raw in ('"false"', "'no'", "0"):
        text = f"triggers:\n  auto_review: {raw}\n"
        assert parse_repo_config(text).triggers.auto_review is False, raw
    assert parse_repo_config(
        "triggers:\n  auto_review: 'yes'\n"
    ).triggers.auto_review is True


def test_skip_title_match__wildcard_and_case_insensitive():
    config = parse_repo_config("triggers:\n  skip_titles:\n    - 'ci: *'\n")
    assert skip_title_match(config, "ci: bump runner image") == "ci: *"
    assert skip_title_match(config, "CI: bump runner image") == "ci: *"
    assert skip_title_match(config, "fix: broken login") is None


def test_skip_title_match__whole_title_semantics():
    """Patterns cover the whole title: a prefix glob must not fire on
    mid-title or mid-word hits (`PCI:` is not `ci:`)."""
    config = parse_repo_config("triggers:\n  skip_titles:\n    - 'ci: *'\n")
    assert skip_title_match(config, "PCI: rotate keys") is None
    assert skip_title_match(config, "revert ci: bump runner") is None


def test_skip_title_match__star_is_glob_not_regex():
    """`WIP*` means "starts with WIP", never regex `WI(P)*` — the latter
    would skip any title containing "wi"."""
    config = parse_repo_config("triggers:\n  skip_titles:\n    - 'WIP*'\n")
    assert skip_title_match(config, "WIP: new dashboard") == "WIP*"
    assert skip_title_match(config, "wip stuff") == "WIP*"
    assert skip_title_match(config, "Fix window resize handling") is None


def test_skip_title_match__regex_metacharacters_are_literals():
    """Regex syntax has no power here: `(a+)+$` neither matches "aaaa!"
    nor reaches the regex engine (the classic ReDoS pattern is inert)."""
    config = parse_repo_config("triggers:\n  skip_titles:\n    - '(a+)+$'\n")
    assert skip_title_match(config, "a" * 64 + "!") is None
    assert skip_title_match(config, "(a+)+$") == "(a+)+$"


def test_skip_title_match__question_mark_and_infix_star():
    config = parse_repo_config(
        "triggers:\n  skip_titles:\n    - '*[skip review]*'\n    - 'v?.?.? release'\n"
    )
    assert skip_title_match(config, "feat: thing [skip review]") == "*[skip review]*"
    assert skip_title_match(config, "v1.2.3 release") == "v?.?.? release"
    assert skip_title_match(config, "v1.2.30 release") is None


def test_skip_title_match__literal_star_in_title():
    """A `*` in the *pattern* must stay a wildcard even when the title
    character under the cursor is a literal `*` — the literal branch must
    not consume it (regression: fuzz found `*` failing against `*b`)."""
    config = parse_repo_config(
        "triggers:\n  skip_titles:\n    - '*new feature'\n    - 'x*y'\n"
    )
    assert skip_title_match(config, "*WIP* new feature") == "*new feature"
    assert skip_title_match(config, "x***y") == "x*y"


def test_skip_title_match__hostile_title_length_clamped():
    """GitHub caps titles at 256 chars, but the clamp must not depend on
    that: an API-crafted multi-kB title stays inside the O(n*m) budget."""
    config = parse_repo_config("triggers:\n  skip_titles:\n    - 'ci: *'\n")
    assert skip_title_match(config, "ci: " + "a" * 10_000) == "ci: *"
    assert skip_title_match(config, "x" * 10_000) is None


def test_skip_title_match__no_patterns_matches_nothing():
    assert skip_title_match(parse_repo_config(None), "ci: anything") is None


# --- engine settings ----------------------------------------------------------


def test_load_settings__engine_default_codex(monkeypatch):
    _set_env(monkeypatch)

    assert load_settings().engine == "codex"


def test_load_settings__engine_claude(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("THEMIS_ENGINE", "claude")

    assert load_settings().engine == "claude"


def test_load_settings__engine_unknown__raises(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("THEMIS_ENGINE", "gemini")

    with pytest.raises(SettingsError, match="invalid engine"):
        load_settings()


# --- repo engine + web_access -------------------------------------------------


def test_repo_config__engine_default_none():
    config = parse_repo_config("model:\n  reasoning_effort: low\n")
    assert config.engine is None
    assert config.web_access is False


def test_repo_config__engine_claude():
    assert parse_repo_config("engine: claude\n").engine == "claude"


def test_repo_config__engine_invalid__coerces_to_none_and_keeps_rest(caplog):
    text = "engine: caude\nlimits:\n  max_attempts: 5\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.engine is None
    assert config.limits.max_attempts == 5  # rest of the config preserved
    assert "themis_invalid_repo_engine" in caplog.text


def test_repo_config__web_access_true():
    assert parse_repo_config("web_access: true\n").web_access is True


def test_repo_config__model_name_default_is_none():
    # Engine-aware defaults resolve in the service, not here.
    assert parse_repo_config(None).model.name is None


def test_repo_config__learnings_defaults():
    config = parse_repo_config(None)
    assert config.learnings.enabled is True
    assert config.learnings.digest_threshold == 10


def test_repo_config__learnings_opt_out():
    config = parse_repo_config("learnings:\n  enabled: false\n")
    assert config.learnings.enabled is False


def test_repo_config__learnings_threshold_below_one_clamps(caplog):
    config = parse_repo_config("learnings:\n  digest_threshold: 0\n")
    assert config.learnings.digest_threshold == 10
    assert "themis_invalid_digest_threshold" in caplog.text


def test_load_settings__data_root_from_env(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_DATA_ROOT": "/var/lib/themis"})
    assert str(load_settings().data_root) == "/var/lib/themis"


def test_load_settings__data_root_default_is_home_dot_themis(monkeypatch):
    _set_env(monkeypatch)
    assert load_settings().data_root.name == ".themis"


def test_load_settings__default_repo_config_unset_is_none(monkeypatch):
    _set_env(monkeypatch)
    assert load_settings().default_repo_config is None


def test_load_settings__default_repo_config_raw_yaml(monkeypatch):
    yaml_text = "triggers:\n  auto_review: false\n"
    _set_env(monkeypatch, extra={"THEMIS_DEFAULT_REPO_CONFIG": yaml_text})
    assert load_settings().default_repo_config == yaml_text


def test_load_settings__default_repo_config_base64_decoded(monkeypatch):
    yaml_text = "triggers:\n  auto_review: false\n"
    encoded = base64.b64encode(yaml_text.encode()).decode()
    _set_env(monkeypatch, extra={"THEMIS_DEFAULT_REPO_CONFIG": encoded})
    assert load_settings().default_repo_config == yaml_text


def test_load_settings__default_repo_config_wrapped_base64_decoded(monkeypatch):
    """GNU base64 wraps output at 76 chars; the wrap must not push a valid
    encoded config onto the raw-yaml path (where it fails as a non-mapping)."""
    yaml_text = "triggers:\n  auto_review: false\nlearnings:\n  enabled: false\n"
    encoded = base64.encodebytes(yaml_text.encode()).decode()
    assert "\n" in encoded.strip()  # the wrap this test is about
    _set_env(monkeypatch, extra={"THEMIS_DEFAULT_REPO_CONFIG": encoded})
    assert load_settings().default_repo_config == yaml_text


def test_load_settings__default_repo_config_invalid_yaml_rejected(monkeypatch):
    """Instance config is trusted operator input: a broken value means the
    operator's intent is lost entirely, so fail fast instead of degrading."""
    _set_env(monkeypatch, extra={"THEMIS_DEFAULT_REPO_CONFIG": "a: [unclosed"})
    with pytest.raises(SettingsError, match="THEMIS_DEFAULT_REPO_CONFIG"):
        load_settings()


def test_load_settings__default_repo_config_non_mapping_rejected(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_DEFAULT_REPO_CONFIG": "just a string"})
    with pytest.raises(SettingsError, match="THEMIS_DEFAULT_REPO_CONFIG"):
        load_settings()


def test_load_settings__default_repo_config_blank_is_none(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_DEFAULT_REPO_CONFIG": ""})
    assert load_settings().default_repo_config is None


# --- review modules (tri-state: always | auto | off) -------------------------


def test_repo_config__review_modules_unset_resolve_global_profile():
    resolved = resolve_modules(parse_repo_config(None))
    assert set(resolved) == set(MODULE_NAMES)
    assert resolved["scorecard"] == "always"
    assert resolved["walkthrough"] == "always"
    assert resolved["product_impact"] == "always"
    assert resolved["sign_off"] == "always"
    assert resolved["verification_steps"] == "always"
    assert resolved["assumptions"] == "always"
    assert resolved["ci_context"] == "auto"
    assert resolved["inline_findings"] == "auto"
    assert resolved["code_suggestions"] == "auto"
    assert resolved["big_picture"] == "auto"


def test_repo_config__review_modules_tri_state_values():
    config = parse_repo_config(
        "review:\n  modules:\n    scorecard: always\n    walkthrough: 'off'\n"
    )
    resolved = resolve_modules(config)
    assert resolved["scorecard"] == "always"
    assert resolved["walkthrough"] == "off"
    assert resolved["product_impact"] == "always"


def test_repo_config__review_modules_boolean_aliases():
    # yaml 1.1 also parses a bare `off` as False, which aliases to "off" - the
    # unquoted spelling users will write must land on the same state.
    config = parse_repo_config(
        "review:\n  modules:\n    scorecard: true\n    sign_off: false\n    walkthrough: off\n"
    )
    resolved = resolve_modules(config)
    assert resolved["scorecard"] == "always"  # true/auto = enabled
    assert resolved["sign_off"] == "off"
    assert resolved["walkthrough"] == "off"


def test_repo_config__review_modules_invalid_value_degrades_and_keeps_rest(caplog):
    text = (
        "engine: claude\nreview:\n  modules:\n"
        "    scorecard: sometimes\n"
        "    walkthrough: 'off'\n"
        "    future_module: always\n"
    )
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.engine == "claude"  # rest of the config preserved
    resolved = resolve_modules(config)
    assert resolved["scorecard"] == "always"  # invalid -> built-in default
    assert resolved["walkthrough"] == "off"  # valid sibling survives
    assert resolved["product_impact"] == "always"  # omitted -> built-in default
    assert "future_module" not in resolved  # unknown fields are ignored
    assert "themis_invalid_review_module" in caplog.text


def test_resolve_modules__partial_config_overlays_defaults_per_field():
    config = parse_repo_config("review:\n  modules:\n    scorecard: false\n")
    resolved = resolve_modules(config)
    assert resolved["scorecard"] == "off"
    assert resolved["walkthrough"] == "always"


def test_resolve_modules__presentation_auto_is_compatibility_alias_for_enabled():
    config = parse_repo_config(
        "review:\n  modules:\n    verification_steps: auto\n    assumptions: auto\n"
    )

    resolved = resolve_modules(config)

    assert resolved["verification_steps"] == "always"
    assert resolved["assumptions"] == "always"


def test_repo_config__review_modules_wrong_container_keeps_rest(caplog):
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config("engine: claude\nreview:\n  modules: nonsense\n")
    assert config.engine == "claude"  # rest of the config preserved
    resolved = resolve_modules(config)
    assert resolved == resolve_modules(parse_repo_config(None))
    assert "themis_invalid_review_modules" in caplog.text


def test_repo_config__review_wrong_container_keeps_rest(caplog):
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config("engine: claude\nreview: 7\n")
    assert config.engine == "claude"
    resolved = resolve_modules(config)
    assert resolved == resolve_modules(parse_repo_config(None))
    assert "themis_invalid_review_config" in caplog.text


def test_repo_config__null_review_containers_keep_rest():
    # `review:` with nothing under it (children commented out) is yaml null;
    # it must behave as unset, not void the rest of the config.
    for text in ("engine: claude\nreview:\n", "engine: claude\nreview:\n  modules:\n"):
        config = parse_repo_config(text)
        assert config.engine == "claude", text
        resolved = resolve_modules(config)
        assert resolved == resolve_modules(parse_repo_config(None))


def test_repo_config__agent_defaults_off():
    config = parse_repo_config("engine: claude\n")
    assert config.agent.context is False
    assert config.agent.skills is False


def test_repo_config__agent_opt_in_independent():
    config = parse_repo_config("agent:\n  context: true\n")
    assert config.agent.context is True
    assert config.agent.skills is False
    config = parse_repo_config("agent:\n  skills: true\n")
    assert config.agent.context is False
    assert config.agent.skills is True


def test_repo_config__agent_invalid_value_degrades_and_keeps_rest(caplog):
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(
            "engine: claude\nagent:\n  context: sometimes\n  skills: true\n"
        )
    assert config.engine == "claude"
    assert config.agent.context is False  # invalid value falls to the default
    assert config.agent.skills is True  # sibling key unaffected
    assert "themis_invalid_agent_capability" in caplog.text


def test_repo_config__agent_wrong_or_null_container_keeps_rest(caplog):
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config("engine: claude\nagent: 7\n")
    assert config.engine == "claude"
    assert config.agent.context is False and config.agent.skills is False
    assert "themis_invalid_agent_config" in caplog.text
    config = parse_repo_config("engine: claude\nagent:\n")  # yaml null
    assert config.engine == "claude"
    assert config.agent.context is False and config.agent.skills is False
