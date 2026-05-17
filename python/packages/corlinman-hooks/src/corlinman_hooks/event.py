"""`HookEvent` — the wire-stable tagged-union broadcast on the hook bus.

Mirrors ``rust/crates/corlinman-hooks/src/event.rs``. The Rust crate uses
``#[serde(tag = "kind")]`` with PascalCase variant names so JSON payloads
carry an explicit discriminant field. We replicate that wire shape here
so the Python plane and the Rust gateway can speak the same JSON.

Each event variant is a nested dataclass under :class:`HookEvent`. The
``HookEvent`` umbrella class also serves as the static type for
"any hook event" — it is never instantiated directly; callers always
construct a concrete variant such as ``HookEvent.MessageReceived(...)``.

The ``kind()`` instance method returns the short snake_case discriminant
used for tracing / metric labels. The ``session_key()`` instance method
returns the scoped session key when one is present, or ``None`` for
global events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

__all__ = ["HookEvent"]


# ---------------------------------------------------------------------------
# Shared helpers (variant -> kind string + JSON dict round-trip).
# ---------------------------------------------------------------------------


def _none_to_default(value: Any, default: Any) -> Any:
    """Skip serializing ``None`` for optional fields (Rust uses
    ``skip_serializing_if = "Option::is_none"`` on those)."""
    return default if value is None else value


class _HookEventBase:
    """Common mixin for every concrete event variant.

    Subclasses set the class-level :attr:`KIND` to the snake_case
    discriminant string returned by :meth:`kind`. The class name itself
    is the PascalCase JSON ``kind`` value used in serialization (which
    mirrors Rust's default for ``#[serde(tag = "kind")]``).
    """

    KIND: ClassVar[str] = ""
    # Sequence of fields that should be omitted from the JSON output when
    # their value is ``None`` (mirrors ``skip_serializing_if =
    # "Option::is_none"``).
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ()

    def kind(self) -> str:
        return self.KIND

    def session_key(self) -> str | None:
        """Default: not session-scoped. Variants override as needed."""
        return None

    # PascalCase wire name. Each concrete variant overrides this with
    # the unprefixed class name (`_MessageReceived` -> `MessageReceived`)
    # so the serializer matches the Rust ``#[serde(tag = "kind")]``
    # default exactly. Set at class definition time below to avoid
    # repeating the string in every subclass body.
    VARIANT_NAME: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.VARIANT_NAME}
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            if f.name in self.OPTIONAL_FIELDS and value is None:
                continue
            out[f.name] = value
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


# ---------------------------------------------------------------------------
# Concrete event variants. Field names + types track the Rust source.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MessageReceived(_HookEventBase):
    channel: str
    session_key_: str
    content: str
    metadata: Any
    user_id: str | None = None

    KIND: ClassVar[str] = "message_received"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("user_id",)

    # `session_key` is both a field name in Rust and the helper method
    # on `_HookEventBase`. We store the field as `session_key_` and
    # expose it via the method/property below to avoid the clash while
    # still serializing as the canonical `session_key` JSON key.
    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _MessageSent(_HookEventBase):
    channel: str
    session_key_: str
    content: str
    success: bool
    user_id: str | None = None

    KIND: ClassVar[str] = "message_sent"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("user_id",)

    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _MessageTranscribed(_HookEventBase):
    session_key_: str
    transcript: str
    media_path: str
    media_type: str
    user_id: str | None = None

    KIND: ClassVar[str] = "message_transcribed"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("user_id",)

    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _MessagePreprocessed(_HookEventBase):
    session_key_: str
    transcript: str
    is_group: bool
    group_id: str | None
    user_id: str | None = None

    KIND: ClassVar[str] = "message_preprocessed"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("user_id",)

    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _SessionPatch(_HookEventBase):
    session_key_: str
    patch: Any
    user_id: str | None = None

    KIND: ClassVar[str] = "session_patch"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("user_id",)

    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _AgentBootstrap(_HookEventBase):
    workspace_dir: str
    session_key_: str
    files: list[str] = field(default_factory=list)

    KIND: ClassVar[str] = "agent_bootstrap"

    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _GatewayStartup(_HookEventBase):
    version: str

    KIND: ClassVar[str] = "gateway_startup"


@dataclass(frozen=True)
class _ConfigChanged(_HookEventBase):
    section: str
    old: Any
    new: Any

    KIND: ClassVar[str] = "config_changed"


@dataclass(frozen=True)
class _ToolCalled(_HookEventBase):
    tool: str
    runner_id: str
    duration_ms: int
    ok: bool
    error_code: str | None
    tenant_id: str | None = None
    user_id: str | None = None

    KIND: ClassVar[str] = "tool_called"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("tenant_id", "user_id")


@dataclass(frozen=True)
class _ApprovalRequested(_HookEventBase):
    id: str
    session_key_: str
    plugin: str
    tool: str
    args_preview: str
    timeout_at_ms: int
    user_id: str | None = None

    KIND: ClassVar[str] = "approval_requested"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("user_id",)

    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _ApprovalDecided(_HookEventBase):
    id: str
    decision: str
    decider: str | None
    decided_at_ms: int
    tenant_id: str | None = None
    user_id: str | None = None

    KIND: ClassVar[str] = "approval_decided"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("tenant_id", "user_id")


@dataclass(frozen=True)
class _RateLimitTriggered(_HookEventBase):
    session_key_: str
    limit_type: str
    retry_after_ms: int
    user_id: str | None = None

    KIND: ClassVar[str] = "rate_limit_triggered"
    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("user_id",)

    def session_key(self) -> str | None:
        return self.session_key_

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_key"] = d.pop("session_key_")
        return d


@dataclass(frozen=True)
class _Telemetry(_HookEventBase):
    node_id: str
    metric: str
    value: float
    tags: dict[str, str] = field(default_factory=dict)

    KIND: ClassVar[str] = "telemetry"

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        # Rust uses BTreeMap so the serialized JSON key order is
        # lexicographic. Mirror that here by sorting the tag dict.
        d["tags"] = dict(sorted(self.tags.items()))
        return d


@dataclass(frozen=True)
class _EngineRunCompleted(_HookEventBase):
    run_id: str
    proposals_generated: int
    duration_ms: int

    KIND: ClassVar[str] = "engine_run_completed"


@dataclass(frozen=True)
class _EngineRunFailed(_HookEventBase):
    run_id: str
    error_kind: str
    exit_code: int | None

    KIND: ClassVar[str] = "engine_run_failed"


@dataclass(frozen=True)
class _SubagentSpawned(_HookEventBase):
    parent_session_key: str
    child_session_key: str
    child_agent_id: str
    agent_card: str
    depth: int
    parent_trace_id: str
    tenant_id: str

    KIND: ClassVar[str] = "subagent_spawned"

    def session_key(self) -> str | None:
        return self.child_session_key


@dataclass(frozen=True)
class _SubagentCompleted(_HookEventBase):
    parent_session_key: str
    child_session_key: str
    child_agent_id: str
    finish_reason: str
    elapsed_ms: int
    tool_calls_made: int
    parent_trace_id: str
    tenant_id: str

    KIND: ClassVar[str] = "subagent_completed"

    def session_key(self) -> str | None:
        return self.child_session_key


@dataclass(frozen=True)
class _SubagentTimedOut(_HookEventBase):
    parent_session_key: str
    child_session_key: str
    child_agent_id: str
    elapsed_ms: int
    parent_trace_id: str
    tenant_id: str

    KIND: ClassVar[str] = "subagent_timed_out"

    def session_key(self) -> str | None:
        return self.child_session_key


@dataclass(frozen=True)
class _SubagentDepthCapped(_HookEventBase):
    parent_session_key: str
    attempted_depth: int
    reason: str
    parent_trace_id: str
    tenant_id: str

    KIND: ClassVar[str] = "subagent_depth_capped"

    def session_key(self) -> str | None:
        return self.parent_session_key


# ---------------------------------------------------------------------------
# Public umbrella class: exposes variants as attributes mirroring Rust's
# ``HookEvent::Variant`` syntax in the source crate.
# ---------------------------------------------------------------------------


class HookEvent(_HookEventBase):
    """Tagged-union of every event broadcast on the bus.

    Concrete variants are exposed as nested attributes so call sites
    look like ``HookEvent.MessageReceived(...)`` to mirror the Rust
    ``HookEvent::MessageReceived { ... }`` constructor syntax.

    Use :meth:`from_dict` / :meth:`from_json` to rehydrate a wire
    payload back into the concrete variant.
    """

    MessageReceived = _MessageReceived
    MessageSent = _MessageSent
    MessageTranscribed = _MessageTranscribed
    MessagePreprocessed = _MessagePreprocessed
    SessionPatch = _SessionPatch
    AgentBootstrap = _AgentBootstrap
    GatewayStartup = _GatewayStartup
    ConfigChanged = _ConfigChanged
    ToolCalled = _ToolCalled
    ApprovalRequested = _ApprovalRequested
    ApprovalDecided = _ApprovalDecided
    RateLimitTriggered = _RateLimitTriggered
    Telemetry = _Telemetry
    EngineRunCompleted = _EngineRunCompleted
    EngineRunFailed = _EngineRunFailed
    SubagentSpawned = _SubagentSpawned
    SubagentCompleted = _SubagentCompleted
    SubagentTimedOut = _SubagentTimedOut
    SubagentDepthCapped = _SubagentDepthCapped

    # Registry of PascalCase variant name -> concrete dataclass. Keys
    # mirror the Rust ``#[serde(tag = "kind")]`` discriminant values
    # exactly. We build this once at module import so ``from_dict`` is
    # an O(1) dispatch.
    _VARIANTS: ClassVar[dict[str, type[_HookEventBase]]] = {
        "MessageReceived": _MessageReceived,
        "MessageSent": _MessageSent,
        "MessageTranscribed": _MessageTranscribed,
        "MessagePreprocessed": _MessagePreprocessed,
        "SessionPatch": _SessionPatch,
        "AgentBootstrap": _AgentBootstrap,
        "GatewayStartup": _GatewayStartup,
        "ConfigChanged": _ConfigChanged,
        "ToolCalled": _ToolCalled,
        "ApprovalRequested": _ApprovalRequested,
        "ApprovalDecided": _ApprovalDecided,
        "RateLimitTriggered": _RateLimitTriggered,
        "Telemetry": _Telemetry,
        "EngineRunCompleted": _EngineRunCompleted,
        "EngineRunFailed": _EngineRunFailed,
        "SubagentSpawned": _SubagentSpawned,
        "SubagentCompleted": _SubagentCompleted,
        "SubagentTimedOut": _SubagentTimedOut,
        "SubagentDepthCapped": _SubagentDepthCapped,
    }

    # Set of variants that store the JSON ``session_key`` field under
    # the dataclass attribute ``session_key_`` (because the umbrella
    # method shadows the name on the class). ``from_dict`` rewrites
    # the key for these variants when constructing.
    _SESSION_KEY_VARIANTS: ClassVar[frozenset[str]] = frozenset(
        {
            "MessageReceived",
            "MessageSent",
            "MessageTranscribed",
            "MessagePreprocessed",
            "SessionPatch",
            "AgentBootstrap",
            "ApprovalRequested",
            "RateLimitTriggered",
        }
    )

    def __init__(self) -> None:  # pragma: no cover — never instantiated
        raise TypeError(
            "HookEvent is a tagged-union umbrella; instantiate a concrete "
            "variant such as HookEvent.MessageReceived(...) instead."
        )

    @classmethod
    def _bind_variant_names(cls) -> None:
        """Populate :attr:`_HookEventBase.VARIANT_NAME` on each variant.

        Called once at module-import time from below. We can't set the
        attribute inline on the dataclass body without repeating the
        string, so we drive it from the registry instead.
        """
        for name, variant in cls._VARIANTS.items():
            variant.VARIANT_NAME = name

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> _HookEventBase:
        """Construct a variant from a wire-format JSON dict.

        ``payload['kind']`` must be one of the PascalCase variant names
        (matching the Rust serde tag).
        """
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise ValueError("missing or non-string 'kind' field")
        variant = cls._VARIANTS.get(kind)
        if variant is None:
            raise ValueError(f"unknown hook event kind: {kind!r}")
        kwargs = {k: v for k, v in payload.items() if k != "kind"}
        if kind in cls._SESSION_KEY_VARIANTS and "session_key" in kwargs:
            kwargs["session_key_"] = kwargs.pop("session_key")
        # Fill in optional fields that may have been skipped by the
        # serializer (Rust ``skip_serializing_if = "Option::is_none"``
        # path). The dataclass defaults to ``None`` for these, so we
        # only need to handle keys the caller did pass.
        return variant(**kwargs)

    @classmethod
    def from_json(cls, raw: str) -> _HookEventBase:
        return cls.from_dict(json.loads(raw))


# Bind each concrete variant's PascalCase wire name now that the registry
# is fully populated. Keeps the dataclass bodies above free of repeated
# string literals while still letting ``to_dict`` emit the correct
# ``kind`` discriminant for round-trips through ``from_dict``.
HookEvent._bind_variant_names()
