"""OpenAI-compatible provider — the escape hatch for vLLM / Ollama / etc.

Any gateway that implements the OpenAI wire format can be wired up as a
:class:`[providers.<name>] kind = "openai_compatible"` entry pointing to
its own ``base_url``. The behaviour is identical to
:class:`OpenAIProvider` — only ``kind`` and ``name`` differ, and the
``base_url`` is **required** (validated at build time) rather than
defaulted to ``api.openai.com``.

Feature C (§1 of the contract) treats this as a first-class provider kind
so the admin UI can distinguish "built-in OpenAI" from "bring-your-own
OpenAI-wire-format gateway".
"""

from __future__ import annotations

from typing import Any, ClassVar

from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


class OpenAICompatibleProvider(OpenAIProvider):
    """Bring-your-own OpenAI-wire-format provider.

    Instantiate via :meth:`build` from a spec whose ``kind`` is
    ``openai_compatible`` and whose ``base_url`` is set.
    """

    # ``name`` stays instance-settable so the registry can stamp it from
    # the spec (users pick their own names for local gateways).
    name: ClassVar[str] = "openai_compatible"
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI_COMPATIBLE

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        instance_name: str | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("openai_compatible provider requires a base_url")
        super().__init__(api_key=api_key, base_url=base_url)
        # Shadow the class-level ``name`` so registry lookups (and the
        # logger attr below) report the user-chosen name. mypy complains
        # about re-assigning a ``ClassVar``, so we set it via __dict__.
        if instance_name:
            self.__dict__["name"] = instance_name

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        if not spec.base_url:
            raise ValueError(
                f"openai_compatible provider {spec.name!r} requires base_url in config"
            )
        return cls(
            base_url=spec.base_url,
            api_key=spec.api_key,
            instance_name=spec.name,
        )

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """Same schema as :class:`OpenAIProvider` — pure OpenAI wire."""
        return OpenAIProvider.params_schema()

    @classmethod
    def supports(cls, model: str) -> bool:
        # openai_compatible never claims a model via the legacy prefix
        # fallback — it's always addressed explicitly via an alias.
        return False
