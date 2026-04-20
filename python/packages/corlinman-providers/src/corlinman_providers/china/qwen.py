"""Qwen (Alibaba DashScope) provider — OpenAI-compatible mode.

DashScope exposes an OpenAI-compatible endpoint at
``dashscope.aliyuncs.com/compatible-mode/v1``; we delegate to
:class:`corlinman_providers.openai_provider.OpenAIProvider`.

TODO(M3): DashScope-native streaming differs slightly in SSE framing when
tool calls are returned; if we run into vendor quirks, override
``chat_stream`` here instead of subclassing blindly.
"""

from __future__ import annotations

from corlinman_providers.openai_provider import OpenAIProvider


class QwenProvider(OpenAIProvider):
    """Qwen / DashScope adapter — reuses OpenAI-standard tool_calls support."""

    name = "qwen"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            env_key="DASHSCOPE_API_KEY",
        )

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith("qwen") or model.startswith("qwq-")
