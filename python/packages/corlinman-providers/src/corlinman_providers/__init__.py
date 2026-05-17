"""corlinman-providers — LLM provider adapters + failover error taxonomy.

Responsibility: wrap vendor SDKs behind a single :class:`CorlinmanProvider`
Protocol so the agent loop is vendor-agnostic, normalise vendor errors to
the :class:`CorlinmanError` hierarchy that maps to the Rust
``FailoverReason`` enum (see plan §8 A1), and normalise vendor streaming
shapes to :class:`ProviderChunk` (plan §14 R5 — OpenAI-standard JSON
tool_calls are the one true protocol).
"""

from __future__ import annotations

from corlinman_providers.anthropic_provider import AnthropicProvider
from corlinman_providers.base import CorlinmanProvider, ProviderChunk
from corlinman_providers.china import DeepSeekProvider, GLMProvider, QwenProvider
from corlinman_providers.declarative import (
    DeclarativeProvider,
    DeclarativeProviderSpec,
    ModelSpec,
    load_all_specs,
    load_spec_from_toml,
)
from corlinman_providers.failover import (
    AuthError,
    AuthPermanentError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    FormatError,
    ModelNotFoundError,
    OverloadedError,
    RateLimitError,
    TimeoutError,  # noqa: A004 — intentional shadowing; see failover.TimeoutError
)
from corlinman_providers.google_provider import GoogleProvider
from corlinman_providers.mock import MOCK_PREAMBLE, MockProvider
from corlinman_providers.openai_compatible import OpenAICompatibleProvider
from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.registry import MODEL_PREFIX_DEFAULTS, ProviderRegistry, resolve
from corlinman_providers.specs import AliasEntry, EmbeddingSpec, ProviderKind, ProviderSpec

__all__ = [
    "MODEL_PREFIX_DEFAULTS",
    "AliasEntry",
    "AnthropicProvider",
    "AuthError",
    "AuthPermanentError",
    "BillingError",
    "ContextOverflowError",
    "CorlinmanError",
    "CorlinmanProvider",
    "DeclarativeProvider",
    "DeclarativeProviderSpec",
    "DeepSeekProvider",
    "EmbeddingSpec",
    "FormatError",
    "GLMProvider",
    "GoogleProvider",
    "MOCK_PREAMBLE",
    "MockProvider",
    "ModelNotFoundError",
    "ModelSpec",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "OverloadedError",
    "ProviderChunk",
    "ProviderKind",
    "ProviderRegistry",
    "ProviderSpec",
    "QwenProvider",
    "RateLimitError",
    "TimeoutError",
    "load_all_specs",
    "load_spec_from_toml",
    "resolve",
]
