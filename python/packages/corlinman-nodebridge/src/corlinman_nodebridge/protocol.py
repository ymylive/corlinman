"""Wire-level message frames for the NodeBridge v1 protocol.

Mirrors ``rust/crates/corlinman-nodebridge/src/message.rs``. Transport:
JSON text frames over WebSocket. Each variant carries an explicit
``kind`` discriminant so a pcap reader can sort frames without knowing
Python field ordering — same contract decision as the Rust source.

Pydantic v2 ``discriminated union`` provides the Python analog of
serde's ``#[serde(tag = "kind", rename_all = "snake_case")]``. Each
variant is a frozen ``BaseModel`` with ``kind: Literal["..."]``; the
umbrella :data:`NodeBridgeMessage` is the tagged union used at every
serialization boundary.

The ``Capability`` model is the one place a future signed attestation
will live: :attr:`Register.signature` is currently ``Optional[str]`` and
populated only when the client opts in. When ``accept_unsigned = False``,
a ``Register`` without a signature is rejected by the server
pre-state-change.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "Capability",
    "DispatchJob",
    "Heartbeat",
    "JobResult",
    "NodeBridgeMessage",
    "NodeBridgeMessageAdapter",
    "Ping",
    "Pong",
    "Register",
    "RegisterRejected",
    "Registered",
    "Shutdown",
    "Telemetry",
    "decode_message",
    "encode_message",
]


# ---------------------------------------------------------------------------
# Capability — advertised at registration time.
# ---------------------------------------------------------------------------


class Capability(BaseModel):
    """A single capability a node advertises at registration time.

    ``params_schema`` is an opaque JSON-Schema-shaped object. Validation
    is deferred to dispatchers; the server treats it as a black box so
    clients can extend their schemas without a server upgrade.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    params_schema: Any

    @classmethod
    def new(cls, name: str, version: str, params_schema: Any) -> Capability:
        """Convenience constructor matching the Rust ``Capability::new``."""
        return cls(name=name, version=version, params_schema=params_schema)


# ---------------------------------------------------------------------------
# Frame base + concrete variants. Each variant carries a ``kind`` literal
# discriminant so pydantic v2's tagged-union machinery can dispatch
# during parsing.
# ---------------------------------------------------------------------------


