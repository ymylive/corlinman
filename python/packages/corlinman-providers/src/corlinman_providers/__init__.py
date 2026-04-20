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
from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.registry import ProviderRegistry, resolve

__all__ = [
    "AnthropicProvider",
    "AuthError",
    "AuthPermanentError",
    "BillingError",
    "ContextOverflowError",
    "CorlinmanError",
    "CorlinmanProvider",
    "DeepSeekProvider",
    "FormatError",
    "GLMProvider",
    "GoogleProvider",
    "ModelNotFoundError",
    "OpenAIProvider",
    "OverloadedError",
    "ProviderChunk",
    "ProviderRegistry",
    "QwenProvider",
    "RateLimitError",
    "TimeoutError",
    "resolve",
]
