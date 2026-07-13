"""qwen engine: Claude Code harness on DashScope's Qwen Coding Plan endpoint
(international). Mainland and pay-as-you-go endpoints are out of scope; if
needed later, add an allowlisted region switch, never a free-form URL."""

from themis.engines.anthropic_api import AnthropicApiEngine

# DashScope Coding Plan exhaustion strings (hour/week/month windows). The
# similarly worded "concurrency allocated quota exceeded" is documented as
# retryable and must NOT match, hence the window-qualified markers. Billing
# arrears ("Arrearage", bill overdue) never auto-reset, so they stay plain
# EngineErrors rather than quota errors.
_QUOTA_MARKERS = (
    "hour allocated quota exceeded",
    "week allocated quota exceeded",
    "month allocated quota exceeded",
)


class QwenEngine(AnthropicApiEngine):
    name = "qwen"
    _token_env = "QWEN_API_KEY"
    _base_url = "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"
    _quota_markers = _QUOTA_MARKERS
