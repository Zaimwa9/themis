"""codex exec adapter (Codex subscription via CODEX_HOME auth.json)."""

import os
from pathlib import Path

from themis.engines.base import allowlisted_env, run_cli

_QUOTA_MARKERS = ("usage limit",)
_EXTRA_ENV = frozenset({"CODEX_HOME"})


def build_command(
    prompt: str, model: str, effort: str, sandbox: str, web_access: bool
) -> list[str]:
    command = [
        "codex", "exec",
        "--sandbox", sandbox,
        "-c", "approval_policy=never",
        "-c", f"model_reasoning_effort={effort}",
    ]
    if web_access:
        # Doctrine-driven external checks (e.g. API contract verification)
        # need the network inside workspace-write.
        command += ["-c", "sandbox_workspace_write.network_access=true"]
    command += ["-m", model, "--color", "never", prompt]
    return command


class CodexEngine:
    name = "codex"

    def __init__(self, sandbox: str = "workspace-write") -> None:
        self._sandbox = sandbox

    def available(self) -> bool:
        home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        return (Path(home) / "auth.json").is_file()

    async def run(
        self, *, prompt: str, workspace: Path, model: str, effort: str,
        timeout: float, web_access: bool = False,
    ) -> str:
        return await run_cli(
            name="codex",
            command=build_command(prompt, model, effort, self._sandbox, web_access),
            workspace=workspace,
            env=allowlisted_env(_EXTRA_ENV),
            timeout=timeout,
            quota_markers=_QUOTA_MARKERS,
        )
