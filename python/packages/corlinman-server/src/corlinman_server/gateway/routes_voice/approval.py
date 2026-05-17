"""Tool-approval bridge for the ``/voice`` route.

Direct Python port of
``rust/crates/corlinman-gateway/src/routes/voice/approval.rs``. When
the upstream provider yields a :class:`VoiceEvent.ToolCall`, the
WebSocket session driver:

1. Halts TTS output to the client so the user doesn't hear the
   assistant continuing while operator approval is pending.
2. Emits ``tool_approval_required`` as a JSON control frame so the
   client UI can render the pending banner.
3. Files an approval request via the existing
   :class:`corlinman_providers.plugins.ApprovalStore` / queue (chat
   surface uses the same gate from the agent-loop hot path). The queue
   parks a coroutine on a future until an operator decides via the
   admin UI or the configured timeout elapses.
4. Resumes on the decision:

   * **Approve** — sends ``ApproveTool(approve=True)`` upstream and
     emits an ``agent_text`` "Approved, continuing..." breadcrumb.
   * **Deny / Timeout** — sends ``ApproveTool(approve=False)`` plus
     ``Interrupt`` so the upstream TTS buffer is flushed; emits an
     apology ``agent_text`` so the user knows the tool was blocked.

Integration with :class:`ApprovalStore`:

The Rust side has its own ``ApprovalGate`` middleware with three modes
(``Auto`` / ``Prompt`` / ``Deny``) plus session-key allowlists; the
Python ``ApprovalStore`` exposes a simpler ``insert`` / ``decide`` /
``wait`` shape. The Python bridge wraps either shape: pass a queue
that supports ``enqueue_and_wait(request, timeout=…)`` (e.g.
:class:`corlinman_providers.plugins.ApprovalQueue`) and the bridge
will block on it; pass ``None`` to opt out and auto-approve every tool
call (mirrors the Rust ``NoMatch → Approved`` default).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from corlinman_server.gateway.routes_voice.framing import ServerControl
from corlinman_server.gateway.routes_voice.provider import ProviderCommand

logger = logging.getLogger("corlinman_server.gateway.routes_voice.approval")


VOICE_TOOL_PLUGIN: Final[str] = "voice"
"""Plugin string used when filing voice-surface approvals against the
gate. Operators can pre-approve / pre-deny every voice tool call by
filtering on this plugin name."""

APPROVAL_RESUME_TEXT: Final[str] = "Approved, continuing."
"""Default TTS phrase emitted as ``agent_text`` after an approval is
granted."""

APPROVAL_DENIED_TEXT: Final[str] = (
    "Sorry, I'm not allowed to use that tool right now."
)
"""Default TTS phrase after a deny — the user should know the tool was
blocked rather than the call mysteriously going silent."""

APPROVAL_TIMEOUT_TEXT: Final[str] = (
    "Sorry, I didn't get approval in time to use that tool."
)
"""Default TTS phrase after a timeout."""


# The Python-side ``ApprovalQueue`` (from corlinman_providers.plugins)
# exposes ``enqueue_and_wait`` returning an ``ApprovalDecision`` enum
# with members ``ALLOW`` / ``DENY`` / ``PROMPT``. We don't import that
# class directly to keep this module testable without a circular
# dependency back to the providers package — instead we duck-type the
# queue via a runtime-checkable Protocol.


class ApprovalDecisionKind:
    """Mirror of :class:`corlinman_providers.plugins.ApprovalDecision`'s
    string values. Avoids importing the enum at module-load so the
    bridge stays usable in tests that stub the queue with a fake."""

    APPROVED: Final[str] = "allow"
    DENIED: Final[str] = "deny"
    TIMEOUT: Final[str] = "timeout"  # synthesised locally on asyncio.TimeoutError


@runtime_checkable
class _ApprovalQueueLike(Protocol):
    """Duck-typed view of
    :class:`corlinman_providers.plugins.ApprovalQueue`. The bridge
    calls ``enqueue_and_wait(request, timeout=…)`` and inspects the
    decision's ``.value`` (the underlying StrEnum's string form).
    """

    async def enqueue_and_wait(
        self, request: Any, *, timeout: float | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class ApprovalOutcome:
    """One end of the gate handoff. The WebSocket session driver calls
    :meth:`VoiceApprovalBridge.handle_tool_call` once per provider
    ``ToolCall`` event and processes the outputs:

    * ``server_frames`` — forwarded to the client as JSON text frames
      in order. The first entry is always
      :class:`ServerControl.ToolApprovalRequired` (so the client UI
      banner shows up before the wait); later entries are the
      ``AgentText`` resume/denial breadcrumb.
    * ``provider_commands`` — forwarded to the upstream provider in
      order. On approve, this is ``[ApproveTool(True)]``; on deny /
      timeout it's ``[ApproveTool(False), Interrupt]`` so the upstream
      TTS buffer is flushed before any apology audio is generated.
    * ``decision`` — the final decision string the gate returned
      (one of :class:`ApprovalDecisionKind` constants), surfaced
      separately so the caller can update ``voice_sessions.end_reason``
      if a denial / cancellation should also terminate the session.
    """

    server_frames: list[ServerControl]
    provider_commands: list[ProviderCommand]
    decision: str


def _coerce_decision(decision: Any) -> str:
    """Translate a ``ApprovalQueue.enqueue_and_wait`` result into the
    bridge's three-value decision string. Accepts the providers
    ``ApprovalDecision`` StrEnum (whose ``.value`` is the underlying
    string) or a bare string.

    ``PROMPT`` is treated as a denial — the queue should never resolve
    with ``PROMPT`` (that's an initial state), but if it does the safe
    fallback is to refuse.
    """
    value = getattr(decision, "value", decision)
    if isinstance(value, str):
        if value == "allow":
            return ApprovalDecisionKind.APPROVED
        if value in ("deny", "prompt"):
            return ApprovalDecisionKind.DENIED
    # Defensive: unknown payload from a custom queue → treat as denial.
    return ApprovalDecisionKind.DENIED


class VoiceApprovalBridge:
    """Optional handle to the approval queue, scoped to one voice
    session. ``queue=None`` (or :meth:`no_gate`) means the bridge
    auto-approves every tool call without prompting — same default
    ``NoMatch → Approved`` semantics as the Rust chat surface.

    Construct via :meth:`no_gate` for the "no gate wired" path or
    :meth:`with_queue` to wire a real
    :class:`corlinman_providers.plugins.ApprovalQueue` (or any
    duck-typed equivalent for tests).
    """

    def __init__(
        self,
        queue: _ApprovalQueueLike | None,
        session_key: str,
        *,
        timeout_seconds: float | None = 300.0,
    ) -> None:
        self._queue = queue
        self._session_key = session_key
        self._timeout_seconds = timeout_seconds

    @classmethod
    def no_gate(
        cls, session_key: str, *, timeout_seconds: float | None = 300.0
    ) -> VoiceApprovalBridge:
        return cls(None, session_key, timeout_seconds=timeout_seconds)

    @classmethod
    def with_queue(
        cls,
        queue: _ApprovalQueueLike,
        session_key: str,
        *,
        timeout_seconds: float | None = 300.0,
    ) -> VoiceApprovalBridge:
        return cls(queue, session_key, timeout_seconds=timeout_seconds)

    @property
    def session_key(self) -> str:
        return self._session_key

    async def handle_tool_call(
        self,
        approval_id: str,
        tool: str,
        args_json: Any,
        cancel: asyncio.Event | None = None,
    ) -> ApprovalOutcome:
        """Drive one tool-call through the approval lifecycle.

        ``approval_id`` and ``tool`` come from the provider event;
        ``args_json`` is the raw argument payload (JSON-serialisable).
        ``cancel`` is an optional per-session cancellation Event —
        closing the WebSocket sets it so the bridge can return early
        without leaking the parked future.

        Returns the :class:`ApprovalOutcome` to apply to client +
        provider channels; never blocks beyond the gate's own timeout.
        """
        pause_frame = ServerControl(
            type=ServerControl.TOOL_APPROVAL_REQUIRED,
            approval_id=approval_id,
            tool=tool,
            args=args_json if args_json is not None else {},
        )

        if self._queue is None:
            # No gate wired: approve immediately, but still emit the
            # pause frame so client UX stays consistent across
            # configurations. The agent_text breadcrumb confirms the
            # resume to the user.
            return ApprovalOutcome(
                server_frames=[
                    pause_frame,
                    ServerControl(
                        type=ServerControl.AGENT_TEXT, text=APPROVAL_RESUME_TEXT
                    ),
                ],
                provider_commands=[
                    ProviderCommand.approve_tool(approval_id, approve=True)
                ],
                decision=ApprovalDecisionKind.APPROVED,
            )

        # Build the request payload. Lazy-import the providers
        # dataclass so the bridge stays usable in tests that stub the
        # queue without dragging the providers package in.
        try:
            from corlinman_providers.plugins import ApprovalRequest
        except ImportError:  # pragma: no cover — providers package unavailable
            ApprovalRequest = None  # type: ignore[assignment]

        args_preview = _args_preview(args_json)
        if ApprovalRequest is not None:
            request: Any = ApprovalRequest(
                call_id=approval_id,
                plugin=VOICE_TOOL_PLUGIN,
                tool=tool,
                args_preview=args_preview,
                session_key=self._session_key,
                reason="voice tool call",
            )
        else:
            # Fallback for tests that stub the queue with a fake.
            request = {
                "call_id": approval_id,
                "plugin": VOICE_TOOL_PLUGIN,
                "tool": tool,
                "args_preview": args_preview,
                "session_key": self._session_key,
                "reason": "voice tool call",
            }

        # Race the queue wait against the cancel event. The
        # `enqueue_and_wait` call already supports its own ``timeout``
        # kwarg so the gate persists a timeout row; we wrap with
        # ``asyncio.wait`` so a cancel event also unsticks us.
        wait_task = asyncio.ensure_future(
            self._queue.enqueue_and_wait(request, timeout=self._timeout_seconds)
        )
        cancel_task: asyncio.Task[None] | None = None
        if cancel is not None:
            cancel_task = asyncio.ensure_future(cancel.wait())

        decision_kind: str
        try:
            wait_for = {wait_task} | ({cancel_task} if cancel_task is not None else set())
            done, pending = await asyncio.wait(
                wait_for, return_when=asyncio.FIRST_COMPLETED
            )

            if cancel_task is not None and cancel_task in done:
                # Cancellation (client disconnect). Cancel the queue
                # wait — the queue's persistent row will still record
                # its own timeout decision when it fires.
                wait_task.cancel()
                logger.warning(
                    "voice: approval cancelled by client disconnect: "
                    "approval_id=%s tool=%s",
                    approval_id,
                    tool,
                )
                return ApprovalOutcome(
                    server_frames=[
                        pause_frame,
                        ServerControl(
                            type=ServerControl.ERROR,
                            code="approval_cancelled",
                            message="approval cancelled before operator decision",
                        ),
                    ],
                    provider_commands=[
                        ProviderCommand.approve_tool(approval_id, approve=False),
                        ProviderCommand.interrupt(),
                    ],
                    decision=ApprovalDecisionKind.DENIED,
                )

            # The queue future completed; cancel the cancel-watch.
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()

            try:
                decision_raw = wait_task.result()
            except asyncio.TimeoutError:
                logger.warning(
                    "voice: approval timed out: approval_id=%s tool=%s",
                    approval_id,
                    tool,
                )
                return ApprovalOutcome(
                    server_frames=[
                        pause_frame,
                        ServerControl(
                            type=ServerControl.AGENT_TEXT,
                            text=APPROVAL_TIMEOUT_TEXT,
                        ),
                    ],
                    provider_commands=[
                        ProviderCommand.approve_tool(approval_id, approve=False),
                        ProviderCommand.interrupt(),
                    ],
                    decision=ApprovalDecisionKind.TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "voice: approval queue errored: approval_id=%s tool=%s err=%s",
                    approval_id,
                    tool,
                    exc,
                )
                return ApprovalOutcome(
                    server_frames=[
                        pause_frame,
                        ServerControl(
                            type=ServerControl.ERROR,
                            code="approval_failed",
                            message=f"approval queue errored: {exc}",
                        ),
                    ],
                    provider_commands=[
                        ProviderCommand.approve_tool(approval_id, approve=False),
                        ProviderCommand.interrupt(),
                    ],
                    decision=ApprovalDecisionKind.DENIED,
                )

            decision_kind = _coerce_decision(decision_raw)
        finally:
            # Best-effort cleanup of any still-pending watcher.
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()

        if decision_kind == ApprovalDecisionKind.APPROVED:
            logger.debug(
                "voice: approval granted; resuming TTS: approval_id=%s tool=%s",
                approval_id,
                tool,
            )
            return ApprovalOutcome(
                server_frames=[
                    pause_frame,
                    ServerControl(
                        type=ServerControl.AGENT_TEXT, text=APPROVAL_RESUME_TEXT
                    ),
                ],
                provider_commands=[
                    ProviderCommand.approve_tool(approval_id, approve=True)
                ],
                decision=ApprovalDecisionKind.APPROVED,
            )

        # Denied (or unrecognised → mapped to denial)
        logger.debug(
            "voice: approval denied; flushing TTS: approval_id=%s tool=%s",
            approval_id,
            tool,
        )
        return ApprovalOutcome(
            server_frames=[
                pause_frame,
                ServerControl(
                    type=ServerControl.AGENT_TEXT, text=APPROVAL_DENIED_TEXT
                ),
            ],
            provider_commands=[
                ProviderCommand.approve_tool(approval_id, approve=False),
                ProviderCommand.interrupt(),
            ],
            decision=ApprovalDecisionKind.DENIED,
        )


def _args_preview(args_json: Any) -> str:
    """Compact JSON-ish preview of the tool-call args. The admin UI
    renders the full args separately; this is just the
    ``pending_approvals.args_preview`` column. Length-capped so a
    pathological call doesn't bloat the row.
    """
    try:
        text = json.dumps(args_json, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = repr(args_json)
    if len(text) > 256:
        text = text[:253] + "..."
    return text


__all__ = [
    "VOICE_TOOL_PLUGIN",
    "APPROVAL_RESUME_TEXT",
    "APPROVAL_DENIED_TEXT",
    "APPROVAL_TIMEOUT_TEXT",
    "ApprovalDecisionKind",
    "ApprovalOutcome",
    "VoiceApprovalBridge",
]
