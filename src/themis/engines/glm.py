"""glm engine: Claude Code harness on Z.ai's GLM Coding Plan endpoint."""

from themis.engines.anthropic_api import AnthropicApiEngine

# No text quota markers: run_cli matches markers against the agent-visible
# output tail, and any text pattern there can be echoed by a prompt-steered
# agent, misclassifying an ordinary failure as exhausted quota (skipping
# retries and posting a false quota comment). Until quota can be classified
# from provider-structured output (#28), ambiguous glm failures stay
# retryable: true plan exhaustion (Z.ai codes 1308-1310, 1316-1321) surfaces
# as a plain EngineError and a generic failure comment after retries.
# Validated markers may return with #20's live-validation round.


class GlmEngine(AnthropicApiEngine):
    name = "glm"
    _token_env = "GLM_API_KEY"
    _base_url = "https://api.z.ai/api/anthropic"
    _quota_markers = ()
