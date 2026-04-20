"""Provider registry — resolves a model id to a ``CorlinmanProvider`` instance.

M2 surface: the registry knows how to build every M2 provider adapter
(Anthropic, OpenAI, Google, DeepSeek, Qwen, GLM). First matching prefix
wins. M3 replaces the hard-coded ``_RULES`` list with
``ModelRedirect.json`` lookup + hot reload.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from corlinman_providers.anthropic_provider import AnthropicProvider
from corlinman_providers.base import CorlinmanProvider
from corlinman_providers.china import DeepSeekProvider, GLMProvider, QwenProvider
from corlinman_providers.google_provider import GoogleProvider
from corlinman_providers.openai_provider import OpenAIProvider

logger = structlog.get_logger(__name__)


_ProviderFactory = Callable[[], CorlinmanProvider]


# Ordered list — first matching prefix wins. M3 replaces with
# ``ModelRedirect.json`` lookup.
_RULES: list[tuple[str, _ProviderFactory]] = [
    ("claude-", lambda: AnthropicProvider()),
    ("gpt-", lambda: OpenAIProvider()),
    ("o1-", lambda: OpenAIProvider()),
    ("o3-", lambda: OpenAIProvider()),
    ("gemini-", lambda: GoogleProvider()),
    ("deepseek-", lambda: DeepSeekProvider()),
    ("qwen", lambda: QwenProvider()),
    ("qwq-", lambda: QwenProvider()),
    ("glm-", lambda: GLMProvider()),
]


class ProviderRegistry:
    """Resolve model id → provider adapter instance (cached per registry)."""

    def __init__(self) -> None:
        self._cache: dict[str, CorlinmanProvider] = {}

    def resolve(self, model: str) -> CorlinmanProvider:
        for prefix, factory in _RULES:
            if model.startswith(prefix):
                if prefix not in self._cache:
                    self._cache[prefix] = factory()
                return self._cache[prefix]
        raise KeyError(f"no provider registered for model {model!r}")


_default = ProviderRegistry()


def resolve(model: str) -> CorlinmanProvider:
    """Module-level convenience: ``resolve("claude-sonnet-4-5")``."""
    return _default.resolve(model)
