"""Wire-level message frames for the distributed tool-bus protocol.

Python port of ``rust/crates/corlinman-wstool/src/message.rs``.

All frames travel as JSON over WebSocket text frames. The ``kind``
discriminant is an explicit tag (snake_case) so a human reading a pcap
can pick out message types without knowing field order; mirrors the
Rust crate's ``#[serde(tag = "kind", rename_all = "snake_case")]``.

The protocol is framed but **not** request/reply ordered on the wire —
concurrent ``invoke`` requests share a single socket and are correlated
by ``request_id``. The server side maintains the waiter map; the runner
side maintains the cancellation map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

__all__ = ["ToolAdvert", "WsToolMessage"]


@dataclass(frozen=True)
class ToolAdvert:
    """Per-tool advertisement emitted by the runner inside ``accept``.

    ``parameters`` is a JSON-Schema-shaped object suitable for
    OpenAI-function-call style advertisement. We don't validate its
    shape here — the registry layer does that when the runner registers.
    """

    name: str
    description: str
    parameters: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolAdvert:
        return cls(
            name=payload["name"],
            description=payload["description"],
            parameters=payload.get("parameters"),
        )


# ---------------------------------------------------------------------------
# Variant base + concrete dataclasses. Each variant has KIND set to the
# snake_case discriminant so ``to_dict`` emits the right ``kind`` field.
# ---------------------------------------------------------------------------


class _WsToolMessageBase:
    """Common mixin for every WsToolMessage variant.

    Subclasses set :attr:`KIND` to the snake_case discriminant matching
    the Rust ``#[serde(rename_all = "snake_case")]`` default.
    """

    KIND: ClassVar[str] = ""

    def kind(self) -> str:
        return self.KIND

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.KIND}
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            # ToolAdvert serializes as a dict; lists of them too.
            if isinstance(value, ToolAdvert):
                out[f.name] = value.to_dict()
            elif isinstance(value, list) and value and isinstance(value[0], ToolAdvert):
                out[f.name] = [v.to_dict() for v in value]
            else:
                out[f.name] = value
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


# ---------- gateway -> runner ----------


@dataclass(frozen=True)
class _Invoke(_WsToolMessageBase):
    """Execute ``tool`` with ``args``. Runner must eventually reply with
    exactly one of ``result`` or ``error`` bearing the same ``request_id``.
    """

    request_id: str
    tool: str
    args: Any
    timeout_ms: int

    KIND: ClassVar[str] = "invoke"


@dataclass(frozen=True)
class _Cancel(_WsToolMessageBase):
    """Cancel an in-flight invocation. Best-effort."""

    request_id: str

    KIND: ClassVar[str] = "cancel"


@dataclass(frozen=True)
class _Ping(_WsToolMessageBase):
    """Liveness probe."""

    KIND: ClassVar[str] = "ping"


# ---------- runner -> gateway ----------


@dataclass(frozen=True)
class _Accept(_WsToolMessageBase):
    """Handshake response — runner accepted the auth token and declares
    its advertised tools. Sent exactly once per connection.
    """

    server_version: str
    heartbeat_secs: int
    supported_tools: list[ToolAdvert] = field(default_factory=list)

    KIND: ClassVar[str] = "accept"


@dataclass(frozen=True)
class _Reject(_WsToolMessageBase):
    """Handshake response — auth/version mismatch or policy reject.
    Followed by connection close.
    """

    code: str
    message: str

    KIND: ClassVar[str] = "reject"


@dataclass(frozen=True)
class _Progress(_WsToolMessageBase):
    """Mid-flight progress update for an in-flight ``invoke``."""

    request_id: str
    data: Any

    KIND: ClassVar[str] = "progress"


@dataclass(frozen=True)
class _Result(_WsToolMessageBase):
    """Terminal success/controlled-failure frame for a given invoke.
    ``ok == False`` carries a structured error payload in ``payload``.
    """

    request_id: str
    ok: bool
    payload: Any

    KIND: ClassVar[str] = "result"


@dataclass(frozen=True)
class _Error(_WsToolMessageBase):
    """Terminal protocol-level error for a given invoke. Distinct from
    ``result(ok=False)`` so callers can tell "tool ran and returned an
    error" from "tool never ran".
    """

    request_id: str
    code: str
    message: str

    KIND: ClassVar[str] = "error"


@dataclass(frozen=True)
class _Pong(_WsToolMessageBase):
    """Heartbeat reply."""

    KIND: ClassVar[str] = "pong"


class WsToolMessage(_WsToolMessageBase):
    """Tagged-union of every protocol frame on the wire.

    Direction is encoded in variant commentary rather than in the type —
    server and runner each implement their own dispatch-by-kind match
    and reject frames that travel the wrong way.

    Concrete variants are exposed as nested classes so call sites look
    like ``WsToolMessage.Invoke(...)`` to mirror the Rust
    ``WsToolMessage::Invoke { ... }`` constructor syntax.
    """

    # gateway -> runner
    Invoke = _Invoke
    Cancel = _Cancel
    Ping = _Ping
    # runner -> gateway
    Accept = _Accept
    Reject = _Reject
    Progress = _Progress
    Result = _Result
    Error = _Error
    Pong = _Pong

    _VARIANTS: ClassVar[dict[str, type[_WsToolMessageBase]]] = {
        "invoke": _Invoke,
        "cancel": _Cancel,
        "ping": _Ping,
        "accept": _Accept,
        "reject": _Reject,
        "progress": _Progress,
        "result": _Result,
        "error": _Error,
        "pong": _Pong,
    }

    def __init__(self) -> None:  # pragma: no cover — never instantiated
        raise TypeError(
            "WsToolMessage is a tagged-union umbrella; instantiate a concrete "
            "variant such as WsToolMessage.Invoke(...) instead."
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> _WsToolMessageBase:
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise ValueError("missing or non-string 'kind' field")
        variant = cls._VARIANTS.get(kind)
        if variant is None:
            raise ValueError(f"unknown wstool message kind: {kind!r}")
        kwargs: dict[str, Any] = {k: v for k, v in payload.items() if k != "kind"}
        # Hydrate nested ToolAdvert lists for Accept.
        if variant is _Accept and "supported_tools" in kwargs:
            raw = kwargs["supported_tools"] or []
            kwargs["supported_tools"] = [ToolAdvert.from_dict(t) for t in raw]
        return variant(**kwargs)

    @classmethod
    def from_json(cls, raw: str) -> _WsToolMessageBase:
        return cls.from_dict(json.loads(raw))
