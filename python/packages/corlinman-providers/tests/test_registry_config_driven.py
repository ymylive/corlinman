"""Tests for Feature C's config-driven ``ProviderRegistry``.

Coverage:
  (a) every :class:`ProviderKind` resolves to the right adapter when
      declared as a spec;
  (b) params merge order is ``provider.params`` < ``alias.params``
      (request-level overrides happen in the reasoning loop, not here);
  (c) raw model ids that don't appear in ``aliases`` still resolve via the
      legacy prefix fallback;
  (d) ``openai_compatible`` requires ``base_url``.
"""

from __future__ import annotations

import pytest
from corlinman_providers import (
    AliasEntry,
    AnthropicProvider,
    DeepSeekProvider,
    GLMProvider,
    GoogleProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderKind,
    ProviderRegistry,
    ProviderSpec,
    QwenProvider,
)


def _spec(
    name: str,
    kind: ProviderKind,
    *,
    api_key: str | None = "sk-test",
    base_url: str | None = None,
    params: dict | None = None,
) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        kind=kind,
        api_key=api_key,
        base_url=base_url,
        enabled=True,
        params=params or {},
    )


@pytest.mark.parametrize(
    "kind, expected_cls, base_url",
    [
        (ProviderKind.ANTHROPIC, AnthropicProvider, None),
        (ProviderKind.OPENAI, OpenAIProvider, None),
        (ProviderKind.GOOGLE, GoogleProvider, None),
        (ProviderKind.DEEPSEEK, DeepSeekProvider, None),
        (ProviderKind.QWEN, QwenProvider, None),
        (ProviderKind.GLM, GLMProvider, None),
        (ProviderKind.OPENAI_COMPATIBLE, OpenAICompatibleProvider, "http://localhost:8000/v1"),
    ],
)
def test_registry_builds_each_kind(
    kind: ProviderKind, expected_cls: type, base_url: str | None
) -> None:
    """Every enum variant must yield a built provider of the right class."""
    name = f"test-{kind.value}"
    spec = _spec(name, kind, base_url=base_url)
    reg = ProviderRegistry([spec])

    provider = reg.get(name)
    assert provider is not None
    assert isinstance(provider, expected_cls)


def test_registry_skips_disabled_specs() -> None:
    """Disabled specs are retained for listing but no provider is built."""
    spec = _spec("disabled", ProviderKind.OPENAI)
    spec.enabled = False
    reg = ProviderRegistry([spec])

    assert reg.get("disabled") is None
    assert [s.name for s in reg.list_specs()] == ["disabled"]


def test_resolve_via_alias_returns_merged_params() -> None:
    """Alias params override provider params (alias wins)."""
    spec = _spec(
        "openai-main",
        ProviderKind.OPENAI,
        params={"temperature": 0.2, "timeout_ms": 30_000},
    )
    reg = ProviderRegistry([spec])
    aliases = {
        "fast": AliasEntry(
            provider="openai-main",
            model="gpt-4o-mini",
            params={"temperature": 0.9, "top_p": 0.95},
        )
    }

    provider, model, merged = reg.resolve(alias_or_model="fast", aliases=aliases)

    assert isinstance(provider, OpenAIProvider)
    assert model == "gpt-4o-mini"
    # alias.temperature (0.9) wins over provider.temperature (0.2)
    assert merged["temperature"] == pytest.approx(0.9)
    # provider-level key flows through when alias doesn't override it
    assert merged["timeout_ms"] == 30_000
    # alias-only keys are present
    assert merged["top_p"] == pytest.approx(0.95)


def test_resolve_legacy_prefix_fallback() -> None:
    """Raw model id not in aliases matches the legacy prefix table."""
    reg = ProviderRegistry([])  # no specs!
    provider, model, merged = reg.resolve(
        alias_or_model="claude-sonnet-4-5", aliases={}
    )
    assert isinstance(provider, AnthropicProvider)
    assert model == "claude-sonnet-4-5"
    assert merged == {}


def test_resolve_raw_model_prefers_configured_provider() -> None:
    """Configured providers must handle matching raw model ids before legacy fallback."""
    spec = _spec(
        "openai-main",
        ProviderKind.OPENAI,
        base_url="https://gateway.example/v1",
        params={"timeout_ms": 60_000},
    )
    reg = ProviderRegistry([spec])

    provider, model, merged = reg.resolve(alias_or_model="gpt-5.5", aliases={})

    assert provider is reg.get("openai-main")
    assert model == "gpt-5.5"
    assert merged["timeout_ms"] == 60_000


def test_resolve_raises_on_unknown_raw_id() -> None:
    reg = ProviderRegistry([])
    with pytest.raises(KeyError):
        reg.resolve(alias_or_model="llama-never-registered", aliases={})


def test_resolve_alias_pointing_to_disabled_provider_raises() -> None:
    spec = _spec("ghost", ProviderKind.OPENAI)
    spec.enabled = False
    reg = ProviderRegistry([spec])

    aliases = {"broken": AliasEntry(provider="ghost", model="gpt-4o")}
    with pytest.raises(KeyError, match="disabled provider"):
        reg.resolve(alias_or_model="broken", aliases=aliases)


def test_openai_compatible_requires_base_url() -> None:
    """``openai_compatible`` specs without a base_url must fail to build."""
    spec = _spec("local-vllm", ProviderKind.OPENAI_COMPATIBLE, base_url=None)
    # Build runs inside __init__; the failure is caught + logged; provider
    # is absent from the registry.
    reg = ProviderRegistry([spec])
    assert reg.get("local-vllm") is None


def test_openai_compatible_honours_user_chosen_name() -> None:
    """The ``name`` instance attribute reflects the user-given spec name."""
    spec = _spec(
        "my-local-gateway",
        ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://localhost:8000/v1",
    )
    reg = ProviderRegistry([spec])
    provider = reg.get("my-local-gateway")
    assert provider is not None
    assert provider.name == "my-local-gateway"


def test_params_schema_per_provider_has_required_common_keys() -> None:
    """Every provider declares the ``temperature`` / ``max_tokens`` keys."""
    for cls in (
        AnthropicProvider,
        OpenAIProvider,
        GoogleProvider,
        DeepSeekProvider,
        QwenProvider,
        GLMProvider,
        OpenAICompatibleProvider,
    ):
        schema = cls.params_schema()
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "temperature" in props
        assert "max_tokens" in props
        assert "timeout_ms" in props


def test_legacy_module_level_resolve_still_works() -> None:
    """Back-compat: ``corlinman_providers.resolve(model)`` returns a provider."""
    from corlinman_providers import resolve

    assert isinstance(resolve("claude-sonnet-4-5"), AnthropicProvider)
    assert isinstance(resolve("gpt-4o-mini"), OpenAIProvider)
    assert isinstance(resolve("gemini-2.0-flash"), GoogleProvider)
