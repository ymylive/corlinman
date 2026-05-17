"""``ChatService`` protocol — Python mirror of the Rust trait.

The Rust crate defines an ``async_trait`` ``ChatService::run`` that
yields a boxed stream of :class:`InternalChatEvent`. In Python the
idiomatic equivalent is an ``async def`` method returning an
``AsyncIterator[InternalChatEvent]`` — callers ``async for`` over the
result the same way they consume ``BoxStream`` on the Rust side.

We expose three shapes so callers can pick the one that fits:

* :class:`ChatService` — :class:`typing.Protocol` (structural typing).
  Use this for type annotations and ``isinstance`` checks
  (``runtime_checkable``); no inheritance required for implementers.
* :class:`ChatServiceBase` — abstract :class:`abc.ABC` base class for
  implementers that prefer nominal inheritance plus a typed ``__init__``
  surface. Mirrors the "implement this trait" ergonomic of Rust.
* :data:`SharedChatService` — convenience type alias matching the Rust
  ``type SharedChatService = Arc<dyn ChatService>``. In Python there is
  no ``Arc`` — references are reference-counted natively — so this is
  just ``ChatService`` aliased for migration clarity.

A note on cancellation: the Rust signature takes
``CancellationToken``; in Python the standard idiom is for the caller
to cancel the consuming task (which propagates ``asyncio.CancelledError``
into the generator) so we accept an :class:`asyncio.Event` as the
explicit cancel flag instead. Implementers MUST honour it and stop
producing events when it fires. See :class:`ChatService.run` for the
full contract.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from corlinman_server.gateway_api.types import (
    InternalChatEvent,
    InternalChatRequest,
)

__all__ = [
    "ChatEventStream",
    "ChatService",
    "ChatServiceBase",
    "SharedChatService",
]


#: Mirrors the Rust ``type ChatEventStream = BoxStream<'static, InternalChatEvent>``.
#:
#: An ``AsyncIterator`` is the Python analogue of a boxed async stream —
#: callers ``async for event in stream`` exactly the way Rust callers do
#: ``while let Some(event) = stream.next().await``.
ChatEventStream = AsyncIterator[InternalChatEvent]


@runtime_checkable
class ChatService(Protocol):
    """Structural protocol implemented by the gateway, consumed by callers.

    Implementations MUST:

    * Honour ``cancel`` — drop upstream work when the event fires and
      stop yielding new events.
    * Terminate the stream after emitting exactly one terminal event
      (:class:`DoneEvent` or :class:`ErrorEvent`).
    * Be cheap to call — callers hold a single shared instance and may
      invoke ``run`` once per inbound message.

    The Rust signature uses ``async fn run(...) -> ChatEventStream`` —
    in Python the equivalent is either:

    * Return an async iterator from a regular ``async def`` (build it
      yourself and return), OR
    * Implement ``run`` as an ``async def`` generator (``yield`` events
      directly). Both satisfy the protocol because both produce an
      ``AsyncIterator[InternalChatEvent]``.

    NOTE: at runtime ``isinstance(x, ChatService)`` checks only that
    ``x`` has a callable ``run`` attribute — it does NOT validate the
    method signature. Use a static type checker (mypy / pyright) to
    enforce the full shape.
    """

    def run(
        self,
        req: InternalChatRequest,
        cancel: asyncio.Event,
    ) -> ChatEventStream:
        """Run a single chat request and stream events back.

        :param req: the request to execute.
        :param cancel: an :class:`asyncio.Event` the caller may set to
            request cancellation. Implementations check
            ``cancel.is_set()`` between yields and SHOULD drop any
            outstanding upstream calls when it fires.
        :returns: an async iterator that yields zero or more
            :class:`TokenDeltaEvent` / :class:`ToolCallEvent` followed
            by exactly one :class:`DoneEvent` or :class:`ErrorEvent`.
        """
        ...


class ChatServiceBase(abc.ABC):
    """Abstract base for ``ChatService`` implementations that prefer
    nominal inheritance over structural typing.

    Equivalent in intent to ``impl ChatService for MyGateway`` on the
    Rust side. Subclass and override :meth:`run`; the concrete
    instance automatically satisfies the :class:`ChatService` protocol
    (it has the right method) so existing call sites typed against the
    protocol Just Work.
    """

    @abc.abstractmethod
    def run(
        self,
        req: InternalChatRequest,
        cancel: asyncio.Event,
    ) -> ChatEventStream:
        """See :meth:`ChatService.run` for the contract."""
        raise NotImplementedError


#: Convenience alias for callers that want to talk about "the shared
#: instance" without dragging in ``typing.Annotated`` / wrapper types.
#: Mirrors ``type SharedChatService = Arc<dyn ChatService>`` on the
#: Rust side — Python has no ``Arc`` (reference counting is built in)
#: so the alias is just :class:`ChatService` itself, preserved for
#: migration / readability.
SharedChatService = ChatService
