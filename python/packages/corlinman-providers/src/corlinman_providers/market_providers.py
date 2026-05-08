"""Market-LLM adapter stubs added with the free-form-providers refactor.

Most market LLM vendors (Mistral, Cohere, Together, Groq, Replicate, …) speak
the OpenAI wire format under their own base URLs. Rather than ask operators
to reach for ``kind = "openai_compatible"`` plus a hand-rolled ``base_url``,
the Rust schema now exposes them as first-class :class:`ProviderKind`
variants. Each adapter here is a thin wrapper that delegates to
:class:`corlinman_providers.openai_compatible.OpenAICompatibleProvider` —
the only thing that differs is a documented default ``base_url`` and the
class-level ``kind`` discriminator.

Bedrock and Azure are declared (so configs can carry them through schema
validation) but raise ``NotImplementedError`` at build time. Real SigV4 /
deployment-routing support lands in a follow-up; operators needing them
today should fall back to ``kind = "openai_compatible"`` against a
compatible proxy.
"""

from __future__ import annotations

from typing import ClassVar

from corlinman_providers.openai_compatible import OpenAICompatibleProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


def _build_compat(
    spec: ProviderSpec,
    *,
    default_base_url: str,
    kind: ProviderKind,
) -> OpenAICompatibleProvider:
    """Shared helper: build an OpenAI-compat adapter with a sensible default
    ``base_url`` so configs that omit it still resolve to the vendor's
    documented endpoint."""
    base_url = spec.base_url or default_base_url
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        api_key=spec.api_key,
        instance_name=spec.name,
    )
    # Stamp the user-visible kind on the instance so admin listings report
    # `mistral` / `cohere` / etc. instead of generic `openai_compatible`.
    provider.__dict__["kind"] = kind
    return provider


class MistralProvider(OpenAICompatibleProvider):
    """Mistral La Plateforme — OpenAI-compat at ``api.mistral.ai/v1``."""

    name: ClassVar[str] = "mistral"
    kind: ClassVar[ProviderKind] = ProviderKind.MISTRAL
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.mistral.ai/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class CohereProvider(OpenAICompatibleProvider):
    """Cohere — OpenAI-compat endpoint at ``api.cohere.ai/compatibility/v1``."""

    name: ClassVar[str] = "cohere"
    kind: ClassVar[ProviderKind] = ProviderKind.COHERE
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.cohere.ai/compatibility/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class TogetherProvider(OpenAICompatibleProvider):
    """Together AI — OpenAI-compat at ``api.together.xyz/v1``."""

    name: ClassVar[str] = "together"
    kind: ClassVar[ProviderKind] = ProviderKind.TOGETHER
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.together.xyz/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class GroqProvider(OpenAICompatibleProvider):
    """Groq Cloud — OpenAI-compat at ``api.groq.com/openai/v1``."""

    name: ClassVar[str] = "groq"
    kind: ClassVar[ProviderKind] = ProviderKind.GROQ
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.groq.com/openai/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class ReplicateProvider(OpenAICompatibleProvider):
    """Replicate — OpenAI-compat at ``api.replicate.com/openai/v1``."""

    name: ClassVar[str] = "replicate"
    kind: ClassVar[ProviderKind] = ProviderKind.REPLICATE
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.replicate.com/openai/v1"

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        return _build_compat(spec, default_base_url=cls.DEFAULT_BASE_URL, kind=cls.kind)


class BedrockProvider:
    """AWS Bedrock — placeholder until SigV4 auth lands.

    Configs may declare ``kind = "bedrock"`` (the schema accepts it) but the
    runtime raises ``NotImplementedError`` at build time so the failure is
    loud and immediate. Use ``kind = "openai_compatible"`` with a SigV4-
    capable proxy as a workaround until a real adapter ships.
    """

    name: ClassVar[str] = "bedrock"
    kind: ClassVar[ProviderKind] = ProviderKind.BEDROCK

    @classmethod
    def build(cls, spec: ProviderSpec) -> BedrockProvider:
        raise NotImplementedError(
            f"Bedrock adapter is not yet implemented (provider {spec.name!r}). "
            "Use kind = 'openai_compatible' with a SigV4 proxy as a workaround."
        )


class AzureProvider:
    """Azure OpenAI Service — placeholder until deployment routing lands.

    Configs may declare ``kind = "azure"`` (the schema accepts it) but the
    runtime raises ``NotImplementedError`` at build time so the failure is
    loud and immediate. Use ``kind = "openai_compatible"`` with the explicit
    Azure deployment URL as a workaround until a real adapter ships.
    """

    name: ClassVar[str] = "azure"
    kind: ClassVar[ProviderKind] = ProviderKind.AZURE

    @classmethod
    def build(cls, spec: ProviderSpec) -> AzureProvider:
        raise NotImplementedError(
            f"Azure OpenAI adapter is not yet implemented (provider {spec.name!r}). "
            "Use kind = 'openai_compatible' with the full Azure deployment URL "
            "as base_url as a workaround."
        )


__all__ = [
    "AzureProvider",
    "BedrockProvider",
    "CohereProvider",
    "GroqProvider",
    "MistralProvider",
    "ReplicateProvider",
    "TogetherProvider",
]
