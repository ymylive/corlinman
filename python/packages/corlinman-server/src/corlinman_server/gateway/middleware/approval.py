"""Tool-approval gate (FastAPI middleware + DI surface).

Python port of ``rust/crates/corlinman-gateway/src/middleware/approval.rs``.

The Rust gate runs *between* an agent-emitted ``ToolCall`` and the
plugin runtime that executes it. The Python plane has the same need:
a thin layer that (a) classifies an inbound ``(plugin, tool,
session_key)`` against the configured rule list, (b) auto-approves /
denies / persists-and-waits accordingly, and (c) exposes ``resolve``
for the operator-driven decision endpoint
(``POST /admin/approvals/:id/decide``).

The persistence + waiter coordination already lives in
:class:`corlinman_providers.plugins.ApprovalStore` /
:class:`~corlinman_providers.plugins.ApprovalQueue`. This module is the
gateway-facing facade on top of it: it owns the rule-matching layer the
Rust impl calls ``match_rule_impl`` and a thin :meth:`ApprovalGate.check`
that mirrors the Rust contract.

Surface:
    * :class:`ApprovalDecision` — sum type (``ALLOW`` / ``DENY`` /
      ``TIMEOUT``). Stable string labels match the Rust ``db_label``.
    * :class:`ApprovalMode` — rule mode enum (``AUTO`` / ``PROMPT`` /
      ``DENY``). Mirrors the TOML ``mode = "auto" | "prompt" | "deny"``
      contract.
    * :class:`ApprovalRule` — single rule (``plugin``, ``tool``,
      ``mode``, ``allow_session_keys``).
    * :class:`RuleMatchKind` / :class:`RuleMatch` — outcome of
      classifying a call against the rule list.
    * :class:`ApprovalGate` — the gate itself, parameterised by an
      :class:`~corlinman_providers.plugins.ApprovalStore` (the durable
      backing store) and an optional :class:`~corlinman_providers.plugins.ApprovalQueue`
      for in-process waiter coordination.
    * :func:`require_approval` — :class:`Depends` factory yielding the
      gate so admin routes can call ``await gate.resolve(...)``.
    * :class:`ApprovalMiddleware` — opt-in pass-through middleware that
      attaches the gate to ``request.state.approval_gate`` (some routes
      prefer this over the Depends factory).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from corlinman_providers.plugins.approval import (
    ApprovalDecision as ProviderApprovalDecision,
)
from corlinman_providers.plugins.approval import (
    ApprovalQueue,
    ApprovalRequest,
    ApprovalStore,
)

logger = structlog.get_logger(__name__)


#: Default ``Prompt`` wait deadline when the caller doesn't override it.
#: Matches the 5-minute figure in the Sprint 2 roadmap (Rust:
#: ``DEFAULT_PROMPT_TIMEOUT``).
DEFAULT_PROMPT_TIMEOUT_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Sum types — mirror the Rust enums byte-for-byte on the wire.
# ---------------------------------------------------------------------------


class ApprovalDecision(StrEnum):
    """Outcome of :meth:`ApprovalGate.check`. Stable string values
    double as the DB column / wire label (Rust ``db_label``)."""

    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"

    def to_provider(self) -> ProviderApprovalDecision:
        """Bridge to the lower-level
        :class:`corlinman_providers.plugins.ApprovalDecision` (which uses
        ``allow`` / ``deny`` / ``prompt`` labels — the persistence layer
        only ever stores resolved decisions)."""

        if self is ApprovalDecision.APPROVED:
            return ProviderApprovalDecision.ALLOW
        if self is ApprovalDecision.DENIED:
            return ProviderApprovalDecision.DENY
        # Timeout has no clean provider mirror; surface as DENY for
        # storage purposes — the gateway already knows it was a
        # timeout (only the queue layer needs a binary decision).
        return ProviderApprovalDecision.DENY


class ApprovalMode(StrEnum):
    """Rule mode. Mirrors Rust ``ApprovalMode``; lowercase strings to
    match the TOML ``mode = "auto" | "prompt" | "deny"`` contract."""

    AUTO = "auto"
    PROMPT = "prompt"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRule:
    """One ``[[approvals.rules]]`` entry. Mirrors the Rust
    ``ApprovalRule`` struct.

    ``tool == None`` matches every tool offered by ``plugin``;
    ``allow_session_keys`` short-circuits a ``PROMPT`` rule when the
    call's ``session_key`` is in the list (handy for trusted internal
    sessions / scheduler jobs).
    """

    plugin: str
    tool: str | None = None
    mode: ApprovalMode = ApprovalMode.AUTO
    allow_session_keys: tuple[str, ...] = ()


class RuleMatchKind(StrEnum):
    """Classification of an incoming call against the rule list.

    Distinct from :class:`ApprovalDecision` — a rule match is the
    *evaluation* of the rule list, the decision is what the gate
    ultimately returns to the caller. They line up most of the time but
    a ``PROMPT`` match can resolve as ``APPROVED`` / ``DENIED`` /
    ``TIMEOUT`` depending on what the operator does.
    """

    NO_MATCH = "no_match"
    MATCHED_AUTO = "matched_auto"
    MATCHED_PROMPT = "matched_prompt"
    MATCHED_DENY = "matched_deny"
    MATCHED_WHITELIST = "matched_whitelist"


@dataclass(frozen=True)
class RuleMatch:
    """Result of :func:`match_rule`. ``reason`` is populated only for
    :attr:`RuleMatchKind.MATCHED_DENY` (mirrors the Rust ``reason``
    field on its enum variant)."""

    kind: RuleMatchKind
    reason: str | None = None
    rule: ApprovalRule | None = None


# ---------------------------------------------------------------------------
# Pure rule-matching — port of Rust ``match_rule_impl``.
# ---------------------------------------------------------------------------


def match_rule(
    rules: tuple[ApprovalRule, ...] | list[ApprovalRule],
    plugin: str,
    tool: str,
    session_key: str,
) -> RuleMatch:
    """Pick the most specific rule that applies.

    Specificity order, copied from Rust:
      1. ``plugin == plugin && rule.tool == Some(tool)``.
      2. ``plugin == plugin && rule.tool is None`` (plugin-wide).
      3. ``NO_MATCH``.

    Within each tier the **first** rule in declaration order wins —
    mirrors the TOML authoring expectation that ``[[approvals.rules]]``
    is a list.
    """

    exact: ApprovalRule | None = None
    plugin_wide: ApprovalRule | None = None
    for r in rules:
        if r.plugin != plugin:
            continue
        if r.tool is not None and r.tool == tool and exact is None:
            exact = r
        elif r.tool is None and plugin_wide is None:
            plugin_wide = r

    rule = exact or plugin_wide
    if rule is None:
        return RuleMatch(kind=RuleMatchKind.NO_MATCH)

    if rule.mode is ApprovalMode.AUTO:
        return RuleMatch(kind=RuleMatchKind.MATCHED_AUTO, rule=rule)
    if rule.mode is ApprovalMode.DENY:
        return RuleMatch(
            kind=RuleMatchKind.MATCHED_DENY,
            reason=f"deny rule matched plugin='{rule.plugin}'",
            rule=rule,
        )
    # PROMPT
    if session_key and session_key in rule.allow_session_keys:
        return RuleMatch(kind=RuleMatchKind.MATCHED_WHITELIST, rule=rule)
    return RuleMatch(kind=RuleMatchKind.MATCHED_PROMPT, rule=rule)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class ApprovalGate:
    """Gate around :class:`ApprovalStore` / :class:`ApprovalQueue`.

    Construction takes a rule list snapshot (callable can later call
    :meth:`swap_rules` to hot-reload), a backing store, and an optional
    queue (auto-created if absent). The default prompt timeout matches
    the Rust constant.

    The gate is safe to share across coroutines: ``ApprovalQueue``
    already serialises store access via an :class:`asyncio.Lock` and the
    rule list is replaced atomically by :meth:`swap_rules`.
    """

    def __init__(
        self,
        rules: list[ApprovalRule] | tuple[ApprovalRule, ...] = (),
        *,
        store: ApprovalStore | None = None,
        queue: ApprovalQueue | None = None,
        default_timeout_seconds: float = DEFAULT_PROMPT_TIMEOUT_SECONDS,
    ) -> None:
        self._rules: tuple[ApprovalRule, ...] = tuple(rules)
        self._store = store or ApprovalStore()
        self._queue = queue or ApprovalQueue(store=self._store)
        self._default_timeout = default_timeout_seconds

    # ---- rule list management ---------------------------------------------

    @property
    def rules(self) -> tuple[ApprovalRule, ...]:
        """Snapshot of the current rule list. Cheap; immutable tuple."""

        return self._rules

    def swap_rules(self, rules: list[ApprovalRule] | tuple[ApprovalRule, ...]) -> None:
        """Replace the rule snapshot. Existing in-flight waits are not
        disturbed — they already captured the outcome of the rule that
        matched their call (mirrors Rust ``swap_rules``)."""

        self._rules = tuple(rules)

    def match_rule(self, plugin: str, tool: str, session_key: str) -> RuleMatch:
        """Public alias for :func:`match_rule` against the current rule
        snapshot. Exposed so callers don't need to import the free
        function separately."""

        return match_rule(self._rules, plugin, tool, session_key)

    # ---- backing handles ---------------------------------------------------

    @property
    def store(self) -> ApprovalStore:
        return self._store

    @property
    def queue(self) -> ApprovalQueue:
        return self._queue

    @property
    def default_timeout_seconds(self) -> float:
        return self._default_timeout

    # ---- the heart of the gate --------------------------------------------

    async def check(
        self,
        *,
        session_key: str,
        plugin: str,
        tool: str,
        args_json: bytes | str = b"",
        timeout_seconds: float | None = None,
        call_id: str | None = None,
    ) -> tuple[ApprovalDecision, str]:
        """Ask whether this tool call should execute.

        Mirrors the Rust ``ApprovalGate::check`` contract:
          1. Match against the rule list.
          2. ``MATCHED_AUTO`` / ``MATCHED_WHITELIST`` → :attr:`APPROVED`.
          3. ``MATCHED_DENY`` → persist + return :attr:`DENIED`.
          4. ``MATCHED_PROMPT`` → enqueue + park on the queue until
             either :meth:`resolve` fires or the timeout elapses.
          5. ``NO_MATCH`` → :attr:`APPROVED` (default-allow), matching
             the Rust ``NoMatch`` arm.

        Returns ``(decision, call_id)`` so the caller can correlate
        with the queued row when needed. ``call_id`` is auto-minted
        when not supplied.
        """

        cid = call_id or _new_call_id()
        verdict = self.match_rule(plugin, tool, session_key)

        if verdict.kind in (
            RuleMatchKind.NO_MATCH,
            RuleMatchKind.MATCHED_AUTO,
            RuleMatchKind.MATCHED_WHITELIST,
        ):
            return ApprovalDecision.APPROVED, cid

        args_preview = _preview_args(args_json)

        if verdict.kind is RuleMatchKind.MATCHED_DENY:
            reason = verdict.reason or "deny rule matched"
            # Persist a decided row so the admin UI's history tab shows
            # this denial alongside operator-driven ones.
            request = ApprovalRequest(
                call_id=cid,
                plugin=plugin,
                tool=tool,
                args_preview=args_preview,
                session_key=session_key,
                reason=reason,
            )
            try:
                await self._store.insert(request)
                await self._store.decide(cid, ProviderApprovalDecision.DENY)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "approval.deny.persist_failed",
                    id=cid,
                    plugin=plugin,
                    tool=tool,
                    error=str(exc),
                )
            return ApprovalDecision.DENIED, cid

        # MATCHED_PROMPT — enqueue + wait.
        request = ApprovalRequest(
            call_id=cid,
            plugin=plugin,
            tool=tool,
            args_preview=args_preview,
            session_key=session_key,
            reason="approval rule matched: prompt",
        )
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        try:
            provider_decision = await self._queue.enqueue_and_wait(
                request, timeout=timeout
            )
        except asyncio.TimeoutError:
            # Persist the timeout outcome so the admin UI sees the row
            # close out — matches the Rust ``persist_decision`` path.
            with _swallow():
                await self._store.decide(cid, ProviderApprovalDecision.DENY)
            return ApprovalDecision.TIMEOUT, cid

        if provider_decision is ProviderApprovalDecision.ALLOW:
            return ApprovalDecision.APPROVED, cid
        return ApprovalDecision.DENIED, cid

    # ---- operator-driven resolution ---------------------------------------

    async def resolve(self, call_id: str, decision: ApprovalDecision) -> None:
        """Operator-driven decision path. Updates the store and wakes
        any parked :meth:`check` future. Idempotent against already-
        decided rows (matches Rust behaviour).

        Raises :class:`LookupError` when ``call_id`` is unknown so the
        admin route can translate it to a 404. The Rust impl uses its
        own ``NotFound`` error; the Python port keeps the stdlib
        equivalent.
        """

        record = await self._store.get(call_id)
        if record is None:
            raise LookupError(call_id)

        # Already-decided is a no-op (idempotent).
        if record.decision is not None:
            return

        provider_decision = decision.to_provider()
        await self._queue.decide(call_id, provider_decision)

    async def list_pending(self) -> list[Any]:
        """Convenience: forward to the queue's pending list. Exposed so
        admin routes don't have to dig through ``gate.queue.pending``."""

        return await self._queue.pending()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex}"


