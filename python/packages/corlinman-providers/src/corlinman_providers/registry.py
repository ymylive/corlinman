"""Provider registry — config-driven per Feature C.

Each ``[providers.<name>]`` entry in ``config.toml`` becomes a
:class:`ProviderSpec`. The registry builds one adapter instance per
enabled spec up-front (eagerly) and resolves ``alias_or_model`` strings
to ``(provider, upstream_model_id, merged_params)`` tuples.

Legacy fallback: when the input isn't an alias, we match via the small
:data:`MODEL_PREFIX_DEFAULTS` table and build a default-config provider
on the fly — this preserves the old "just paste ``claude-sonnet-4-5``"
behaviour for callers that haven't migrated to alias configs yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from corlinman_providers.anthropic_provider import AnthropicProvider
from corlinman_providers.base import CorlinmanProvider
from corlinman_providers.china import DeepSeekProvider, GLMProvider, QwenProvider
from corlinman_providers.declarative import (
    DeclarativeProvider,
    DeclarativeProviderSpec,
    load_all_specs,
)
from corlinman_providers.google_provider import GoogleProvider
from corlinman_providers.market_providers import (
    AzureProvider,
    BedrockProvider,
    CohereProvider,
    GroqProvider,
    MistralProvider,
    ReplicateProvider,
    TogetherProvider,
)
from corlinman_providers.openai_compatible import OpenAICompatibleProvider
from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import AliasEntry, ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)


# Map ProviderKind → adapter class. Every first-party kind is wired here;
# ``openai_compatible`` plus the seven market kinds added in the
# free-form-providers refactor (Mistral / Cohere / Together / Groq /
# Replicate / Bedrock / Azure) are also dispatched here so any
# ``[providers.<name>]`` entry whose ``kind`` is one of these resolves to
# a concrete adapter without an ad-hoc shim.
_KIND_TO_CLASS: dict[ProviderKind, type[Any]] = {
    ProviderKind.ANTHROPIC: AnthropicProvider,
    ProviderKind.OPENAI: OpenAIProvider,
    ProviderKind.GOOGLE: GoogleProvider,
    ProviderKind.DEEPSEEK: DeepSeekProvider,
    ProviderKind.QWEN: QwenProvider,
    ProviderKind.GLM: GLMProvider,
    ProviderKind.OPENAI_COMPATIBLE: OpenAICompatibleProvider,
    ProviderKind.MISTRAL: MistralProvider,
    ProviderKind.COHERE: CohereProvider,
    ProviderKind.TOGETHER: TogetherProvider,
    ProviderKind.GROQ: GroqProvider,
    ProviderKind.REPLICATE: ReplicateProvider,
    # Bedrock + Azure raise NotImplementedError at build time — config
    # validation accepts them so operators can declare intent, but the
    # runtime fails loudly until proper SigV4 / deployment-routing lands.
    ProviderKind.BEDROCK: BedrockProvider,
    ProviderKind.AZURE: AzureProvider,
}


# Legacy prefix fallback. Used ONLY when ``resolve()`` gets a raw model id
# that doesn't match any alias. The class matched here is built with its
# default constructor so existing ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``
# env vars keep working even when the config-driven path is active.
MODEL_PREFIX_DEFAULTS: list[tuple[str, type[Any]]] = [
    ("claude-", AnthropicProvider),
    ("gpt-", OpenAIProvider),
    ("o1-", OpenAIProvider),
    ("o3-", OpenAIProvider),
    ("gemini-", GoogleProvider),
    ("deepseek-", DeepSeekProvider),
    ("qwen", QwenProvider),
    ("qwq-", QwenProvider),
    ("glm-", GLMProvider),
]


#: Default location for declarative ``*.toml`` specs. Resolved relative to
#: the package root so operators drop files next to ``src/`` without
#: touching ``PYTHONPATH``.
DEFAULT_SPEC_DIR: Path = Path(__file__).resolve().parent.parent.parent / "spec"


class ProviderRegistry:
    """Eagerly-built registry of provider adapters keyed by spec name."""

    def __init__(
        self,
        specs: list[ProviderSpec] | None = None,
        *,
        declarative_specs: list[DeclarativeProviderSpec] | None = None,
        spec_dir: Path | None = None,
    ) -> None:
        """Build each enabled spec. Disabled specs are retained for listing.

        ``specs=None`` constructs an empty registry — every call then falls
        through to the :data:`MODEL_PREFIX_DEFAULTS` legacy path. Kept
        default-able so pre-Feature-C callers that did ``ProviderRegistry()``
        still work.

        ``declarative_specs`` lets callers pass pre-loaded
        :class:`DeclarativeProviderSpec` values (used by tests). When
        ``None``, we scan ``spec_dir`` (falling back to
        :data:`DEFAULT_SPEC_DIR`) for ``*.toml`` files. Class-based
        providers are built first; any TOML spec whose ``id`` collides
        with an already-built provider is dropped with a WARNING —
        class-based wins so operators can't accidentally shadow a
        vetted built-in adapter.
        """
        specs = specs or []
        self._specs: dict[str, ProviderSpec] = {s.name: s for s in specs}
        self._providers: dict[str, CorlinmanProvider] = {}
        # Cache of legacy-fallback providers keyed by adapter class.
        self._legacy_cache: dict[type[Any], CorlinmanProvider] = {}
        # Declarative specs are retained for admin-listing alongside
        # class-based ``_specs``; distinct dict because the shape differs.
        self._declarative_specs: dict[str, DeclarativeProviderSpec] = {}
        for spec in specs:
            if not spec.enabled:
                continue
            cls = _KIND_TO_CLASS.get(spec.kind)
            if cls is None:
                logger.warning("provider.unknown_kind", name=spec.name, kind=spec.kind)
                continue
            try:
                self._providers[spec.name] = cls.build(spec)
            except Exception as exc:
                logger.error(
                    "provider.build_failed",
                    name=spec.name,
                    kind=spec.kind,
                    error=str(exc),
                )

        # Declarative TOML specs — loaded *after* class-based so conflicts
        # resolve in favour of the built-in adapter.
        self._ingest_declarative(declarative_specs, spec_dir)

    def _ingest_declarative(
        self,
        declarative_specs: list[DeclarativeProviderSpec] | None,
        spec_dir: Path | None,
    ) -> None:
        """Build :class:`DeclarativeProvider` instances from TOML specs.

        Honours the class-based-wins conflict policy: if a TOML spec's
        ``id`` matches an already-built provider name, skip + warn.
        """
        if declarative_specs is None:
            resolved_dir = spec_dir or DEFAULT_SPEC_DIR
            declarative_specs = load_all_specs(resolved_dir)
        for dspec in declarative_specs:
            if dspec.id in self._providers:
                logger.warning(
                    "provider.declarative_conflict",
                    id=dspec.id,
                    reason="class-based provider already registered; TOML spec ignored",
                )
                continue
            try:
                self._providers[dspec.id] = DeclarativeProvider(dspec)
                self._declarative_specs[dspec.id] = dspec
            except Exception as exc:
                logger.error(
                    "provider.declarative_build_failed",
                    id=dspec.id,
                    error=str(exc),
                )

    def list_declarative_specs(self) -> list[DeclarativeProviderSpec]:
        """Return every TOML-declared spec that actually built successfully."""
        return list(self._declarative_specs.values())

    def list_specs(self) -> list[ProviderSpec]:
        """Return all specs (enabled + disabled) for ``/admin/providers``."""
        return list(self._specs.values())

    def get(self, name: str) -> CorlinmanProvider | None:
        """Return the built provider for ``name`` or ``None`` if not built."""
        return self._providers.get(name)

    def resolve(
        self,
        alias_or_model: str,
        *,
        aliases: Mapping[str, AliasEntry] | None = None,
    ) -> tuple[CorlinmanProvider, str, dict[str, Any]]:
        """Resolve a user-supplied string to a provider + upstream model + merged params.

        Order:
          1. If ``alias_or_model`` is a key in ``aliases``, route to that
             alias's ``provider`` + ``model`` and merge
             ``providers.<name>.params`` under ``alias.params``.
          2. Otherwise treat the input as a raw upstream model id and match
             it against :data:`MODEL_PREFIX_DEFAULTS`. Returns the legacy
             adapter with an empty params map.
          3. If neither hits, raise :class:`KeyError`.
        """
        aliases = aliases or {}

        alias = aliases.get(alias_or_model)
        if alias is not None:
            provider = self._providers.get(alias.provider)
            if provider is None:
                raise KeyError(
                    f"alias {alias_or_model!r} references unknown or disabled "
                    f"provider {alias.provider!r}"
                )
            spec = self._specs.get(alias.provider)
            provider_params: dict[str, Any] = dict(spec.params) if spec else {}
            merged = _merge_params(provider_params, alias.params)
            return provider, alias.model, merged

        # Configured-provider fallback — raw model id. This keeps config-driven
        # deployments on their declared base_url/api_key even when callers use
        # the provider's model id directly instead of a named alias.
        for spec in self._specs.values():
            provider = self._providers.get(spec.name)
            cls = _KIND_TO_CLASS.get(spec.kind)
            if provider is None or cls is None:
                continue
            if cls.supports(alias_or_model):
                return provider, alias_or_model, dict(spec.params)

        # Legacy fallback — raw model id.
        for prefix, cls in MODEL_PREFIX_DEFAULTS:
            if alias_or_model.startswith(prefix):
                if cls not in self._legacy_cache:
                    self._legacy_cache[cls] = cls()
                return self._legacy_cache[cls], alias_or_model, {}

        raise KeyError(f"no provider registered for {alias_or_model!r}")


def _merge_params(
    provider_params: Mapping[str, Any],
    alias_params: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge provider defaults under alias overrides (alias wins).

    Request-level overrides are layered on top **by the caller** (usually
    the reasoning loop), so this function only does the provider ≺ alias
    half. Shallow merge — nested dicts are replaced, not deep-merged, to
    keep semantics obvious.
    """
    merged: dict[str, Any] = dict(provider_params)
    merged.update(alias_params)
    return merged


# ---- Legacy module-level singleton (kept for backward compat) --------------

# Pre-Feature-C callers used ``from corlinman_providers import resolve`` and
# passed raw model ids. We keep a specs-less registry around so that path
# keeps working — every hit falls through to the legacy prefix table.

_default = ProviderRegistry([])


def resolve(model: str) -> CorlinmanProvider:
    """Module-level convenience: ``resolve("claude-sonnet-4-5")``.

    Back-compat shim — returns only the provider, drops the model id +
    merged params. New callers should use
    :meth:`ProviderRegistry.resolve` directly.
    """
    provider, _, _ = _default.resolve(alias_or_model=model)
    return provider


__all__ = [
    "MODEL_PREFIX_DEFAULTS",
    "ProviderRegistry",
    "resolve",
]
