"""glm engine: Claude Code harness on Z.ai's GLM Coding Plan endpoint."""

from themis.engines.anthropic_api import AnthropicApiEngine

# Z.ai exhausted-plan diagnostics (error codes 1308-1310, 1316-1321):
# "Usage limit reached for ...", "Weekly/Monthly Limit Exhausted",
# "Your GLM Coding Plan package has expired". Transient throttling (1302)
# says "Rate limit reached for requests", which none of these match; it
# must remain a retryable EngineError.
#
# Markers must stay phrase-specific: run_cli matches them against the
# lowercased tail of agent-visible output (stdout/stderr the CLI produced),
# not a curated error code, so a marker that is too generic can accidentally
# match ordinary agent prose (e.g. "the retry limit exhausted while calling
# the API") and misclassify a plain failure as quota-exhausted. The 1310
# template substitutes the window in ("Weekly/Monthly Limit Exhausted..."),
# so all three forms are listed explicitly rather than matching on the bare
# "limit exhausted" substring.
_QUOTA_MARKERS = (
    "usage limit reached for",
    "weekly limit exhausted",
    "monthly limit exhausted",
    "weekly/monthly limit exhausted",
    "coding plan package has expired",
)


class GlmEngine(AnthropicApiEngine):
    name = "glm"
    _token_env = "GLM_API_KEY"
    _base_url = "https://api.z.ai/api/anthropic"
    _quota_markers = _QUOTA_MARKERS
