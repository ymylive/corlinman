"""``POST /v1/chat/completions`` — OpenAI-compatible chat entry point.

Python port of ``rust/crates/corlinman-gateway/src/routes/chat.rs``.
The Rust file is the largest of the gateway routes (~2000 LoC) and
covers: model-alias resolution, request validation, session-history
load/persist, gRPC streaming bridge, OpenAI-shape SSE rendering,
tool-call placeholder ack, approval-gate wrapping.

In the Python plane the gRPC bridge collapses to the in-process
:class:`corlinman_server.gateway_api.ChatService` Protocol (W1) —
events arrive as an ``AsyncIterator`` of
:class:`~corlinman_server.gateway_api.InternalChatEvent` values. The
HTTP handler is responsible for:

* Request validation (``model`` + ``messages`` non-empty).
* Model-alias / unknown-model fallback (mirrors the Rust
  :class:`ModelRedirect` semantics).
* Session-key resolution: body wins over the
  ``X-Session-Key`` header. The handler doesn't persist sessions in
  this milestone — the in-process :class:`ChatService` impl already
  owns session storage in Python.
* Dispatching to :class:`ChatService.run` and rendering the resulting
  event stream as OpenAI-shaped SSE (``stream=true``) or a
  single-shot JSON body (``stream=false``).

Tool-call execution remains the gateway's responsibility in Rust; in
Python the :class:`ChatService` implementation already executes
tools internally (the gateway just observes
:class:`ToolCallEvent`s) so we surface them to the SSE consumer in
the OpenAI standard form and otherwise leave the loop alone.

See :class:`ChatState` for the wiring surface and
:func:`router` for the FastAPI APIRouter factory.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from corlinman_server.gateway_api import (
    ChatService,
    DoneEvent,
    ErrorEvent,
    InternalChatRequest,
    Message,
    Role,
    TokenDeltaEvent,
    ToolCallEvent,
)
from corlinman_server.gateway_api.types import InternalChatEvent

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatState",
    "ModelRedirect",
    "ResolvedModel",
    "apply_model_aliases",
    "router",
]


# ─── Request / response shapes ───────────────────────────────────────


class ChatMessage(BaseModel):
    """OpenAI-shaped chat message. Mirrors the Rust ``ChatMessage`` struct."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    """OpenAI-compatible chat request body.

    Mirrors the Rust ``ChatRequest`` field-for-field. ``tools`` is
    typed as ``Any`` because the gateway treats it opaquely and hands
    it through to the reasoning loop. Extra fields are allowed so
    OpenAI clients that send ``user`` / ``logit_bias`` etc. don't
    400 — they're just ignored.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: object | None = None
    session_key: str | None = None


# ─── Model redirect ──────────────────────────────────────────────────


@dataclass(slots=True)
class ModelRedirect:
    """Alias / unknown-model fallback bundle.

    Mirrors the Rust ``ModelRedirect`` struct + its
    :func:`apply_model_aliases` resolution order.
    """

    aliases: dict[str, str] = field(default_factory=dict)
    default: str = ""
    known_models: set[str] = field(default_factory=set)


@dataclass(slots=True)
class ResolvedModel:
    """Outcome of :func:`apply_model_aliases`. ``kind`` discriminates the four
    cases the Rust enum surfaces: ``aliased`` / ``passthrough`` /
    ``fallback_default`` / ``unknown_no_default``.
    """

    kind: str
    resolved: str | None = None


def apply_model_aliases(model: str, redirect: ModelRedirect) -> ResolvedModel:
    """Pure resolution helper. Mirrors the Rust ``apply_model_aliases`` impl."""
    if model in redirect.aliases:
        return ResolvedModel(kind="aliased", resolved=redirect.aliases[model])
    if not redirect.known_models or model in redirect.known_models:
        return ResolvedModel(kind="passthrough", resolved=model)
    if redirect.default:
        return ResolvedModel(kind="fallback_default", resolved=redirect.default)
    return ResolvedModel(kind="unknown_no_default")


