"""China-bucket LLM adapters: DeepSeek, Qwen (DashScope), GLM (智谱).

All three share an OpenAI-compatible REST shape but differ in ``base_url``
and auth env var; each concrete adapter is a thin subclass of
:class:`corlinman_providers.openai_provider.OpenAIProvider`.
"""

from __future__ import annotations

from corlinman_providers.china.deepseek import DeepSeekProvider
from corlinman_providers.china.glm import GLMProvider
from corlinman_providers.china.qwen import QwenProvider

__all__ = ["DeepSeekProvider", "GLMProvider", "QwenProvider"]
