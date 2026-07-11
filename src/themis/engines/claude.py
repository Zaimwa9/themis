"""claude -p adapter (Claude subscription via CLAUDE_CODE_OAUTH_TOKEN)."""

import os
from pathlib import Path

from themis.engines.base import allowlisted_env, run_cli

# The claude CLI retries transient 429s internally; either marker surfacing
# in the tail means the subscription window is exhausted.
_QUOTA_MARKERS = ("usage limit", "rate limit")
_EXTRA_ENV = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

# No self-updates or third-party telemetry from inside a review job.
_HYGIENE_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}


def build_command(prompt: str, model: str, web_access: bool) -> list[str]:
    command = [
        "claude", "-p", prompt,
        "--model", model,
        "--dangerously-skip-permissions",
        "--output-format", "text",
    ]
    if not web_access:
        command += ["--disallowedTools", "WebFetch,WebSearch"]
    return command


class ClaudeEngine:
    name = "claude"

    def available(self) -> bool:
        return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    async def run(
        self, *, prompt: str, workspace: Path, model: str, effort: str,
        timeout: float, web_access: bool = False,
    ) -> str:
        # effort is accepted for protocol parity; the claude CLI has no
        # reasoning-effort flag.
        return await run_cli(
            name="claude",
            command=build_command(prompt, model, web_access),
            workspace=workspace,
            env=allowlisted_env(_EXTRA_ENV) | _HYGIENE_ENV,
            timeout=timeout,
            quota_markers=_QUOTA_MARKERS,
        )
