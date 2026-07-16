"""Contract tests for the release-please uv.lock updater.

release-please-config.json points a GenericToml extra-files updater at
uv.lock so the release PR bumps the lockfile atomically with
pyproject.toml. The updater fails open: if its jsonpath selects nothing
it leaves uv.lock untouched, which would reintroduce the locked-version
drift that broke CI on main (#15). uv.lock's schema only changes in
normal PRs (uv rewrites it during dependency work; release-please only
text-edits it), so pinning the selector's structural assumptions here
turns silent drift into a red check in the PR that introduces it.
"""

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

LOCKFILE_JSONPATH = "$.package[?(@.name.value=='themis')].version"


def test_release_config_pins_the_lockfile_updater() -> None:
    config = json.loads((REPO_ROOT / "release-please-config.json").read_text())
    assert {
        "type": "toml",
        "path": "uv.lock",
        "jsonpath": LOCKFILE_JSONPATH,
    } in config.get("extra-files", []), (
        "release-please-config.json no longer carries the uv.lock updater; "
        "without it release PRs ship a stale lockfile and break "
        "`uv sync --locked` on main"
    )


def test_uv_lock_still_has_the_shape_the_updater_selects() -> None:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    themis_entries = [
        package
        for package in lock.get("package", [])
        if package.get("name") == "themis"
    ]
    assert len(themis_entries) == 1 and "version" in themis_entries[0], (
        "uv.lock no longer matches the GenericToml jsonpath "
        f"{LOCKFILE_JSONPATH!r}; update the selector in "
        "release-please-config.json (and this test) or release PRs will "
        "silently ship a stale lockfile"
    )


def test_uv_lock_pins_the_pyproject_version() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    locked = next(p for p in lock["package"] if p.get("name") == "themis")
    assert locked["version"] == pyproject["project"]["version"], (
        "uv.lock pins a different themis version than pyproject.toml; "
        "run `uv lock` and commit the result"
    )