def _preview_args(args_json: bytes | str, *, max_bytes: int = 512) -> str:
    """Truncate args_json to a safe preview size for the admin UI.

    Mirrors the Rust ``preview_args`` cap (512 bytes). Binary payloads
    surface as best-effort UTF-8 with ``replace`` errors so the
    persistence layer never crashes on a non-UTF8 byte.
    """

    if isinstance(args_json, str):
        s = args_json
    else:
        try:
            s = args_json.decode("utf-8")
        except UnicodeDecodeError:
            s = args_json.decode("utf-8", errors="replace")
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    # Truncate at a codepoint boundary.
    truncated = s.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + "…"  # …


class _swallow:
    """Tiny ``contextlib.suppress(Exception)`` clone (avoids importing
    ``contextlib`` for one site)."""

    def __enter__(self) -> "_swallow":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            logger.debug("approval.swallowed", error=str(exc))
            return True
        return False


# ---------------------------------------------------------------------------
# Middleware + Depends
# ---------------------------------------------------------------------------


@dataclass
class ApprovalMiddlewareState:
    """Cloneable handle to the gate exposed to handlers."""

    gate: ApprovalGate | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class ApprovalMiddleware(BaseHTTPMiddleware):
    """Pass-through middleware that attaches the configured
    :class:`ApprovalGate` to ``request.state.approval_gate``.

    The gate itself does not gate HTTP requests — it gates *tool calls*
    inside the agent runtime. The middleware just plumbs the handle so
    admin endpoints can resolve approvals without going through the
    Depends factory.
    """

    def __init__(
        self,
        app: ASGIApp,
        state: ApprovalMiddlewareState | None = None,
    ) -> None:
        super().__init__(app)
        self._state = state or ApprovalMiddlewareState()

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        state = (
            getattr(request.app.state, "approval", None)
            if hasattr(request.app, "state")
            else None
        )
        if not isinstance(state, ApprovalMiddlewareState):
            state = self._state
        request.state.approval_gate = state.gate
        return await call_next(request)


