"""Google (Gemini) provider adapter.

Wraps ``google.genai`` behind
:class:`corlinman_providers.base.CorlinmanProvider`.

Google's Gemini SDK exposes function calls as structured ``Part`` entries
inside each streamed chunk. Gemini usually delivers the whole parsed call
in one ``Part`` once, so the unified streaming translation is:

    * when a chunk carries a ``function_call`` part: emit
      ``tool_call_start`` + ``tool_call_delta`` (with ``json.dumps(args)``)
      + ``tool_call_end`` back-to-back (no partial aggregation needed);
    * text parts → ``token`` chunks.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import structlog

from corlinman_providers.base import ProviderChunk
from corlinman_providers.failover import CorlinmanError
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)


class GoogleProvider:
    """Google Gemini adapter."""

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
            if tools:
                config["tools"] = _normalise_tools(tools)
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
            synthetic_call_index = 0
            async for chunk in gen:
                text = getattr(chunk, "text", None) or ""
                if text:
                    yield ProviderChunk(kind="token", text=text)
                for function_call in _iter_function_calls(chunk):
                    finish = "tool_calls"
                    call_id = _get(function_call, "id")
                    if not call_id:
                        call_id = f"call_{synthetic_call_index}"
                        synthetic_call_index += 1
                    name = _get(function_call, "name") or ""
                    args = _get(function_call, "args") or {}
                    yield ProviderChunk(
                        kind="tool_call_start",
                        tool_call_id=call_id,
                        tool_name=name,
                    )
                    yield ProviderChunk(
                        kind="tool_call_delta",
                        tool_call_id=call_id,
                        arguments_delta=json.dumps(_jsonable(args)),
                    )
                    yield ProviderChunk(kind="tool_call_end", tool_call_id=call_id)
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


def _get_any(obj: Any, *keys: str) -> Any:
    for key in keys:
        value = _get(obj, key)
        if value is not None:
            return value
    return None


def _iter_function_calls(chunk: Any) -> list[Any]:
    direct_calls = getattr(chunk, "function_calls", None)
    if direct_calls:
        return list(direct_calls)

    calls: list[Any] = []
    parts = getattr(chunk, "parts", None)
    if parts is None:
        parts = []
        for candidate in getattr(chunk, "candidates", None) or []:
            content = _get(candidate, "content")
            parts.extend(_get(content, "parts") or [])

    for part in parts:
        function_call = _get_any(part, "function_call", "functionCall")
        if function_call is not None:
            calls.append(function_call)
    return calls


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _normalise_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    passthrough: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict):
            passthrough.append(tool)
            continue

        declaration: dict[str, Any] = {"name": function.get("name", "")}
        if function.get("description"):
            declaration["description"] = function["description"]
        parameters = function.get("parameters")
        if parameters:
            declaration["parameters"] = parameters
        declarations.append(declaration)

    normalised = list(passthrough)
    if declarations:
        normalised.append({"function_declarations": declarations})
    return normalised


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