# ─── ChatState ───────────────────────────────────────────────────────


@dataclass(slots=True)
class ChatState:
    """State holder injected into every chat handler.

    Mirrors the Rust ``ChatState`` reduced to the surface a Python
    gateway needs: the in-process :class:`ChatService` (W1) plus the
    optional model redirect. Session storage, tool executor, approval
    gate, and identity store all live inside the ``ChatService``
    implementation on the Python side, so they don't need a separate
    wiring slot here.
    """

    service: ChatService
    model_redirect: ModelRedirect = field(default_factory=ModelRedirect)


# ─── Helpers ─────────────────────────────────────────────────────────


def _resolve_session_key(req: ChatRequest, header_val: str | None) -> str | None:
    """Body wins over header; empty / whitespace treated as absent.
    Mirrors the Rust ``resolve_session_key`` helper.
    """
    if req.session_key is not None:
        v = req.session_key.strip()
        if v:
            return v
    if header_val is not None:
        v = header_val.strip()
        if v:
            return v
    return None


def _role_from_str(s: str) -> Role:
    try:
        return Role(s)
    except ValueError:
        return Role.USER


def _build_internal_request(req: ChatRequest, session_key: str | None) -> InternalChatRequest:
    """Translate the OpenAI body into the internal protocol shape."""
    return InternalChatRequest(
        model=req.model,
        messages=[
            Message(role=_role_from_str(m.role), content=m.content)
            for m in req.messages
        ],
        session_key=session_key or "",
        stream=req.stream,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
    )


def _new_chat_id() -> str:
    """``chatcmpl-<uuid4>`` matches OpenAI + the Rust impl."""
    return f"chatcmpl-{uuid.uuid4()}"


def _normalise_finish_reason(raw: str, had_tool_calls: bool) -> str:
    """Mirror the Rust ``normalise_finish_reason`` mapping."""
    if raw in ("stop", "length", "tool_calls", "error"):
        return raw
    if raw == "tool_call":
        return "tool_calls"
    if raw == "":
        return "tool_calls" if had_tool_calls else "stop"
    return raw


def _tool_call_envelope(event: ToolCallEvent, call_id: str) -> dict[str, object]:
    """OpenAI non-streaming tool_call envelope."""
    args = event.args_json.decode("utf-8") if event.args_json else "{}"
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": event.tool,
            "arguments": args,
        },
    }


def _tool_call_delta_chunk(
    chat_id: str, model: str, index: int, event: ToolCallEvent, call_id: str
) -> dict[str, object]:
    args = event.args_json.decode("utf-8") if event.args_json else "{}"
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": event.tool,
                                "arguments": args,
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }


def _token_delta_chunk(chat_id: str, model: str, text: str) -> dict[str, object]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": None,
            }
        ],
    }


def _finish_chunk(chat_id: str, model: str, finish_reason: str) -> dict[str, object]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


# ─── Streaming + non-streaming bodies ────────────────────────────────


