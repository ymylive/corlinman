"""GLM (智谱 Zhipu) provider — OpenAI-compatible mode.

Zhipu's BigModel platform exposes an OpenAI-compatible endpoint at
``open.bigmodel.cn/api/paas/v4``. Auth is a JWT derived from a split
``id.secret`` API key — we defer the JWT dance to
:class:`corlinman_providers.openai_provider.OpenAIProvider` by feeding the
raw key as Bearer; the server accepts that path for ``glm-4*`` models
(verified with 智谱 docs 2025-10).

TODO(M3): implement the ``id.secret`` → JWT generation if Zhipu tightens
the Bearer-key path; signed tokens rotate every 30 minutes.
"""

from __future__ import annotations

from typing import ClassVar

from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


class GLMProvider(OpenAIProvider):
    """GLM / 智谱 BigModel adapter — reuses OpenAI-standard tool_calls support."""

    name: ClassVar[str] = "glm"
    kind: ClassVar[ProviderKind] = ProviderKind.GLM

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or "https://open.bigmodel.cn/api/paas/v4",
            env_key="ZHIPU_API_KEY",
        )

    @classmethod
    def build(cls, spec: ProviderSpec) -> GLMProvider:
        return cls(api_key=spec.api_key, base_url=spec.base_url)

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith("glm-")
