"""codex exec adapter (Codex subscription via CODEX_HOME auth.json)."""

import os
from pathlib import Path

from themis.engines.base import allowlisted_env, run_cli

_QUOTA_MARKERS = ("usage limit",)
_EXTRA_ENV = frozenset({"CODEX_HOME"})


def build_command(
    prompt: str, model: str, effort: str, sandbox: str, web_access: bool,
    native_context: bool = False,
) -> list[str]:
    command = [
        "codex", "exec",
        "--sandbox", sandbox,
        # Reviews run in untrusted PR workspaces. Keep authentication from
        # CODEX_HOME; worker user configuration and repo execpolicy .rules
        # files never load. Note --ignore-rules does NOT cover AGENTS.md:
        # codex discovers that natively and has no flag against it, so
        # instruction-file isolation is the workspace mask applied by
        # trusted_context.py before every job. The context opt-in changes
        # nothing here; it materializes trusted base copies for that same
        # native discovery to read.
        "--ignore-user-config",
        "--ignore-rules",
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
        native_context: bool = False, native_skills: bool = False,
        max_thinking_tokens: int | None = None,
    ) -> str:
        # native_skills is accepted for protocol parity; codex has no skills
        # surface, so the skills opt-in changes nothing here. max_thinking_tokens
        # is likewise claude-only; codex thinking is driven by reasoning_effort.
        return await run_cli(
            name="codex",
            command=build_command(
                prompt, model, effort, self._sandbox, web_access,
                native_context=native_context,
            ),
            workspace=workspace,
            env=allowlisted_env(_EXTRA_ENV),
            timeout=timeout,
            quota_markers=_QUOTA_MARKERS,
        )
