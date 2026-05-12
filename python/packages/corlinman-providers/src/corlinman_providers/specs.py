"""Provider / alias / embedding configuration specs.

Feature C (§2) pulls the provider wire-up out of a hardcoded prefix table and
into ``config.toml`` — each provider is declared via ``[providers.<name>]``
with a ``kind`` discriminator. This module defines the pydantic shapes the
Rust gateway hands us over whatever channel the Python side learns about
config (today: ``CORLINMAN_PY_CONFIG`` env → JSON file).

Authoritative reference: ``/tmp/corlinman-feature-c-contract.md`` §1 + §2.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProviderKind(StrEnum):
    """Lowercase discriminator identifying the provider wire shape.

    First-party kinds (``anthropic`` / ``openai`` / ``google`` / ``deepseek``
    / ``qwen`` / ``glm``) have bespoke adapters.

    ``openai_compatible`` plus the seven market kinds added in the
    free-form-providers refactor (``mistral`` / ``cohere`` / ``together`` /
    ``groq`` / ``replicate`` / ``bedrock`` / ``azure``) all speak the OpenAI
    wire format and route through :class:`OpenAICompatibleProvider`. They
    are surfaced as named kinds so admin UIs / configs can document operator
    intent without inventing per-kind adapter classes.

    ``bedrock`` and ``azure`` are declared but the runtime currently raises
    ``NotImplementedError`` when one is used — proper SigV4 / deployment-id
    support lands in a follow-up. Operators who need them today should use
    ``kind = "openai_compatible"`` with an explicit ``base_url``.
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    GLM = "glm"
    OPENAI_COMPATIBLE = "openai_compatible"
    MISTRAL = "mistral"
    COHERE = "cohere"
    TOGETHER = "together"
    GROQ = "groq"
    REPLICATE = "replicate"
    BEDROCK = "bedrock"
    AZURE = "azure"
    # sub2api (Wei-Shaw/sub2api) sidecar — OAuth-subscription pooling
    # exposed over OpenAI-compat. Wire shape is identical to
    # ``openai_compatible``; the named kind exists so admin UIs / configs
    # document operator intent. See ``docs/design/sub2api-integration.md``.
    SUB2API = "sub2api"


class ProviderSpec(BaseModel):
    """Single ``[providers.<name>]`` entry from ``config.toml``.

    The backend builds exactly one :class:`CorlinmanProvider` instance per
    enabled spec. Disabled specs are retained for admin-listing only.
    """

    model_config = ConfigDict(frozen=False, extra="allow")

    name: str
    """Unique key, e.g. ``"anthropic"`` or ``"my-local-vllm"``."""

    kind: ProviderKind
    """Wire-shape discriminator — selects which adapter class to build."""

    api_key: str | None = None
    """Resolved API key; ``None`` means "no auth" (valid for local gateways)."""

    base_url: str | None = None
    """Optional for first-party; REQUIRED for ``openai_compatible``."""

    enabled: bool = True

    params: dict[str, Any] = Field(default_factory=dict)
    """Provider-level defaults merged below alias-level overrides."""


class AliasEntry(BaseModel):
    """``[models.aliases.<alias>]`` — routes an alias to a provider+model.

    The alias is the *display* / *user* identifier; ``provider`` must match a
    :class:`ProviderSpec` name and ``model`` is the upstream model id passed
    to the vendor SDK.
    """

    model_config = ConfigDict(frozen=False, extra="allow", protected_namespaces=())

    provider: str
    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class EmbeddingSpec(BaseModel):
    """``[embedding]`` — selects provider + model + dimension for embeddings.

    ``provider`` references a ``[providers.<name>]`` key. The provider SDK
    is reused for embeddings when the kind supports it (OpenAI-compatible
    shapes do; Anthropic does not, for example).
    """

    model_config = ConfigDict(frozen=False, extra="allow", protected_namespaces=())

    provider: str
    model: str
    dimension: int
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AliasEntry",
    "EmbeddingSpec",
    "ProviderKind",
    "ProviderSpec",
]