class _FrameBase(BaseModel):
    """Mixin: pydantic config shared by every frame variant."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- client -> server ----------


class Register(_FrameBase):
    """Handshake + advertisement. First frame a client sends.

    The server replies with :class:`Registered` or
    :class:`RegisterRejected` before any other traffic is accepted.
    ``signature`` is reserved for client attestation (future-work); when
    the server's ``accept_unsigned = False`` and this is ``None``,
    registration is rejected.
    """

    kind: Literal["register"] = "register"
    node_id: str
    # Free-form client classification: "ios", "android", "macos",
    # "linux", "other". Not enforced server-side.
    node_type: str
    capabilities: list[Capability] = Field(default_factory=list)
    auth_token: str
    version: str
    # Future: signed client attestation. Skipped from serialized form
    # when ``None`` to mirror Rust's
    # ``skip_serializing_if = "Option::is_none"``.
    signature: str | None = None


class Heartbeat(_FrameBase):
    """Client -> server liveness ping.

    Expected cadence is ``heartbeat_secs`` returned in the prior
    :class:`Registered` frame. After three consecutive missed heartbeats
    the server drops the connection.
    """

    kind: Literal["heartbeat"] = "heartbeat"
    node_id: str
    at_ms: int


class JobResult(_FrameBase):
    """Terminal result for a previously-dispatched job.

    The server correlates by ``job_id``; unknown job ids are logged and
    dropped.
    """

    kind: Literal["job_result"] = "job_result"
    job_id: str
    ok: bool
    payload: Any


class Telemetry(_FrameBase):
    """Arbitrary metric emission.

    Forwarded as ``HookEvent.Telemetry`` on the gateway's hook bus.
    ``tags`` keys are serialized in lexicographic order to match the
    Rust ``BTreeMap`` so wire output is stable across emits.
    """

    kind: Literal["telemetry"] = "telemetry"
    node_id: str
    metric: str
    value: float
    tags: dict[str, str] = Field(default_factory=dict)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        # Mirror Rust BTreeMap key ordering on the wire.
        data = super().model_dump(**kwargs)
        if "tags" in data and isinstance(data["tags"], dict):
            data["tags"] = dict(sorted(data["tags"].items()))
        return data


# ---------- server -> client ----------


class Registered(_FrameBase):
    """Registration accepted.

    ``heartbeat_secs`` tells the client how often to emit
    :class:`Heartbeat`.
    """

    kind: Literal["registered"] = "registered"
    node_id: str
    server_version: str
    heartbeat_secs: int


class RegisterRejected(_FrameBase):
    """Registration refused. Followed by connection close."""

    kind: Literal["register_rejected"] = "register_rejected"
    code: str
    message: str


class DispatchJob(_FrameBase):
    """Execute ``kind`` on the client.

    The client must eventually respond with ``JobResult { job_id }``;
    otherwise the server synthesises a local
    ``JobResult { ok: false, payload: { "error": "timeout" } }`` once
    ``timeout_ms`` elapses.

    The job-kind field is named ``job_kind`` on the wire to avoid a
    collision with the enum discriminant tag (``kind``); the Python
    attribute keeps the shorter name ``job_kind`` for clarity (the Rust
    source uses ``kind`` internally with a ``serde(rename)`` to
    ``job_kind`` on the wire — we just use ``job_kind`` end-to-end).
    """

    kind: Literal["dispatch_job"] = "dispatch_job"
    job_id: str
    job_kind: str
    params: Any
    timeout_ms: int


class Ping(_FrameBase):
    """Liveness probe in either direction."""

    kind: Literal["ping"] = "ping"


class Pong(_FrameBase):
    """Liveness reply in either direction."""

    kind: Literal["pong"] = "pong"


class Shutdown(_FrameBase):
    """Server-initiated connection close with a human-readable reason."""

    kind: Literal["shutdown"] = "shutdown"
    reason: str


# ---------------------------------------------------------------------------
# Tagged union + helpers. Use ``TypeAdapter`` for parse / dump because
# the union itself is just a type alias.
# ---------------------------------------------------------------------------


NodeBridgeMessage = Annotated[
    Register
    | Heartbeat
    | JobResult
    | Telemetry
    | Registered
    | RegisterRejected
    | DispatchJob
    | Ping
    | Pong
    | Shutdown,
    Field(discriminator="kind"),
]
"""Tagged union covering every frame the v1 protocol defines.

Use :func:`encode_message` / :func:`decode_message` to convert to and
from JSON text. The union itself is suitable as a parameter / return
type annotation in user code.
"""


NodeBridgeMessageAdapter: TypeAdapter[NodeBridgeMessage] = TypeAdapter(NodeBridgeMessage)


def encode_message(msg: NodeBridgeMessage) -> str:
    """Serialize a frame to a compact JSON string.

    ``signature = None`` on :class:`Register` is omitted from the output
    to match Rust's ``skip_serializing_if = "Option::is_none"``.
    :class:`Telemetry` tags are sorted lexicographically.
    """
    data = msg.model_dump(exclude_none=True) if isinstance(msg, Register) else msg.model_dump()
    import json

    return json.dumps(data, separators=(",", ":"))


def decode_message(raw: str | bytes | bytearray) -> NodeBridgeMessage:
    """Parse a JSON frame into the matching concrete variant.

    Raises :class:`pydantic.ValidationError` if the frame is malformed
    or the ``kind`` discriminant is unknown.
    """
    if isinstance(raw, (bytes, bytearray)):
        return NodeBridgeMessageAdapter.validate_json(bytes(raw))
    return NodeBridgeMessageAdapter.validate_json(raw)
