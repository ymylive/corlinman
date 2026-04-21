"""DeepSeek provider — OpenAI-compatible endpoint at ``api.deepseek.com``.

Reuses :class:`corlinman_providers.openai_provider.OpenAIProvider` with a
DeepSeek-specific ``base_url`` and ``DEEPSEEK_API_KEY`` env var.
"""

from __future__ import annotations

from typing import ClassVar

from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek adapter — inherits OpenAI-standard tool_calls support."""

    name: ClassVar[str] = "deepseek"
    kind: ClassVar[ProviderKind] = ProviderKind.DEEPSEEK

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or "https://api.deepseek.com/v1",
            env_key="DEEPSEEK_API_KEY",
        )

    @classmethod
    def build(cls, spec: ProviderSpec) -> DeepSeekProvider:
        return cls(api_key=spec.api_key, base_url=spec.base_url)

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith("deepseek-")
