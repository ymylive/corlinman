"""``corlinman.v1.Agent`` gRPC servicer.

Implements the bidirectional streaming ``Chat`` RPC:

1. read the first :class:`ClientFrame`, expect ``ClientFrame.start``;
2. resolve a provider via :func:`corlinman_providers.registry.resolve`;
3. drive :class:`corlinman_agent.reasoning_loop.ReasoningLoop` and translate
   each yielded event into the matching :class:`ServerFrame` variant;
4. return — the client always closes the request half by dropping its
   ``mpsc::Sender<ClientFrame>``.

M1/M2 scope: ``ToolCall`` frames are emitted but we don't wait for a matching
``ToolResult`` — the gateway echoes an ``awaiting_plugin_runtime`` placeholder
and we advance to ``Done`` so the E2E pipeline completes. M3 flips this to a
full wait-for-ToolResult loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import grpc
import structlog
from corlinman_agent.reasoning_loop import (
    Attachment as AgentAttachment,
)
from corlinman_agent.reasoning_loop import (
    ChatStart as AgentChatStart,
)
from corlinman_agent.reasoning_loop import (
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_providers import registry as provider_registry
from corlinman_providers.base import ProviderChunk

logger = structlog.get_logger(__name__)


class _MockProvider:
    """Offline provider used by the E2E smoke script.

    Activated by setting ``CORLINMAN_TEST_MOCK_PROVIDER`` in the environment —
    the value is streamed back verbatim as a single ``token`` chunk so the
    Rust gateway / Python loop can be exercised without network access.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        yield ProviderChunk(kind="token", text=self._text)
        yield ProviderChunk(kind="done", finish_reason="stop")


def _mock_resolver(_model: str) -> Any:
    text = os.environ.get("CORLINMAN_TEST_MOCK_PROVIDER", "")
    return _MockProvider(text)


class CorlinmanAgentServicer(agent_pb2_grpc.AgentServicer):
    """Concrete implementation — replaces the default UNIMPLEMENTED stub."""

    def __init__(self, provider_resolver: Any | None = None) -> None:
        """``provider_resolver`` defaults to :mod:`corlinman_providers.registry`.

        The indirection exists so tests can inject a fake provider without
        touching the global registry. If the caller doesn't supply one and
        ``CORLINMAN_TEST_MOCK_PROVIDER`` is set, a mock resolver is used —
        this drives the E2E smoke script without hitting the real network.
        """
        if provider_resolver is not None:
            self._resolve = provider_resolver
        elif os.environ.get("CORLINMAN_TEST_MOCK_PROVIDER") is not None:
            self._resolve = _mock_resolver
        else:
            self._resolve = provider_registry.resolve

    async def Chat(  # noqa: N802 — gRPC method name
        self,
        request_iterator: AsyncIterator[agent_pb2.ClientFrame],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[agent_pb2.ServerFrame]:
        start_frame = await _expect_start(request_iterator)
        if start_frame is None:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "first frame must be ClientFrame.start",
            )
            return

        start = _to_agent_start(start_frame.start)
        logger.info("agent.chat.start", model=start.model, session=start.session_key)

        try:
            provider = self._resolve(start.model)
        except KeyError as exc:
            yield _error_frame("model_not_found", str(exc))
            return

        # Bump the tool-result timeout above the M2 default (0.05s) so the
        # loop actually waits long enough for the gateway to round-trip a
        # ToolResult frame back. The servicer is now the real feedback
        # channel — the ``awaiting_plugin_runtime`` placeholder short-circuit
        # still protects us against runaway loops.
        loop = ReasoningLoop(provider, tool_result_timeout=30.0)

        inbound_task = asyncio.create_task(
            _pump_inbound(request_iterator, loop),
            name="agent.chat.pump_inbound",
        )

        seq = 0
        try:
            async for event in loop.run(start):
                if isinstance(event, TokenEvent):
                    yield agent_pb2.ServerFrame(
                        token=agent_pb2.TokenDelta(
                            text=event.text,
                            is_reasoning=event.is_reasoning,
                            seq=seq,
                        )
                    )
                    seq += 1
                elif isinstance(event, ToolCallEvent):
                    yield agent_pb2.ServerFrame(
                        tool_call=agent_pb2.ToolCall(
                            call_id=event.call_id,
                            plugin=event.plugin,
                            tool=event.tool,
                            args_json=event.args_json,
                            seq=seq,
                        )
                    )
                    seq += 1
                elif isinstance(event, ErrorEvent):
                    yield _error_frame(event.reason, event.message)
                    return
                elif isinstance(event, DoneEvent):
                    yield agent_pb2.ServerFrame(
                        done=agent_pb2.Done(finish_reason=event.finish_reason)
                    )
                    return
        except Exception as exc:
            logger.exception("agent.chat.fatal", error=str(exc))
            yield _error_frame("unknown", str(exc))
        finally:
            inbound_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await inbound_task


async def _expect_start(
    iterator: AsyncIterator[agent_pb2.ClientFrame],
) -> agent_pb2.ClientFrame | None:
    """Drain the iterator until the first frame; return it if it carries a
    ``ChatStart``, else ``None``."""
    async for frame in iterator:
        if frame.WhichOneof("kind") == "start":
            return frame
        return None
    return None


