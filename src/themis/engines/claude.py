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
# No self-updates or third-party telemetry from inside a review job.
_HYGIENE_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}


def build_command(
    prompt: str, model: str, web_access: bool, native_discovery: bool = False
) -> list[str]:
    # Default: never load repo-controlled CLAUDE.md, settings, hooks, skills,
    # agents, plugins, or MCP servers from the untrusted PR checkout. With
    # native_discovery the workspace has been rebuilt by trusted_context.py
    # (instructions/skills from the PR base, executable surfaces scrubbed),
    # so project-level discovery may run; MCP stays pinned empty either way.
    # Safe mode also disables CLAUDE.md/skills wholesale, so it must be
    # dropped on the trusted path or the opt-in silently does nothing — the
    # workspace scrub is the guardrail there.
    command = [
        "claude", "-p", prompt,
        "--model", model,
        "--dangerously-skip-permissions",
        *([] if native_discovery else ["--safe-mode"]),
        "--setting-sources", "project" if native_discovery else "",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--output-format", "text",
    ]
    if not web_access:
        command += ["--disallowedTools", "WebFetch,WebSearch"]
    return command


class ClaudeEngine:
    """Also the base for API-mode engines (glm): subclasses override
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
        native_context: bool = False, native_skills: bool = False,
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
                command=build_command(
                    prompt, model, web_access,
                    native_discovery=native_context or native_skills,
                ),
                workspace=workspace,
                env=env,
                timeout=timeout,
                quota_markers=self._quota_markers,
            )
