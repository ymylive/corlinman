"""OpenAI provider adapter.

Wraps :class:`openai.AsyncOpenAI` behind
:class:`corlinman_providers.base.CorlinmanProvider`; also used as the base
implementation for OpenAI-compatible endpoints (DeepSeek, Qwen DashScope,
GLM) which just vary ``base_url`` and auth.

Tool-call handling (plan §14 R5): the OpenAI chat-completion stream emits
``choices[0].delta.tool_calls[]`` with one entry per new or in-progress
tool call. Each entry carries an ``index``; successive deltas for the same
index append to the same call's ``function.arguments`` buffer. We track
whether we've seen a call's ``id`` yet — the **first** chunk for a given
index carries the ``id`` + ``function.name``, and we emit
``tool_call_start`` the first time we see it. Argument fragments flow
through as ``tool_call_delta``. When the terminal chunk's
``finish_reason == "tool_calls"`` arrives, we emit ``tool_call_end`` for
every open call before the final ``done`` chunk.

Tested against ``openai==2.32``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import structlog

from corlinman_providers.base import ProviderChunk
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
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)


class OpenAIProvider:
    """OpenAI adapter (and base for OpenAI-compatible endpoints)."""

    name: ClassVar[str] = "openai"
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        env_key: str = "OPENAI_API_KEY",
    ) -> None:
        self._api_key = api_key or os.environ.get(env_key) or None
        self._base_url = base_url

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAIProvider:
        """Construct from a :class:`ProviderSpec`.

        Falls back to the ``OPENAI_API_KEY`` env var when the spec omits one
        — matches the historic constructor behaviour so existing envs keep
        working even when the new config path is active.
        """
        return cls(
            api_key=spec.api_key,
            base_url=spec.base_url,
        )

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """JSON Schema (draft 2020-12) for per-request params.

        Covers the portable chat-completion knobs plus the ``reasoning_effort``
        escape hatch for the ``o1``/``o3`` reasoning family (forwarded via
        ``extra``; ignored by models that don't accept it).
        """
        return _OPENAI_PARAMS_SCHEMA

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
            raise RuntimeError(f"API key missing for provider {self.name}")

        from openai import AsyncOpenAI  # type: ignore[import-not-found]

        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        client = AsyncOpenAI(**client_kwargs)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [_normalise_message(m) for m in messages],
            "stream": True,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = list(tools)
        if extra:
            kwargs.update(extra)

        # index → (call_id, emitted_start). We emit `tool_call_start` at most
        # once per index and always close with `tool_call_end`.
        open_calls: dict[int, tuple[str, bool]] = {}
        finish_reason = "stop"

        try:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                finish = getattr(choice, "finish_reason", None)

                if delta is not None:
                    text = getattr(delta, "content", None)
                    if text:
                        yield ProviderChunk(kind="token", text=text)

                    tool_deltas = getattr(delta, "tool_calls", None) or []
                    for td in tool_deltas:
                        idx = getattr(td, "index", 0) or 0
                        tc_id = getattr(td, "id", None)
                        fn = getattr(td, "function", None)
                        fn_name = getattr(fn, "name", None) if fn else None
                        fn_args = getattr(fn, "arguments", None) if fn else None

                        # First sighting of this index → open the call.
                        if idx not in open_calls:
                            call_id = tc_id or f"call_{idx}"
                            open_calls[idx] = (call_id, True)
                            yield ProviderChunk(
                                kind="tool_call_start",
                                tool_call_id=call_id,
                                tool_name=fn_name or "",
                            )
                        elif tc_id and not open_calls[idx][1]:
                            # Late id — shouldn't happen in practice, guard
                            # anyway so we always emit `start` before `delta`.
                            open_calls[idx] = (tc_id, True)
                            yield ProviderChunk(
                                kind="tool_call_start",
                                tool_call_id=tc_id,
                                tool_name=fn_name or "",
                            )

                        call_id = open_calls[idx][0]
                        if fn_args:
                            yield ProviderChunk(
                                kind="tool_call_delta",
                                tool_call_id=call_id,
                                arguments_delta=fn_args,
                            )

                if finish is not None:
                    # Close any still-open tool calls before the terminal done.
                    for call_id, _ in open_calls.values():
                        yield ProviderChunk(
                            kind="tool_call_end",
                            tool_call_id=call_id,
                        )
                    open_calls.clear()
                    finish_reason = _map_finish_reason(finish)
                    break
        except CorlinmanError:
            raise
        except Exception as exc:
            raise _map_openai_error(exc, model=model, provider=self.name) from exc

        yield ProviderChunk(kind="done", finish_reason=finish_reason)

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        # TODO(M3): implement via client.embeddings.create.
        raise NotImplementedError("OpenAIProvider.embed lands in M3")

    @classmethod
    def supports(cls, model: str) -> bool:
        """Claim ``gpt-*`` / ``o1-*`` / ``o3-*`` model ids."""
        return (
            model.startswith("gpt-")
            or model.startswith("o1-")
            or model.startswith("o3-")
            or model == "gpt-3.5-turbo"
        )


def _normalise_message(m: Any) -> dict[str, Any]:
    """Accept both dicts and objects with ``role``/``content`` attributes."""
    if isinstance(m, dict):
        return m
    out: dict[str, Any] = {
        "role": getattr(m, "role", "user"),
        "content": getattr(m, "content", "") or "",
    }
    name = getattr(m, "name", None)
    if name:
        out["name"] = name
    tool_call_id = getattr(m, "tool_call_id", None)
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    return out


def _map_finish_reason(reason: str | None) -> str:
    """Normalise OpenAI ``finish_reason`` values.

    OpenAI already emits ``stop`` / ``length`` / ``tool_calls`` verbatim; we
    keep the same surface. ``content_filter`` and ``function_call`` (legacy)
    collapse to ``stop`` so the downstream reasoning loop has a stable set.
    """
    if reason in ("stop", "length", "tool_calls"):
        return reason
    return "stop"


def _map_openai_error(exc: Exception, *, model: str, provider: str) -> CorlinmanError:
    """Coerce any OpenAI SDK exception into a :class:`CorlinmanError` subtype."""
    try:
        from openai import (  # type: ignore[import-not-found]
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            NotFoundError,
            PermissionDeniedError,
        )
        from openai import (
            RateLimitError as OaRateLimit,
        )
    except Exception:  # pragma: no cover
        return CorlinmanError(str(exc), provider=provider, model=model)

    ctx: dict[str, Any] = {"provider": provider, "model": model}
    if isinstance(exc, OaRateLimit):
        return RateLimitError(str(exc), status_code=429, **ctx)
    if isinstance(exc, APITimeoutError):
        return TimeoutError(str(exc), **ctx)
    if isinstance(exc, AuthenticationError):
        return AuthError(str(exc), status_code=401, **ctx)
    if isinstance(exc, PermissionDeniedError):
        return AuthPermanentError(str(exc), status_code=403, **ctx)
    if isinstance(exc, NotFoundError):
        return ModelNotFoundError(str(exc), status_code=404, **ctx)
    if isinstance(exc, BadRequestError):
        msg = str(exc).lower()
        if "quota" in msg or "billing" in msg or "credit" in msg:
            return BillingError(str(exc), status_code=402, **ctx)
        if "context" in msg or "too long" in msg or "maximum context" in msg:
            return ContextOverflowError(str(exc), status_code=400, **ctx)
        return FormatError(str(exc), status_code=400, **ctx)
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status in (503, 529):
            return OverloadedError(str(exc), status_code=status, **ctx)
        if status == 429:
            return RateLimitError(str(exc), status_code=status, **ctx)
        if status in (401, 403):
            return AuthError(str(exc), status_code=status, **ctx)
        if status == 404:
            return ModelNotFoundError(str(exc), status_code=status, **ctx)
        return CorlinmanError(str(exc), status_code=status, **ctx)
    return CorlinmanError(str(exc), **ctx)


# Hand-authored JSON Schema (draft 2020-12). Kept tight per the contract:
# common knobs as a slider-friendly ``number`` with bounds, plus the one
# OpenAI-family-specific extra (``reasoning_effort``).
_OPENAI_PARAMS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "temperature": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 2.0,
            "description": "Sampling temperature. 0 = deterministic.",
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
            "description": "Maximum tokens in the completion.",
        },
        "system_prompt": {
            "type": "string",
            "maxLength": 16000,
            "description": "System message prepended to the conversation.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "description": "Client-side request timeout in milliseconds.",
        },
        "reasoning_effort": {
            "type": "string",
            "enum": ["minimal", "low", "medium", "high"],
            "description": "o1/o3-family reasoning effort hint.",
        },
    },
}