async def _pump_inbound(
    iterator: AsyncIterator[agent_pb2.ClientFrame],
    loop: ReasoningLoop,
) -> None:
    """Forward post-ChatStart :class:`ClientFrame` messages to the loop.

    * ``tool_result`` → :meth:`ReasoningLoop.feed_tool_result`
    * ``cancel`` → :meth:`ReasoningLoop.cancel` and return
    * ``approval`` → logged only (S5 wires this into an approval gate)
    * duplicate ``start`` / unknown kinds → ignored
    """
    async for frame in iterator:
        kind = frame.WhichOneof("kind")
        if kind == "tool_result":
            tr = frame.tool_result
            content = tr.result_json.decode("utf-8", errors="replace")
            loop.feed_tool_result(
                ToolResult(
                    call_id=tr.call_id,
                    content=content,
                    is_error=tr.is_error,
                )
            )
            logger.debug(
                "agent.chat.tool_result_in",
                call_id=tr.call_id,
                is_error=tr.is_error,
                duration_ms=tr.duration_ms,
            )
        elif kind == "cancel":
            reason = frame.cancel.reason or "client_cancel"
            logger.info("agent.chat.cancel_in", reason=reason)
            loop.cancel(reason=reason)
            return
        elif kind == "approval":
            # S5 will wire this into an approval gate; today we just log.
            logger.debug(
                "agent.chat.approval_received_but_not_wired",
                call_id=frame.approval.call_id,
                approved=frame.approval.approved,
            )
        elif kind == "start":
            logger.warning("agent.chat.duplicate_start_ignored")
        # Unknown kinds silently ignored — protobuf forward compatibility.


def _to_agent_start(pb_start: agent_pb2.ChatStart) -> AgentChatStart:
    """Convert a protobuf ``ChatStart`` into the agent's dataclass form."""
    messages = [
        {"role": _role_name(m.role), "content": m.content}
        for m in pb_start.messages
    ]
    attachments = [_to_agent_attachment(a) for a in pb_start.attachments]
    return AgentChatStart(
        model=pb_start.model,
        messages=messages,
        tools=[],  # tools_json parsing lands with the full OpenAI tool schema in M3
        session_key=pb_start.session_key,
        temperature=pb_start.temperature or None,
        max_tokens=pb_start.max_tokens or None,
        attachments=attachments,
    )


def _to_agent_attachment(pb: agent_pb2.Attachment) -> AgentAttachment:
    """Convert a protobuf ``Attachment`` to the agent dataclass.

    Empty strings / empty bytes on the proto side (the default for
    unset fields) map to ``None`` so providers can distinguish "unset"
    from "explicitly empty".
    """
    kind = _attachment_kind_name(pb.kind)
    return AgentAttachment(
        kind=kind,
        url=pb.url or None,
        bytes_=bytes(pb.bytes) if pb.bytes else None,
        mime=pb.mime or None,
        file_name=pb.file_name or None,
    )


def _attachment_kind_name(kind: Any) -> str:
    """Map ``AttachmentKind`` enum → lower-case string used in the dataclass.

    ``kind`` is the protobuf ``AttachmentKind`` wrapper (behaves like an
    int); typed as ``Any`` because the generated stub exposes the enum
    values as a custom wrapper class that mypy can't index against.
    """
    if kind == agent_pb2.ATTACHMENT_KIND_IMAGE:
        return "image"
    if kind == agent_pb2.ATTACHMENT_KIND_AUDIO:
        return "audio"
    if kind == agent_pb2.ATTACHMENT_KIND_VIDEO:
        return "video"
    return "file"


def _role_name(role: common_pb2.Role) -> str:
    mapping: dict[common_pb2.Role, str] = {
        common_pb2.USER: "user",
        common_pb2.ASSISTANT: "assistant",
        common_pb2.SYSTEM: "system",
        common_pb2.TOOL: "tool",
    }
    return mapping.get(role, "user")


def _error_frame(reason: str, message: str) -> agent_pb2.ServerFrame:
    return agent_pb2.ServerFrame(
        error=common_pb2.ErrorInfo(
            reason=_reason_to_proto(reason),
            message=message,
            retryable=reason in ("rate_limit", "timeout", "overloaded", "unknown"),
        )
    )


def _reason_to_proto(reason: str) -> common_pb2.FailoverReason:
    mapping: dict[str, common_pb2.FailoverReason] = {
        "billing": common_pb2.BILLING,
        "rate_limit": common_pb2.RATE_LIMIT,
        "auth": common_pb2.AUTH,
        "auth_permanent": common_pb2.AUTH_PERMANENT,
        "timeout": common_pb2.TIMEOUT,
        "model_not_found": common_pb2.MODEL_NOT_FOUND,
        "format": common_pb2.FORMAT,
        "context_overflow": common_pb2.CONTEXT_OVERFLOW,
        "overloaded": common_pb2.OVERLOADED,
    }
    return mapping.get(reason, common_pb2.UNKNOWN)
