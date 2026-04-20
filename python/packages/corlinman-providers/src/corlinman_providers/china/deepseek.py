"""DeepSeek provider — OpenAI-compatible endpoint at ``api.deepseek.com``.

Reuses :class:`corlinman_providers.openai_provider.OpenAIProvider` with a
DeepSeek-specific ``base_url`` and ``DEEPSEEK_API_KEY`` env var.
"""

from __future__ import annotations

from corlinman_providers.openai_provider import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek adapter — inherits OpenAI-standard tool_calls support."""

    name = "deepseek"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://api.deepseek.com/v1",
            env_key="DEEPSEEK_API_KEY",
        )

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith("deepseek-")
