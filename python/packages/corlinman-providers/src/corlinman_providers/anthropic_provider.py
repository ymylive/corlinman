"""Anthropic provider adapter.

Wraps :class:`anthropic.AsyncAnthropic` behind
:class:`corlinman_providers.base.CorlinmanProvider`, maps vendor errors to
the :mod:`corlinman_providers.failover` hierarchy, and streams deltas as
:class:`ProviderChunk` values.

Tool-call handling (plan §14 R5): we listen for Anthropic's
``content_block_start`` / ``content_block_delta`` / ``content_block_stop``
events. When the starting content block is a ``tool_use``, we emit
``tool_call_start`` / ``tool_call_delta`` / ``tool_call_end`` chunks
mirroring the OpenAI-standard ``tool_calls`` surface. Text blocks become
ordinary ``token`` chunks. OpenAI-compatible tool_use blocks only.

Tested against ``anthropic==0.96`` (the ``messages.stream()`` raw-event API
stabilised in the 0.40+ line; we use ``event.type`` string tags rather than
``isinstance`` so minor SDK bumps don't break the adapter).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

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

logger = structlog.get_logger(__name__)


class AnthropicProvider:
    """Anthropic adapter.

    Instantiate with ``AnthropicProvider()`` (default) or
    ``AnthropicProvider(api_key="...")``. Calls lazily construct
    ``anthropic.AsyncAnthropic`` so import-time failures stay benign.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or None

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
        """Stream a chat completion via ``anthropic.messages.stream``.

        Raises :class:`RuntimeError` when no API key is configured —
        surfacing config gaps early instead of silent failure.
        """
        if not self._api_key:
            raise RuntimeError("API key missing: set ANTHROPIC_API_KEY")

        # Imported lazily so test environments without the SDK still import this
        # module (and so importing the module doesn't require network).
        from anthropic import AsyncAnthropic  # type: ignore[import-not-found]

        client = AsyncAnthropic(api_key=self._api_key)
        system, anthropic_messages = _split_system(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens if max_tokens else 1024,
        }
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = list(tools)
        if extra:
            kwargs.update(extra)

        try:
            async with client.messages.stream(**kwargs) as stream:
                # Per-block state: which content blocks are tool_use vs text.
                open_tool_ids: dict[int, str] = {}
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        idx = getattr(event, "index", 0)
                        if getattr(block, "type", None) == "tool_use":
                            call_id = getattr(block, "id", "") or ""
                            name = getattr(block, "name", "") or ""
                            open_tool_ids[idx] = call_id
                            yield ProviderChunk(
                                kind="tool_call_start",
                                tool_call_id=call_id,
                                tool_name=name,
                            )
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", None)
                        idx = getattr(event, "index", 0)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "") or ""
                            if text:
                                yield ProviderChunk(kind="token", text=text)
                        elif dtype == "input_json_delta":
                            partial = getattr(delta, "partial_json", "") or ""
                            call_id = open_tool_ids.get(idx, "")
                            if call_id:
                                yield ProviderChunk(
                                    kind="tool_call_delta",
                                    tool_call_id=call_id,
                                    arguments_delta=partial,
                                )
                    elif etype == "content_block_stop":
                        idx = getattr(event, "index", 0)
                        call_id = open_tool_ids.pop(idx, None)
                        if call_id:
                            yield ProviderChunk(
                                kind="tool_call_end",
                                tool_call_id=call_id,
                            )
                    # Other event types (message_start, message_delta,
                    # message_stop) carry only accounting data we pick up via
                    # get_final_message below.
                final = await stream.get_final_message()
                finish = _map_stop_reason(getattr(final, "stop_reason", None))
                yield ProviderChunk(kind="done", finish_reason=finish)
        except CorlinmanError:
            raise
        except Exception as exc:
            raise _map_anthropic_error(exc, model=model) from exc

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        raise NotImplementedError("Anthropic has no embedding API — route to OpenAI / local")

    @classmethod
    def supports(cls, model: str) -> bool:
        """Claim any model id starting with ``claude-``."""
        return model.startswith("claude-")


def _split_system(messages: Sequence[Any]) -> tuple[str | None, list[dict[str, str]]]:
    """Split out ``role="system"`` messages — Anthropic takes ``system`` as a
    top-level parameter rather than an entry in ``messages``."""
    system_parts: list[str] = []
    chat: list[dict[str, str]] = []
    for m in messages:
        role = _get(m, "role")
        content = _get(m, "content") or ""
        if role == "system":
            if content:
                system_parts.append(content)
        else:
            # Anthropic requires role in {"user", "assistant"}; collapse "tool" for now.
            anth_role = "user" if role in ("user", "tool") else "assistant"
            chat.append({"role": anth_role, "content": content})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _map_stop_reason(reason: str | None) -> str:
    """Map Anthropic ``stop_reason`` to our normalised finish_reason set."""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    return mapping.get(reason or "", "stop")


def _map_anthropic_error(exc: Exception, *, model: str) -> CorlinmanError:
    """Coerce any vendor SDK exception into a :class:`CorlinmanError` subtype."""
    # Late import keeps module safe when anthropic isn't installed.
    try:
        from anthropic import (  # type: ignore[import-not-found]
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            NotFoundError,
            PermissionDeniedError,
        )
        from anthropic import (
            RateLimitError as AnthRateLimit,
        )
    except Exception:  # pragma: no cover — import-time guard
        return CorlinmanError(str(exc), provider="anthropic", model=model)

    ctx: dict[str, Any] = {"provider": "anthropic", "model": model}
    if isinstance(exc, AnthRateLimit):
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
        if "credit" in msg or "billing" in msg or "quota" in msg:
            return BillingError(str(exc), status_code=402, **ctx)
        if "context" in msg or "too long" in msg or "tokens" in msg:
            return ContextOverflowError(str(exc), status_code=400, **ctx)
        return FormatError(str(exc), status_code=400, **ctx)
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status == 503 or status == 529:
            return OverloadedError(str(exc), status_code=status, **ctx)
        if status == 429:
            return RateLimitError(str(exc), status_code=status, **ctx)
        if status in (401, 403):
            return AuthError(str(exc), status_code=status, **ctx)
        if status == 404:
            return ModelNotFoundError(str(exc), status_code=status, **ctx)
        return CorlinmanError(str(exc), status_code=status, **ctx)
    return CorlinmanError(str(exc), **ctx)
