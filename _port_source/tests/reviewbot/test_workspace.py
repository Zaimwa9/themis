import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from reviewbot.workspace import _git as _git_async
from reviewbot.workspace import (
    WorkspaceError,
    clone_url_for,
    prepare_workspace,
    remove_workspace,
    run_git,
    sweep_stale,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def fake_remote(tmp_path: Path) -> tuple[Path, str]:
    """A bare repo with main + refs/pull/7/head; returns (bare_path, pr_head_sha)."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "a.py").write_text("print('hi')\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("checkout", "-b", "feature", cwd=work)
    (work / "a.py").write_text("print('bye')\n")
    _git("commit", "-am", "change", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("update-ref", "refs/pull/7/head", sha, cwd=bare)
    return bare, sha


@pytest.mark.asyncio
async def test_prepare_workspace__pr_ref__checks_out_head(fake_remote, tmp_path: Path):
    bare, sha = fake_remote

    workspace = await prepare_workspace(
        root=tmp_path / "ws", clone_url=str(bare), pr_number=7, base_ref="main"
    )

    assert _git("rev-parse", "HEAD", cwd=workspace) == sha
    assert (workspace / "a.py").read_text() == "print('bye')\n"
    # base ref fetched so the agent can diff against origin/main
    assert "origin/main" in _git("branch", "-r", cwd=workspace)


@pytest.mark.asyncio
async def test_prepare_workspace__clone_url__not_persisted_in_git_config(
    fake_remote, tmp_path: Path
):
    bare, _ = fake_remote

    workspace = await prepare_workspace(
        root=tmp_path / "ws", clone_url=str(bare), pr_number=7, base_ref="main"
    )

    # the clone URL (which carries credentials in prod) must not be stored
    # where the review agent, running on untrusted PR content, can read it
    config = (workspace / ".git" / "config").read_text()
    assert str(bare) not in config
    assert "origin/main" in _git("branch", "-r", cwd=workspace)


@pytest.mark.asyncio
async def test_prepare_workspace__bad_pr_number__raises_and_cleans_up(
    fake_remote, tmp_path: Path
):
    bare, _ = fake_remote
    root = tmp_path / "ws"

    with pytest.raises(WorkspaceError, match="fetch failed"):
        await prepare_workspace(
            root=root, clone_url=str(bare), pr_number=999, base_ref="main"
        )

    # failed preparation must not leave a workspace behind
    assert not root.exists() or not any(root.iterdir())


def test_clone_url_for__token__embedded():
    url = clone_url_for("acme/widgets", "ghs_abc")
    assert url == "https://x-access-token:ghs_abc@github.com/acme/widgets.git"


def test_remove_workspace__existing_dir__gone(tmp_path: Path):
    target = tmp_path / "ws"
    target.mkdir()
    (target / "f.txt").write_text("x")

    remove_workspace(target)

    assert not target.exists()


def test_sweep_stale__old_dirs_removed_fresh_kept(tmp_path: Path):
    root = tmp_path / "root"
    old = root / "old"
    fresh = root / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    stale_time = time.time() - 100_000
    os.utime(old, (stale_time, stale_time))

    sweep_stale(root, max_age_seconds=86_400)

    assert not old.exists()
    assert fresh.exists()


def test_sweep_stale__missing_root__no_error(tmp_path: Path):
    sweep_stale(tmp_path / "does-not-exist")


@pytest.mark.asyncio
async def test_git__failure_output_with_token__token_scrubbed(tmp_path: Path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _git("init", "-q", cwd=cwd)

    with pytest.raises(WorkspaceError) as exc_info:
        await _git_async(
            "fetch",
            "file:///x-access-token:sekrit123@/nonexistent/x.git",
            cwd=cwd,
        )

    assert "sekrit123" not in str(exc_info.value)
    assert "x-access-token:***" in str(exc_info.value)


@pytest.mark.asyncio
async def test_prepare_workspace__after_checkout__no_fetch_url_left_on_disk(
    fake_remote, tmp_path: Path
):
    bare, _ = fake_remote

    workspace = await prepare_workspace(
        root=tmp_path / "ws", clone_url=str(bare), pr_number=7, base_ref="main"
    )

    assert not (workspace / ".git" / "FETCH_HEAD").exists()
    assert not (workspace / ".git" / "logs").exists()
    for f in (workspace / ".git").rglob("*"):
        if f.is_file():
            assert str(bare) not in f.read_text(errors="ignore")


def _spawn_sleeper(monkeypatch, spawned: dict) -> None:
    """Make the git runner launch a real long-lived sleeper instead of git,
    so timeout/cancel kill behavior can be asserted against a real PID/group."""
    real = asyncio.create_subprocess_exec

    async def slow(*args: str, **kwargs) -> asyncio.subprocess.Process:
        process = await real(
            "sleep", "30",
            cwd=kwargs.get("cwd"),
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            start_new_session=kwargs.get("start_new_session", False),
        )
        spawned["process"] = process
        return process

    monkeypatch.setattr(
        "reviewbot.workspace.asyncio.create_subprocess_exec", slow
    )


@pytest.mark.asyncio
async def test_run_git__timeout__raises_and_kills_process_group(tmp_path, monkeypatch):
    spawned: dict = {}
    _spawn_sleeper(monkeypatch, spawned)

    with pytest.raises(WorkspaceError, match="timed out"):
        await run_git("fetch", cwd=tmp_path, timeout=0.1)

    # reaped after SIGKILL: returncode is set, so no lingering zombie
    assert spawned["process"].returncode is not None


@pytest.mark.asyncio
async def test_run_git__cancelled__kills_child(tmp_path, monkeypatch):
    spawned: dict = {}
    _spawn_sleeper(monkeypatch, spawned)

    task = asyncio.ensure_future(run_git("fetch", cwd=tmp_path, timeout=30))
    while "process" not in spawned:
        await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawned["process"].returncode is not None


@pytest.mark.asyncio
async def test_run_git__success__returns_returncode_and_output(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _git("init", "-q", cwd=cwd)

    returncode, output = await run_git("rev-parse", "--is-inside-work-tree", cwd=cwd)

    assert returncode == 0
    assert output.strip() == "true"


@pytest.mark.asyncio
async def test_run_git__failure__returns_nonzero_without_raising(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _git("init", "-q", cwd=cwd)

    returncode, output = await run_git("rev-parse", "HEAD", cwd=cwd)

    assert returncode != 0


def test_sweep_stale__child_vanishes_mid_sweep__other_stale_dir_still_removed(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    vanishing = root / "vanishing"
    stale = root / "stale"
    vanishing.mkdir(parents=True)
    stale.mkdir(parents=True)
    stale_time = time.time() - 100_000
    os.utime(vanishing, (stale_time, stale_time))
    os.utime(stale, (stale_time, stale_time))

    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "vanishing":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    sweep_stale(root, max_age_seconds=86_400)

    assert not stale.exists()
