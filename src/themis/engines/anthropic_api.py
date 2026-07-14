"""Claude Code harness in API mode against third-party Anthropic-compatible
endpoints (Z.ai, DashScope). Same binary and hardening as the claude engine;
only the auth env differs."""

import os

from themis.engines.claude import ClaudeEngine


class AnthropicApiEngine(ClaudeEngine):
    """The endpoint is baked into the subclass, never read from env or repo
    config: a controllable base URL would redirect the provider key to an
    attacker host."""

    _base_url: str

    def _auth_env(self) -> dict[str, str]:
        env = {
            "ANTHROPIC_BASE_URL": self._base_url,
            # Providers recommend a generous request timeout for long agentic
            # turns; run_cli's wall clock still bounds the whole attempt.
            "API_TIMEOUT_MS": "3000000",
        }
        token = os.environ.get(self._token_env)
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
        return env
