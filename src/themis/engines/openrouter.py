"""openrouter engine: Claude Code harness on OpenRouter's Anthropic-protocol
gateway ("Anthropic Skin") — one prepaid key, many models via OpenRouter
slugs (moonshotai/kimi-k3, z-ai/glm-5.2, ...)."""

from themis.engines.anthropic_api import AnthropicApiEngine

# No text quota markers, same rationale as glm: markers match the
# agent-visible output tail and can be echoed by a prompt-steered agent,
# and running out of prepaid credits (402) never auto-resets, so the
# "mention me later to retry" quota comment would mislead. Structured
# classification is #28.


class OpenRouterEngine(AnthropicApiEngine):
    name = "openrouter"
    _token_env = "OPENROUTER_API_KEY"
    _base_url = "https://openrouter.ai/api"
    _quota_markers = ()
