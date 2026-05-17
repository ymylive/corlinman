"""Crate-local errors for the NodeBridge stub server.

Mirrors ``rust/crates/corlinman-nodebridge/src/error.rs``. The Rust crate
uses :class:`thiserror::Error` with named variants; in Python each
variant is a subclass of :class:`NodeBridgeError` so callers can
``except NodeBridgeTimeout`` specifically while still being able to
``except NodeBridgeError`` for "anything from this crate".
"""

from __future__ import annotations

__all__ = [
    "NodeBridgeBindError",
    "NodeBridgeError",
    "NodeBridgeInvalidListenAddr",
    "NodeBridgeNoCapableNode",
    "NodeBridgeProtocolError",
    "NodeBridgeRegisterRejected",
    "NodeBridgeTimeout",
]


class NodeBridgeError(Exception):
    """Errors surfaced at the package boundary.

    Internal paths that talk to the reader/writer halves of a socket tend
    to collapse into :class:`NodeBridgeBindError` or
    :class:`NodeBridgeProtocolError`; dispatch failures into
    :class:`NodeBridgeNoCapableNode` / :class:`NodeBridgeTimeout`.
    """


class NodeBridgeInvalidListenAddr(NodeBridgeError):  # noqa: N818
    """The supplied ``cfg.bind`` failed to parse as ``host:port``."""

    def __init__(self, addr: str) -> None:
        super().__init__(f"invalid listen address: {addr}")
        self.addr = addr


class NodeBridgeBindError(NodeBridgeError):
    """Binding the TCP listener failed."""

    def __init__(self, message: str) -> None:
        super().__init__(f"bind: {message}")


class NodeBridgeProtocolError(NodeBridgeError):
    """A frame arrived in a position it isn't allowed to.

    Common causes: something other than ``Register`` as the first frame;
    the outbox for a session was closed before the server could write to
    it.
    """

    def __init__(self, message: str) -> None:
        super().__init__(f"protocol: {message}")
        self.detail = message


# N818 (Error suffix) is intentionally suppressed on the three classes
# below: their names mirror the Rust ``NodeBridgeError`` variants verbatim
# (``NoCapableNode``, ``Timeout``, ``RegisterRejected``) so cross-language
# searches land on the same identifier. They are still ``NodeBridgeError``
# subclasses, so ``except NodeBridgeError`` continues to work.


class NodeBridgeNoCapableNode(NodeBridgeError):  # noqa: N818
    """``dispatch_job`` asked for a ``kind`` no registered node advertises."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"no capable node for kind: {kind}")
        self.kind = kind


class NodeBridgeTimeout(NodeBridgeError):  # noqa: N818
    """A dispatched job didn't receive a ``JobResult`` in time."""

    def __init__(self, millis: int) -> None:
        super().__init__(f"dispatch timed out after {millis}ms")
        self.millis = millis


class NodeBridgeRegisterRejected(NodeBridgeError):  # noqa: N818
    """Registration was refused with a coded reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"register rejected [{code}]: {message}")
        self.code = code
        self.message = message
