"""Google (Gemini) provider adapter.

Wraps ``google.genai`` behind
:class:`corlinman_providers.base.CorlinmanProvider`.

M2 scope: **text-only streaming**. Google's Gemini SDK exposes function
calls as structured ``Part`` entries inside each streamed chunk; mapping
them to the unified ``tool_call_{start,delta,end}`` shape is strictly more
work than Anthropic/OpenAI because Gemini does not stream argument JSON —
it delivers the whole parsed call in one ``Part`` once. The clean
translation is:

    * when a chunk carries a ``function_call`` part: emit
      ``tool_call_start`` + ``tool_call_delta`` (with ``json.dumps(args)``)
      + ``tool_call_end`` back-to-back (no partial aggregation needed);
    * text parts → ``token`` chunks.

TODO(M3): implement the function-call translation above once a real Gemini
test fixture lands. The current scaffold streams text only and raises a
``NotImplementedError`` if the caller passes ``tools``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import structlog

from corlinman_providers.base import ProviderChunk
from corlinman_providers.failover import CorlinmanError
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)


class GoogleProvider:
    """Google Gemini adapter (text-only for M2; see module docstring TODO)."""

    name: ClassVar[str] = "google"
    kind: ClassVar[ProviderKind] = ProviderKind.GOOGLE

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY") or None

    @classmethod
    def build(cls, spec: ProviderSpec) -> GoogleProvider:
        return cls(api_key=spec.api_key)

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """Per-request params accepted by the Gemini generate_content API.

        Note: google-genai maps ``top_p`` to ``top_p`` inside its
        ``GenerateContentConfig`` — we forward it verbatim via ``extra``.
        ``safety_settings`` is the Gemini-specific escape hatch; declared as
        a free-form object because the SDK validates its own shape.
        """
        return _GOOGLE_PARAMS_SCHEMA

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
        if not self._api_key:
            raise RuntimeError("API key missing: set GOOGLE_API_KEY")
        if tools:
            # TODO(M3): translate Gemini `function_call` parts.
            raise NotImplementedError(
                "Google provider tool_call translation is a TODO — pass tools=None"
            )

        from google import genai  # type: ignore[import-not-found]

        try:
            client = genai.Client(api_key=self._api_key)
            # Gemini wants a single flat prompt; join any history into one
            # string for the text-only path. Multi-turn + roles are in the
            # TODO above.
            prompt_parts: list[str] = []
            for m in messages:
                role = _get(m, "role") or "user"
                content = _get(m, "content") or ""
                if content:
                    prompt_parts.append(f"{role}: {content}")
            prompt = "\n".join(prompt_parts)

            config: dict[str, Any] = {}
            if temperature is not None:
                config["temperature"] = temperature
            if max_tokens:
                config["max_output_tokens"] = max_tokens
            if extra:
                config.update(extra)

            gen = await client.aio.models.generate_content_stream(
                model=model,
                contents=prompt,
                # google-genai accepts a plain dict at runtime but declares
                # a stricter ``GenerateContentConfig | GenerateContentConfigDict``
                # in its stubs; M3 will switch to the typed config builder.
                config=config or None,  # type: ignore[arg-type]
            )
            finish = "stop"
            async for chunk in gen:
                text = getattr(chunk, "text", None) or ""
                if text:
                    yield ProviderChunk(kind="token", text=text)
            yield ProviderChunk(kind="done", finish_reason=finish)
        except CorlinmanError:
            raise
        except Exception as exc:
            raise CorlinmanError(str(exc), provider="google", model=model) from exc

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        raise NotImplementedError("Google embeddings land with the RAG pipeline in M3")

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith("gemini-")


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# Hand-authored JSON Schema (draft 2020-12). ``safety_settings`` is
# free-form: the google-genai SDK validates its internal shape and we don't
# want to duplicate that here — declare as an object with no constraints.
_GOOGLE_PARAMS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "temperature": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 2.0,
            "description": "Sampling temperature.",
        },
        "top_p": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Nucleus sampling probability mass.",
        },
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "description": "max_output_tokens in Gemini terminology.",
        },
        "system_prompt": {
            "type": "string",
            "maxLength": 16000,
            "description": "System instruction; concatenated with any history.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "description": "Client-side request timeout in milliseconds.",
        },
        "safety_settings": {
            "type": "object",
            "additionalProperties": True,
            "description": "Forwarded verbatim to google-genai (shape validated by SDK).",
        },
    },
}