async def _run_nonstream(
    service: ChatService,
    internal_req: InternalChatRequest,
    model: str,
    cancel: asyncio.Event,
) -> JSONResponse:
    """Drain the event stream and assemble an OpenAI-shaped JSON body.
    Mirrors the Rust ``chat_nonstream`` implementation.
    """
    content_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    finish_reason = "stop"

    stream: AsyncIterator[InternalChatEvent] = service.run(internal_req, cancel)
    async for event in stream:
        if isinstance(event, TokenDeltaEvent):
            content_parts.append(event.text)
        elif isinstance(event, ToolCallEvent):
            call_id = f"call_{uuid.uuid4().hex[:16]}"
            tool_calls.append(_tool_call_envelope(event, call_id))
        elif isinstance(event, DoneEvent):
            finish_reason = _normalise_finish_reason(
                event.finish_reason, bool(tool_calls)
            )
            break
        elif isinstance(event, ErrorEvent):
            return _error_response(
                status.HTTP_502_BAD_GATEWAY,
                f"upstream_{event.error.reason}",
                event.error.message,
            )

    body: dict[str, object] = {
        "id": _new_chat_id(),
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(content_parts),
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    return JSONResponse(body)


async def _sse_iter(
    service: ChatService,
    internal_req: InternalChatRequest,
    model: str,
    cancel: asyncio.Event,
) -> AsyncIterator[bytes]:
    """Render the event stream as OpenAI-shaped SSE.
    Mirrors the Rust ``build_sse_stream`` implementation.
    """
    chat_id = _new_chat_id()
    next_index = 0
    tool_calls_seen = False
    stream: AsyncIterator[InternalChatEvent] = service.run(internal_req, cancel)
    async for event in stream:
        if isinstance(event, TokenDeltaEvent):
            chunk = _token_delta_chunk(chat_id, model, event.text)
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        elif isinstance(event, ToolCallEvent):
            call_id = f"call_{uuid.uuid4().hex[:16]}"
            chunk = _tool_call_delta_chunk(chat_id, model, next_index, event, call_id)
            next_index += 1
            tool_calls_seen = True
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        elif isinstance(event, DoneEvent):
            finish = _normalise_finish_reason(event.finish_reason, tool_calls_seen)
            chunk = _finish_chunk(chat_id, model, finish)
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            break
        elif isinstance(event, ErrorEvent):
            err = {
                "error": {
                    "code": "upstream_error",
                    "reason": event.error.reason,
                    "message": event.error.message,
                }
            }
            yield f"data: {json.dumps(err)}\n\n".encode()
            break
    yield b"data: [DONE]\n\n"


# ─── Router ──────────────────────────────────────────────────────────


def router(state: ChatState | None = None) -> APIRouter:
    """Build the ``/v1/chat/completions`` sub-router.

    :param state: :class:`ChatState` carrying the wired
        :class:`ChatService`. When ``None`` the route returns 501
        ``not_implemented`` — matches the Rust stub router.
    """
    api = APIRouter()

    @api.post("/v1/chat/completions")
    async def handle_chat(
        req: ChatRequest,
        request: Request,
        x_session_key: str | None = Header(default=None),
    ) -> JSONResponse | StreamingResponse:
        if state is None:
            return _error_response(
                status.HTTP_501_NOT_IMPLEMENTED,
                "not_implemented",
                "no ChatService wired; build router(state=...)",
            )

        if not req.model:
            return _error_response(
                status.HTTP_400_BAD_REQUEST,
                "invalid_request",
                "`model` is required",
            )
        if not req.messages:
            return _error_response(
                status.HTTP_400_BAD_REQUEST,
                "invalid_request",
                "`messages` must be non-empty",
            )

        # Model alias / unknown-model fallback. Pure function so the
        # logging tier sits in the handler.
        original_model = req.model
        resolution = apply_model_aliases(req.model, state.model_redirect)
        if resolution.kind == "aliased":
            req.model = resolution.resolved or req.model
        elif resolution.kind == "fallback_default":
            req.model = resolution.resolved or req.model
        elif resolution.kind == "unknown_no_default":
            return _error_response(
                status.HTTP_400_BAD_REQUEST,
                "unknown_model",
                f"model `{original_model}` is not a known alias or provider "
                f"model, and no `models.default` fallback is configured",
            )

        session_key = _resolve_session_key(req, x_session_key)
        internal_req = _build_internal_request(req, session_key)
        cancel = asyncio.Event()

        if req.stream:
            async def _agen() -> AsyncIterator[bytes]:
                try:
                    async for chunk in _sse_iter(
                        state.service, internal_req, req.model, cancel
                    ):
                        if await request.is_disconnected():
                            cancel.set()
                            break
                        yield chunk
                finally:
                    cancel.set()

            return StreamingResponse(_agen(), media_type="text/event-stream")

        return await _run_nonstream(state.service, internal_req, req.model, cancel)

    return api
