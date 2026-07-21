"""kimi engine: Claude Code harness on Moonshot's pay-as-you-go platform
endpoint."""

from themis.engines.anthropic_api import AnthropicApiEngine

# Deliberately NOT the Kimi Code subscription endpoint
# (https://api.kimi.com/coding/): its guidelines restrict subscriptions to
# "personal interactive use only" and name scripted/non-interactive use as
# a violation — exactly Themis's usage pattern. The platform key is
# pay-as-you-go and carries no such restriction.
#
# No text quota markers, same rationale as glm: markers match the
# agent-visible output tail and can be echoed by a prompt-steered agent,
# and pay-as-you-go exhaustion (insufficient balance) never auto-resets,
# so the "mention me later to retry" quota comment would mislead.
# Structured classification is #28.


class KimiEngine(AnthropicApiEngine):
    name = "kimi"
    _token_env = "KIMI_API_KEY"
    _base_url = "https://api.moonshot.ai/anthropic"
    _quota_markers = ()