def install_approval_middleware(
    app: Any,
    *,
    gate: ApprovalGate | None = None,
) -> ApprovalMiddlewareState:
    """Attach :class:`ApprovalMiddleware` to ``app`` and publish the
    gate on ``app.state.approval`` so :func:`require_approval` can
    pick it up."""

    state = ApprovalMiddlewareState(gate=gate)
    app.state.approval = state
    app.add_middleware(ApprovalMiddleware, state=state)
    return state


def require_approval() -> Any:
    """:class:`Depends` factory that yields the configured gate.

    Usage::

        @router.post("/admin/approvals/{call_id}/decide")
        async def decide(
            call_id: str,
            body: DecideBody,
            gate: ApprovalGate = Depends(require_approval()),
        ):
            await gate.resolve(call_id, body.decision)

    Raises HTTP 503 ``approvals_disabled`` when no gate is wired —
    matches the Rust ``approvals_disabled`` envelope used by
    ``POST /v1/chat/completions/{turn_id}/approve``.
    """

    def dependency(request: Request) -> ApprovalGate:
        cached = getattr(request.state, "approval_gate", None)
        if isinstance(cached, ApprovalGate):
            return cached

        state = getattr(request.app.state, "approval", None)
        if isinstance(state, ApprovalMiddlewareState) and state.gate is not None:
            return state.gate

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "approvals_disabled",
                "message": "approval gate is not configured on this gateway",
            },
        )

    return Depends(dependency)


__all__ = [
    "DEFAULT_PROMPT_TIMEOUT_SECONDS",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalMiddleware",
    "ApprovalMiddlewareState",
    "ApprovalMode",
    "ApprovalRule",
    "RuleMatch",
    "RuleMatchKind",
    "install_approval_middleware",
    "match_rule",
    "require_approval",
]
