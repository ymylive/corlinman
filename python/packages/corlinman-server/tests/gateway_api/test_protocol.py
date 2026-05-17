"""Unit tests for ``corlinman_server.gateway_api.protocol``.

Validates the structural ``ChatService`` protocol and the abstract base
behave as documented:

* A class implementing ``async def run(...) -> AsyncIterator[Event]``
  satisfies the protocol at runtime.
* The abstract base refuses instantiation without an override.
* Cancellation via the supplied :class:`asyncio.Event` is honoured by a
  reference implementation (the tests own this fixture, but the
  contract is asserted in :class:`ChatService.run`'s docstring).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from corlinman_server.gateway_api import (
    ChatService,
    ChatServiceBase,
    DoneEvent,
    InternalChatEvent,
    InternalChatRequest,
    Message,
    Role,
    TokenDeltaEvent,
)


class _RefChatService:
    """Reference implementation used to exercise the protocol contract.

    Yields one ``TokenDeltaEvent`` per character in the first user
    message, then a single ``DoneEvent``. Honours ``cancel`` between
    yields.
    """

    async def run(
        self,
        req: InternalChatRequest,
        cancel: asyncio.Event,
    ) -> AsyncIterator[InternalChatEvent]:
        text = req.messages[0].content if req.messages else ""
        for ch in text:
            if cancel.is_set():
                return
            yield TokenDeltaEvent(text=ch)
            # Cooperative checkpoint so cancellation can land between chars.
            await asyncio.sleep(0)
        yield DoneEvent(finish_reason="stop")


def _req(text: str) -> InternalChatRequest:
    return InternalChatRequest(
        model="m",
        messages=[Message(role=Role.USER, content=text)],
    )


def test_runtime_isinstance_passes_for_protocol_implementer() -> None:
    """``@runtime_checkable`` lets us assert duck-typed conformance."""
    assert isinstance(_RefChatService(), ChatService)


def test_runtime_isinstance_rejects_object_missing_run() -> None:
    class _NotAService:
        pass

    assert not isinstance(_NotAService(), ChatService)


@pytest.mark.asyncio
async def test_reference_impl_streams_tokens_then_done() -> None:
    svc = _RefChatService()
    cancel = asyncio.Event()
    events: list[InternalChatEvent] = []
    async for ev in svc.run(_req("hi"), cancel):
        events.append(ev)
    # Two TokenDelta ("h", "i") + one Done.
    assert len(events) == 3
    assert all(isinstance(e, TokenDeltaEvent) for e in events[:2])
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_cancel_event_stops_stream_early() -> None:
    """A reference impl that checks ``cancel.is_set()`` between yields
    must stop emitting events once the flag fires."""
    svc = _RefChatService()
    cancel = asyncio.Event()
    cancel.set()
    events: list[InternalChatEvent] = []
    async for ev in svc.run(_req("hello"), cancel):
        events.append(ev)
    # Cancelled before first iteration → no events.
    assert events == []


def test_chat_service_base_is_abstract() -> None:
    with pytest.raises(TypeError):
        ChatServiceBase()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_chat_service_base_subclass_satisfies_protocol() -> None:
    class _SubclassImpl(ChatServiceBase):
        async def run(
            self,
            req: InternalChatRequest,
            cancel: asyncio.Event,
        ) -> AsyncIterator[InternalChatEvent]:
            yield DoneEvent(finish_reason="stop")

    svc = _SubclassImpl()
    assert isinstance(svc, ChatService)
    cancel = asyncio.Event()
    events = [ev async for ev in svc.run(_req(""), cancel)]
    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)
