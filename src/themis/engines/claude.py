"""claude -p adapter (Claude subscription via CLAUDE_CODE_OAUTH_TOKEN)."""

import os
import tempfile
from pathlib import Path

from themis.engines.base import allowlisted_env, run_cli

# Match Claude's terminal subscription-limit diagnostics, not generic agent
# output about rate limits. Transient API throttling must remain retryable.
_QUOTA_MARKERS = (
    "you've hit your session limit",
    "you've hit your weekly limit",
    "you've hit your opus limit",
    "you've hit your limit · resets",
)
_EXTRA_ENV = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

# No self-updates or third-party telemetry from inside a review job.
_HYGIENE_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}


def build_command(prompt: str, model: str, web_access: bool) -> list[str]:
    command = [
        "claude", "-p", prompt,
        "--model", model,
        "--dangerously-skip-permissions",
        "--safe-mode",
        # Never load repo-controlled CLAUDE.md, settings, hooks, skills,
        # agents, plugins, or MCP servers from the untrusted PR checkout.
        "--setting-sources", "",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
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
        # CLAUDE_CONFIG_DIR also isolates ~/.claude.json and user plugins,
        # which setting-sources intentionally does not cover.
        with tempfile.TemporaryDirectory(prefix="themis-claude-") as config_dir:
            env = allowlisted_env(_EXTRA_ENV) | _HYGIENE_ENV | {
                "CLAUDE_CONFIG_DIR": config_dir,
            }
            return await run_cli(
                name="claude",
                command=build_command(prompt, model, web_access),
                workspace=workspace,
                env=env,
                timeout=timeout,
                quota_markers=_QUOTA_MARKERS,
            )
