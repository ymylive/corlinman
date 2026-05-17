"""Built-in mock (echo) provider — zero-config easy-setup fallback.

Wave 2.2 of the easy-setup plan (``docs/PLAN_EASY_SETUP.md`` §2 W2.2)
introduces a "skip LLM connection" path during onboarding so new users
land on a functional agent without configuring real provider credentials
first. The mock provider plugs into that path:

* It implements :class:`corlinman_providers.base.CorlinmanProvider`, so
  the reasoning loop, registry, alias resolver, and embedding pipeline
  all treat it like any other adapter.
* On chat, it returns a single assistant message containing a fixed
  one-line preamble (``[mock provider — install a real provider via
  /admin/credentials]``) followed by the reverse of the last non-empty
  user-role message in the request. Empty histories degrade gracefully
  to the preamble alone.
* Streaming yields the full response as a single ``token`` chunk
  followed by a terminal ``done`` chunk. There's no incremental delta;
  the surface intentionally mirrors a one-shot echo.
* It never emits tool calls, never raises billing/rate-limit errors,
  and reports zero token / cost telemetry.
* :meth:`embed` returns deterministic zero vectors at the caller's
  requested dimension (default 3072 for the corlinman RAG pipeline),
  so existing embedding callers don't have to special-case the mock
  during onboarding.

The provider is registered with kind :data:`ProviderKind.MOCK` and the
short id ``"mock"``. Operators enable it via a ``[providers.mock]``
TOML block; the onboarding skip endpoint at
``POST /admin/onboard/finalize-skip`` writes that block for new users.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import structlog

from corlinman_providers.base import ProviderChunk
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)


#: Embedded preamble prefix every mock chat response carries. Documented
#: so callers (incl. UI banners) can detect the mock path without sniffing
#: provider identity from the registry.
MOCK_PREAMBLE: str = (
    "[mock provider — install a real provider via /admin/credentials]"
)

#: Default embedding dimension when the caller doesn't pass one through
#: ``extra={"dimension": N}``. Matches the corlinman RAG pipeline default
#: so downstream vector stores don't have to special-case the mock.
DEFAULT_EMBEDDING_DIM: int = 3072


class MockProvider:
    """Echo-style provider used by the easy-setup skip path.

    Construction takes no required args; :meth:`build` is provided for
    parity with the other adapter classes so the registry's
    ``cls.build(spec)`` call site works uniformly.
    """

    name: ClassVar[str] = "mock"
    kind: ClassVar[ProviderKind] = ProviderKind.MOCK

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        # Mock accepts but ignores credentials so operators can wire it
        # into the same ``[providers.<name>]`` block shape as real adapters.
        self._api_key = api_key
        self._base_url = base_url

    @classmethod
    def build(cls, spec: ProviderSpec) -> MockProvider:
        """Construct from a :class:`ProviderSpec`. Credentials are ignored."""
        return cls(api_key=spec.api_key, base_url=spec.base_url)

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """Empty schema — the mock provider has no tunable knobs."""
        return _MOCK_PARAMS_SCHEMA

    async def chat_stream(
        self,
        *,
        model: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        """Echo the last user message reversed, with a fixed preamble.

        The full response is delivered as a single ``token`` chunk, then a
        terminal ``done`` chunk with ``finish_reason="stop"``. No tool
        calls are ever produced — the provider exists to demo the chat
        loop end-to-end without an upstream LLM.
        """
        last_user = _extract_last_user_text(messages)
        if last_user:
            body = last_user[::-1]
            response = f"{MOCK_PREAMBLE}\n{body}"
        else:
            response = MOCK_PREAMBLE

        yield ProviderChunk(kind="token", text=response)
        yield ProviderChunk(kind="done", finish_reason="stop")

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        """Return deterministic zero vectors at the requested dimension.

        Dimension resolution order: explicit ``extra["dimension"]`` →
        :data:`DEFAULT_EMBEDDING_DIM`. The vectors are pure zeros so any
        cosine-similarity caller falls through to its tie-break path
        instead of getting nonsense embeddings during onboarding.
        """
        dim = _resolve_dim(extra)
        return [[0.0] * dim for _ in inputs]

    @classmethod
    def supports(cls, model: str) -> bool:
        """Claim the ``mock-*`` prefix and the bare ``mock`` model id.

        Used by :class:`corlinman_providers.registry.ProviderRegistry`
        when callers pass a raw model id instead of a configured alias.
        Conservative on purpose: only the explicit ``mock`` family
        matches so we never accidentally swallow a real provider's id.
        """
        return model == "mock" or model.startswith("mock-")


def _extract_last_user_text(messages: Sequence[Any]) -> str:
    """Return the last non-empty user-role message's text content.

    Accepts both dict-shaped messages and objects with ``role``/``content``
    attributes (matching the structural :class:`ChatMessage` protocol).
    Multipart OpenAI-shape content (list of ``{type, text}`` parts)
    collapses to the concatenation of its ``text`` fragments — the mock
    provider operates on text only.
    """
    for msg in reversed(list(messages)):
        role = _get_field(msg, "role")
        if role != "user":
            continue
        content = _get_field(msg, "content")
        text = _flatten_content(content)
        if text:
            return text
    return ""


def _get_field(msg: Any, key: str) -> Any:
    if isinstance(msg, dict):
        return msg.get(key)
    return getattr(msg, key, None)


def _flatten_content(content: Any) -> str:
    """Coerce OpenAI-shape content (str | list[part]) to a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict):
                txt = part.get("text")
                if isinstance(txt, str):
                    out.append(txt)
            elif isinstance(part, str):
                out.append(part)
        return "".join(out)
    return str(content)


def _resolve_dim(extra: dict[str, Any] | None) -> int:
    if not extra:
        return DEFAULT_EMBEDDING_DIM
    raw = extra.get("dimension")
    if isinstance(raw, int) and raw > 0:
        return raw
    return DEFAULT_EMBEDDING_DIM


_MOCK_PARAMS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {},
    "description": "Mock provider has no tunable knobs.",
}


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "MOCK_PREAMBLE",
    "MockProvider",
]
