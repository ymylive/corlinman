"""``GET /v1/voice`` — realtime audio WebSocket endpoint.

Python port of ``rust/crates/corlinman-gateway/src/routes/voice/mod.rs``
plus the in-session pump from the Rust ``bridge.rs`` collapsed onto a
single FastAPI :class:`WebSocket` handler.

This module is the *tying-together* point for the voice surface — every
collaborator (:mod:`.framing` / :mod:`.cost` / :mod:`.budget` /
:mod:`.approval` / :mod:`.provider` / :mod:`.persistence`) lives in its
own file. This one wires them onto the FastAPI side:

* :func:`router` returns the FastAPI :class:`APIRouter` exposing
  ``websocket("/v1/voice")``.
* :class:`VoiceState` carries the live config + injected providers /
  stores / sinks. Construction sites build one of these once at boot.
* :func:`run_voice_session` is the per-connection driver: it negotiates
  the subprotocol, runs the budget gate, opens a provider session,
  pumps audio + control frames in both directions, surfaces transcript
  / tool-call / budget events to the client, and on cleanup writes the
  ``voice_sessions`` row + flushes the spend store.

Hard contract: this file does **not** mutate state outside this
subpackage. The middleware-style approval gate is supplied as an
optional :class:`ApprovalQueue`-shaped object on the state (no new
import on the gateway middleware tree).

Threading model: every collaborator is asyncio-native; the only thread
sync is the :class:`InMemoryVoiceSpend` :class:`threading.Lock`. The
inbound / outbound pumps run as two child :class:`asyncio.Task`s under
a :class:`asyncio.TaskGroup`-equivalent ``gather`` so a failure in one
half cancels the other.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect, WebSocketState

from corlinman_server.gateway.routes_voice.approval import (
    APPROVAL_DENIED_TEXT,
    ApprovalDecisionKind,
    ApprovalOutcome,
    VoiceApprovalBridge,
)
from corlinman_server.gateway.routes_voice.budget import (
    BudgetEnforcer,
    BudgetTickAction,
    terminate_reason_to_code,
    terminate_reason_to_end_reason,
    terminate_reason_to_message,
)
from corlinman_server.gateway.routes_voice.cost import (
    BudgetDecision,
    BudgetDenyReason,
    CLOSE_CODE_BUDGET,
    CLOSE_CODE_MAX_SESSION,
    InMemoryVoiceSpend,
    VoiceConfig,
    VoiceSpend,
    evaluate_budget,
    next_utc_midnight,
    now_unix_secs,
    utc_day_epoch,
)
from corlinman_server.gateway.routes_voice.framing import (
    AudioFrameError,
    ClientControl,
    ControlParseError,
    ServerControl,
    SUBPROTOCOL,
    SubprotocolDecision,
    accept_subprotocol,
    encode_server_control,
    parse_audio_frame,
    parse_client_control,
)
from corlinman_server.gateway.routes_voice.persistence import (
    VoiceEndReason,
    VoiceSessionEnd,
    VoiceSessionStart,
    VoiceSessionStore,
    VoiceTranscriptSink,
    audio_path_for,
)
from corlinman_server.gateway.routes_voice.provider import (
    ProviderCommand,
    ProviderEndReason,
    VoiceEvent,
    VoiceProvider,
    VoiceProviderSession,
    VoiceSessionStartParams,
)

__all__ = [
    "CLOSE_CODE_NORMAL",
    "CLOSE_CODE_PROTOCOL_ERROR",
    "CLOSE_CODE_PROVIDER_ERROR",
    "CLOSE_CODE_VOICE_DISABLED",
    "DEFAULT_START_TIMEOUT_SECONDS",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "VoiceRouterConfig",
    "VoiceState",
    "router",
    "run_voice_session",
]


logger = logging.getLogger("corlinman_server.gateway.routes_voice")


# ---------------------------------------------------------------------------
# Close codes
# ---------------------------------------------------------------------------

CLOSE_CODE_NORMAL: int = 1000
"""RFC 6455 normal closure (graceful end)."""

CLOSE_CODE_PROTOCOL_ERROR: int = 1002
"""RFC 6455 protocol error — bad subprotocol, missing ``start`` frame,
or an unrecoverable control-frame parse failure."""

CLOSE_CODE_VOICE_DISABLED: int = 4000
"""Application-level close code: ``[voice] enabled = false`` at the
moment the upgrade completed. Pre-upgrade this is surfaced as an HTTP
503; mid-upgrade only used if a hot-reload flips the flag between
accept and the budget check."""

CLOSE_CODE_PROVIDER_ERROR: int = 4003
"""Application-level close code: the upstream provider failed to start
or terminated with an error mid-session."""

DEFAULT_TICK_INTERVAL_SECONDS: float = 1.0
"""Per-design tick cadence for the budget enforcer. Once per second is
the same as the Rust implementation."""

DEFAULT_START_TIMEOUT_SECONDS: float = 5.0
"""How long to wait for the client's first ``start`` control frame
before treating the session as a protocol error and closing 1002.
Matches the Rust route handler's 5-second timeout."""


# ---------------------------------------------------------------------------
# Router-level config + state
# ---------------------------------------------------------------------------


class VoiceRouterConfig(BaseModel):
    """Pydantic v2 carrier for the live ``[voice]`` config snapshot.

    The route handler reads a snapshot per request so a hot-reload that
    flips ``enabled`` (or any of the budget / sample-rate knobs) takes
    effect on the next connect without rebuilding the router.

    Mirrors :class:`corlinman_server.gateway.routes_voice.cost.VoiceConfig`
    one-for-one but as a Pydantic model so callers wiring this from
    ``config.toml`` get validation for free. :meth:`to_cost_config`
    projects back onto the frozen dataclass the cost / budget layers
    consume.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    enabled: bool = False
    budget_minutes_per_tenant_per_day: int = Field(default=0, ge=0)
    max_session_seconds: int = Field(default=0, ge=0)
    provider_alias: str = ""
    sample_rate_hz_in: int = Field(default=16_000, gt=0)
    sample_rate_hz_out: int = Field(default=24_000, gt=0)
    retain_audio: bool = False
    default_tenant: str = "default"

    def to_cost_config(self) -> VoiceConfig:
        return VoiceConfig(
            enabled=self.enabled,
            budget_minutes_per_tenant_per_day=self.budget_minutes_per_tenant_per_day,
            max_session_seconds=self.max_session_seconds,
            provider_alias=self.provider_alias,
            sample_rate_hz_in=self.sample_rate_hz_in,
            sample_rate_hz_out=self.sample_rate_hz_out,
            retain_audio=self.retain_audio,
        )


ConfigLoader = Callable[[], VoiceRouterConfig]
"""Live ``[voice]`` config snapshot loader. The handler calls this on
every connect — wire a closure that reads the current ``ArcSwap`` /
``RWLock`` / ``contextvar`` shaped snapshot."""


@dataclass
class VoiceState:
    """State injected into the FastAPI WebSocket handler. Mirrors the
    Rust ``VoiceState`` struct field-for-field.

    Construction is via plain :func:`dataclasses.field` defaults; the
    callsite (gateway boot) wires the provider, session store, transcript
    sink, approval queue, and config loader.

    The default :attr:`spend` is :class:`InMemoryVoiceSpend`. Multi-tenant
    deployments swap to a SQLite-backed :class:`VoiceSpend` impl behind
    the Protocol without touching this state.

    The default :attr:`tenant_resolver` reads the ``X-Tenant-Id``
    header; if absent it falls back to ``config.default_tenant``. Wire
    a custom resolver to thread the session-token-derived tenant.
    """

    config_loader: ConfigLoader
    spend: VoiceSpend = field(default_factory=InMemoryVoiceSpend)
    provider: VoiceProvider | None = None
    session_store: VoiceSessionStore | None = None
    transcript_sink: VoiceTranscriptSink | None = None
    approval_queue: Any | None = None
    data_dir: Path = field(default_factory=lambda: Path("."))
    tenant_resolver: Callable[[WebSocket, VoiceRouterConfig], str] | None = None
    tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS
    start_timeout_seconds: float = DEFAULT_START_TIMEOUT_SECONDS

    def resolve_tenant(self, websocket: WebSocket, cfg: VoiceRouterConfig) -> str:
        """Pick the tenant slug for this connection. Default = header
        wins over the config-supplied fallback."""
        if self.tenant_resolver is not None:
            return self.tenant_resolver(websocket, cfg)
        raw = websocket.headers.get("x-tenant-id")
        if raw and raw.strip():
            return raw.strip()
        return cfg.default_tenant or "default"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def router(state: VoiceState | None = None) -> APIRouter:
    """Build the FastAPI sub-router exposing
    ``websocket("/v1/voice")``.

    When ``state`` is ``None`` the route accepts the upgrade then
    immediately closes with :data:`CLOSE_CODE_VOICE_DISABLED` — matching
    the Rust stub behaviour for compositions that never wire live state.
    """

    api = APIRouter()

    @api.websocket("/v1/voice")
    async def voice_endpoint(websocket: WebSocket) -> None:
        await run_voice_session(websocket, state)

    return api


# ---------------------------------------------------------------------------
# Per-connection driver
# ---------------------------------------------------------------------------


async def run_voice_session(
    websocket: WebSocket, state: VoiceState | None
) -> None:
    """Drive one ``/v1/voice`` connection from upgrade through cleanup.

    This is the entry point the FastAPI endpoint forwards to. Split out
    of the route closure so tests can drive it against an in-memory
    :class:`WebSocket` double.

    Lifecycle:

    1. Validate subprotocol against the ``Sec-WebSocket-Protocol`` header
       *before* accepting the upgrade. A mismatch closes 1002.
    2. Refuse if ``[voice] enabled = false`` (close 4000).
    3. Run the per-tenant daily budget gate. Over budget → close 4002.
    4. Accept the upgrade with the negotiated subprotocol echoed back.
    5. Read the first control frame (``start``) within the start
       timeout. A missing / malformed first frame closes 1002.
    6. Open a provider session. A provider open failure emits an
       ``error`` event then closes 4003 (``provider_error``).
    7. Send the ``started`` event and persist the
       :class:`VoiceSessionStart` row.
    8. Run two concurrent pumps — inbound (client → provider) and
       outbound (provider → client) — plus a ticker that drives the
       :class:`BudgetEnforcer` once per second. The pumps coordinate
       via a single :class:`asyncio.Event` (``cancel``).
    9. On any exit path: flush the spend store, write the
       :class:`VoiceSessionEnd` row, and close the WebSocket if it's
       still open.
    """
    # ---- subprotocol negotiation (pre-accept) -------------------------
    offered = websocket.headers.get("sec-websocket-protocol")
    decision = accept_subprotocol(offered)
    if not decision.is_accept:
        # ``starlette`` lets us close before accepting. Some clients
        # only see the close-without-accept as a generic upgrade
        # failure; the design accepts that — pre-upgrade rejection is
        # the same shape as the Rust 400.
        logger.warning(
            "voice: subprotocol rejected offered=%r reason=%s",
            offered,
            decision.reason,
        )
        await websocket.close(
            code=CLOSE_CODE_PROTOCOL_ERROR,
            reason=decision.reason or "subprotocol rejected",
        )
        return

    accepted_subprotocol = decision.accepted or SUBPROTOCOL

    # ---- live config + feature flag -----------------------------------
    if state is None:
        logger.debug("voice: no state wired; closing voice_disabled")
        await websocket.accept(subprotocol=accepted_subprotocol)
        await websocket.close(
            code=CLOSE_CODE_VOICE_DISABLED, reason="voice not configured"
        )
        return

    try:
        cfg = state.config_loader()
    except Exception as exc:  # noqa: BLE001 — operator config-loader failure
        logger.exception("voice: config loader raised; closing")
        await websocket.accept(subprotocol=accepted_subprotocol)
        await websocket.close(
            code=CLOSE_CODE_VOICE_DISABLED, reason=f"config error: {exc}"
        )
        return

    if not cfg.enabled:
        logger.debug("voice: feature flag off; closing")
        await websocket.accept(subprotocol=accepted_subprotocol)
        await websocket.close(
            code=CLOSE_CODE_VOICE_DISABLED, reason="voice disabled"
        )
        return

    # ---- per-tenant daily budget gate ---------------------------------
    tenant = state.resolve_tenant(websocket, cfg)
    cost_cfg = cfg.to_cost_config()
    now = now_unix_secs()
    day_epoch = utc_day_epoch(now)
    reset_at = next_utc_midnight(now)
    today = state.spend.snapshot(tenant, day_epoch)
    decision_budget = evaluate_budget(cost_cfg, today, reset_at)
    if not decision_budget.allowed:
        await _close_budget_exhausted(
            websocket, accepted_subprotocol, decision_budget.reason, reset_at
        )
        return

    # Record a session-start counter regardless of subsequent failure
    # so the audit table sees one row per attempt.
    state.spend.record_session_start(tenant, day_epoch)

    # ---- accept the upgrade with the negotiated subprotocol -----------
    await websocket.accept(subprotocol=accepted_subprotocol)

    session_id = f"voice-{uuid.uuid4()}"

    # If no provider is wired, send a `started` event then close 1000
    # (matches the Rust iter-2 stub path so probes still get a useful
    # signal that the gate / negotiation succeeded).
    if state.provider is None:
        logger.debug("voice: no provider wired; sending stub started + closing")
        await _send_server(
            websocket,
            ServerControl(
                type=ServerControl.STARTED,
                session_id=session_id,
                provider=cfg.provider_alias,
            ),
        )
        await _safe_close(
            websocket, CLOSE_CODE_NORMAL, "voice provider not configured"
        )
        return

    # ---- read the mandatory `start` control frame ---------------------
    try:
        start_frame, deferred = await _read_start_frame(
            websocket, state.start_timeout_seconds
        )
    except _StartTimeout:
        logger.warning("voice: client did not send start within timeout")
        await _safe_close(
            websocket, CLOSE_CODE_PROTOCOL_ERROR, "missing start frame"
        )
        return
    except _StartMalformed as exc:
        logger.warning("voice: start frame malformed: %s", exc)
        await _safe_close(
            websocket, CLOSE_CODE_PROTOCOL_ERROR, f"invalid start: {exc}"
        )
        return
    except _StartDisconnect:
        logger.debug("voice: client disconnected before start")
        return

    session_key = start_frame.session_key or session_id
    agent_id = start_frame.agent_id
    sample_rate_in = (
        start_frame.sample_rate_hz
        if start_frame.sample_rate_hz
        else cfg.sample_rate_hz_in
    )

    # ---- open the provider session ------------------------------------
    params = VoiceSessionStartParams(
        session_id=session_id,
        provider_alias=cfg.provider_alias,
        sample_rate_hz_in=sample_rate_in,
        sample_rate_hz_out=cfg.sample_rate_hz_out,
        voice_id=None,
        agent_id=agent_id,
    )
    try:
        provider_session = await state.provider.open(params)
    except Exception as exc:  # noqa: BLE001 — provider open is best-effort
        logger.exception("voice: provider open failed")
        await _send_server(
            websocket,
            ServerControl(
                type=ServerControl.ERROR,
                code="provider_error",
                message=f"provider open failed: {exc}",
            ),
        )
        await _safe_close(
            websocket, CLOSE_CODE_PROVIDER_ERROR, "provider open failed"
        )
        await _record_session_start_end(
            state, session_id, tenant, session_key, agent_id, cfg.provider_alias,
            started_at_unix=now, ended_at_unix=now_unix_secs(),
            duration_secs=0, audio_path=None, transcript_text=None,
            end_reason=VoiceEndReason.START_FAILED,
        )
        return

    # ---- persistence row + transcript buffer --------------------------
    started_at_unix = now_unix_secs()
    started_at_monotonic = time.monotonic()
    transcript_lines: list[str] = []
    audio_path = (
        str(audio_path_for(state.data_dir, tenant, session_id))
        if cfg.retain_audio
        else None
    )

    if state.session_store is not None:
        try:
            await state.session_store.record_start(
                VoiceSessionStart(
                    id=session_id,
                    tenant_id=tenant,
                    session_key=session_key,
                    agent_id=agent_id,
                    provider_alias=cfg.provider_alias,
                    started_at=started_at_unix,
                )
            )
        except Exception:  # noqa: BLE001 — persistence failures don't kill session
            logger.exception("voice: session_store.record_start failed; continuing")

    # ---- approval bridge ----------------------------------------------
    approval_bridge = VoiceApprovalBridge(
        queue=state.approval_queue, session_key=session_key
    )

    # ---- budget enforcer ----------------------------------------------
    budget = BudgetEnforcer.start(
        cfg=cost_cfg,
        spend=state.spend,
        tenant=tenant,
        day_epoch=day_epoch,
        started_at=started_at_monotonic,
    )

    # ---- send the `started` event -------------------------------------
    await _send_server(
        websocket,
        ServerControl(
            type=ServerControl.STARTED,
            session_id=session_id,
            provider=cfg.provider_alias,
        ),
    )

    # ---- run the pumps ------------------------------------------------
    cancel = asyncio.Event()
    end_reason: VoiceEndReason = VoiceEndReason.GRACEFUL

    inbound_task = asyncio.create_task(
        _pump_inbound(
            websocket=websocket,
            provider_session=provider_session,
            approval_bridge=approval_bridge,
            transcript_lines=transcript_lines,
            transcript_sink=state.transcript_sink,
            tenant=tenant,
            session_key=session_key,
            deferred=deferred,
            cancel=cancel,
        ),
        name=f"voice-in-{session_id}",
    )
    outbound_task = asyncio.create_task(
        _pump_outbound(
            websocket=websocket,
            provider_session=provider_session,
            approval_bridge=approval_bridge,
            transcript_lines=transcript_lines,
            transcript_sink=state.transcript_sink,
            tenant=tenant,
            session_key=session_key,
            cancel=cancel,
        ),
        name=f"voice-out-{session_id}",
    )
    ticker_task = asyncio.create_task(
        _pump_ticker(
            websocket=websocket,
            budget=budget,
            tick_interval=state.tick_interval_seconds,
            cancel=cancel,
        ),
        name=f"voice-tick-{session_id}",
    )

    pump_tasks: list[asyncio.Task[Any]] = [inbound_task, outbound_task, ticker_task]
    close_code = CLOSE_CODE_NORMAL
    close_reason = "graceful"

    try:
        done, pending = await asyncio.wait(
            pump_tasks, return_when=asyncio.FIRST_COMPLETED
        )
        # Whichever pump finished first determines the close shape.
        cancel.set()

        # Drain results — propagates cancellation but swallows expected
        # disconnect exceptions.
        for task in done:
            outcome = _consume_pump_outcome(task)
            if outcome is not None:
                end_reason = outcome.end_reason
                close_code = outcome.close_code
                close_reason = outcome.close_reason

        # Best-effort drain of the still-pending tasks.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception:  # noqa: BLE001
        logger.exception("voice: pump driver errored")
        end_reason = VoiceEndReason.PROVIDER_ERROR
        close_code = CLOSE_CODE_PROVIDER_ERROR
        close_reason = "internal error"
        cancel.set()
        for task in pump_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pump_tasks, return_exceptions=True)

    # ---- cleanup: provider close + spend flush + persistence + ws -----
    try:
        await provider_session.close()
    except Exception:  # noqa: BLE001
        logger.debug("voice: provider close errored", exc_info=True)

    finalised_seconds = budget.finalize(time.monotonic())
    ended_at_unix = now_unix_secs()

    if state.session_store is not None:
        try:
            await state.session_store.record_end(
                VoiceSessionEnd(
                    id=session_id,
                    ended_at=ended_at_unix,
                    duration_secs=finalised_seconds,
                    audio_path=audio_path,
                    transcript_text=(
                        "\n".join(transcript_lines) if transcript_lines else None
                    ),
                    end_reason=end_reason,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("voice: session_store.record_end failed")

    await _safe_close(websocket, close_code, close_reason)
    logger.debug(
        "voice: session closed session_id=%s end_reason=%s duration=%ds",
        session_id,
        end_reason.value,
        finalised_seconds,
    )


# ---------------------------------------------------------------------------
# Pump coroutines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PumpOutcome:
    end_reason: VoiceEndReason
    close_code: int
    close_reason: str


async def _pump_inbound(
    *,
    websocket: WebSocket,
    provider_session: VoiceProviderSession,
    approval_bridge: VoiceApprovalBridge,
    transcript_lines: list[str],
    transcript_sink: VoiceTranscriptSink | None,
    tenant: str,
    session_key: str,
    deferred: ClientControl | None,
    cancel: asyncio.Event,
) -> _PumpOutcome:
    """Pump client → provider.

    Each iteration awaits one WebSocket frame, dispatches on type, and
    feeds the provider. Audio frames flow as raw bytes; control frames
    are translated into :class:`ProviderCommand`s (or, for
    ``approve_tool``, signalled to the approval bridge via the same
    queue).
    """
    if deferred is not None:
        # The mod.rs `replay_first` analogue: dispatch the buffered
        # control frame before reading from the wire.
        await _handle_client_control(
            deferred,
            websocket=websocket,
            provider_session=provider_session,
        )

    while not cancel.is_set():
        try:
            msg = await websocket.receive()
        except WebSocketDisconnect:
            return _PumpOutcome(
                end_reason=VoiceEndReason.CLIENT_DISCONNECT,
                close_code=CLOSE_CODE_NORMAL,
                close_reason="client disconnected",
            )
        except RuntimeError:
            # starlette raises when receive() is called on a closed
            # socket — treat as a clean client disconnect.
            return _PumpOutcome(
                end_reason=VoiceEndReason.CLIENT_DISCONNECT,
                close_code=CLOSE_CODE_NORMAL,
                close_reason="client disconnected",
            )

        msg_type = msg.get("type")
        if msg_type == "websocket.disconnect":
            return _PumpOutcome(
                end_reason=VoiceEndReason.CLIENT_DISCONNECT,
                close_code=CLOSE_CODE_NORMAL,
                close_reason="client disconnected",
            )

        if msg_type != "websocket.receive":
            continue

        if (data := msg.get("bytes")) is not None:
            try:
                frame = parse_audio_frame(data)
            except AudioFrameError as exc:
                logger.debug("voice: bad audio frame: %s", exc)
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.ERROR,
                        code="invalid_audio",
                        message=str(exc),
                    ),
                )
                continue
            try:
                await provider_session.push_audio(frame.pcm_le_bytes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("voice: provider push_audio failed: %s", exc)
                return _PumpOutcome(
                    end_reason=VoiceEndReason.PROVIDER_ERROR,
                    close_code=CLOSE_CODE_PROVIDER_ERROR,
                    close_reason="provider audio push failed",
                )
            continue

        if (text := msg.get("text")) is not None:
            try:
                control = parse_client_control(text)
            except ControlParseError as exc:
                logger.debug("voice: bad client control frame: %s", exc)
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.ERROR,
                        code="invalid_control",
                        message=exc.message,
                    ),
                )
                continue
            should_end = await _handle_client_control(
                control,
                websocket=websocket,
                provider_session=provider_session,
            )
            if should_end:
                return _PumpOutcome(
                    end_reason=VoiceEndReason.GRACEFUL,
                    close_code=CLOSE_CODE_NORMAL,
                    close_reason="client requested end",
                )
            continue

    return _PumpOutcome(
        end_reason=VoiceEndReason.GRACEFUL,
        close_code=CLOSE_CODE_NORMAL,
        close_reason="cancelled",
    )


async def _pump_outbound(
    *,
    websocket: WebSocket,
    provider_session: VoiceProviderSession,
    approval_bridge: VoiceApprovalBridge,
    transcript_lines: list[str],
    transcript_sink: VoiceTranscriptSink | None,
    tenant: str,
    session_key: str,
    cancel: asyncio.Event,
) -> _PumpOutcome:
    """Pump provider → client.

    Iterates :meth:`VoiceProviderSession.events` and translates each
    :class:`VoiceEvent` into the corresponding WebSocket frame(s).
    Tool-call events go through the :class:`VoiceApprovalBridge`; the
    resulting :class:`ApprovalOutcome` produces 1..n server control
    frames + 1..n provider commands.
    """
    try:
        async for event in provider_session.events():
            if cancel.is_set():
                break

            if event.kind == VoiceEvent.READY:
                # The route handler already sent `started`; READY is the
                # provider's own ack and need not surface to the wire.
                continue

            if event.kind == VoiceEvent.AUDIO_OUT:
                if event.pcm_le_bytes is not None:
                    try:
                        await websocket.send_bytes(event.pcm_le_bytes)
                    except (WebSocketDisconnect, RuntimeError):
                        return _PumpOutcome(
                            end_reason=VoiceEndReason.CLIENT_DISCONNECT,
                            close_code=CLOSE_CODE_NORMAL,
                            close_reason="client disconnected",
                        )
                continue

            if event.kind == VoiceEvent.TRANSCRIPT_PARTIAL:
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.TRANSCRIPT_PARTIAL,
                        role=event.role,
                        text=event.text,
                    ),
                )
                continue

            if event.kind == VoiceEvent.TRANSCRIPT_FINAL:
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.TRANSCRIPT_FINAL,
                        role=event.role,
                        text=event.text,
                    ),
                )
                # Buffer for `voice_sessions.transcript_text` + flush to
                # the chat-session bridge so the agent loop sees the
                # turn.
                role = event.role or "user"
                text = event.text or ""
                transcript_lines.append(f"{role}: {text}")
                if transcript_sink is not None and text:
                    try:
                        await transcript_sink.append_turn(
                            tenant, session_key, role, text
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "voice: transcript sink append_turn failed"
                        )
                continue

            if event.kind == VoiceEvent.AGENT_TEXT:
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.AGENT_TEXT, text=event.text
                    ),
                )
                continue

            if event.kind == VoiceEvent.TOOL_CALL:
                outcome = await approval_bridge.handle_tool_call(
                    approval_id=event.call_id or "",
                    tool=event.tool or "",
                    args_json=event.args,
                    cancel=cancel,
                )
                await _apply_approval_outcome(
                    outcome, websocket=websocket, provider_session=provider_session
                )
                if outcome.decision == ApprovalDecisionKind.DENIED:
                    logger.debug(
                        "voice: tool call denied; continuing session"
                    )
                continue

            if event.kind == VoiceEvent.ERROR:
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.ERROR,
                        code=event.code or "provider_error",
                        message=event.message or "provider error",
                    ),
                )
                return _PumpOutcome(
                    end_reason=VoiceEndReason.PROVIDER_ERROR,
                    close_code=CLOSE_CODE_PROVIDER_ERROR,
                    close_reason=event.message or "provider error",
                )

            if event.kind == VoiceEvent.END:
                reason = event.end_reason or ProviderEndReason.GRACEFUL
                if reason == ProviderEndReason.PROVIDER_ERROR:
                    return _PumpOutcome(
                        end_reason=VoiceEndReason.PROVIDER_ERROR,
                        close_code=CLOSE_CODE_PROVIDER_ERROR,
                        close_reason="provider ended with error",
                    )
                if reason == ProviderEndReason.START_FAILED:
                    return _PumpOutcome(
                        end_reason=VoiceEndReason.START_FAILED,
                        close_code=CLOSE_CODE_PROVIDER_ERROR,
                        close_reason="provider failed to start",
                    )
                return _PumpOutcome(
                    end_reason=VoiceEndReason.GRACEFUL,
                    close_code=CLOSE_CODE_NORMAL,
                    close_reason="provider ended",
                )

        # Provider session iterator exhausted without an explicit END —
        # treat as graceful close.
        return _PumpOutcome(
            end_reason=VoiceEndReason.GRACEFUL,
            close_code=CLOSE_CODE_NORMAL,
            close_reason="provider stream ended",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("voice: outbound pump errored")
        return _PumpOutcome(
            end_reason=VoiceEndReason.PROVIDER_ERROR,
            close_code=CLOSE_CODE_PROVIDER_ERROR,
            close_reason=f"provider stream error: {exc}",
        )


async def _pump_ticker(
    *,
    websocket: WebSocket,
    budget: BudgetEnforcer,
    tick_interval: float,
    cancel: asyncio.Event,
) -> _PumpOutcome:
    """1-Hz budget enforcer ticker. Emits ``budget_warning`` frames as
    the meter approaches the cap; on terminate, returns the final close
    code + ``end_reason`` mapping."""
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=tick_interval)
            # The wait() above returned without TimeoutError → cancel
            # was set. Loop condition handles exit.
            continue
        except asyncio.TimeoutError:
            pass

        action = budget.tick(time.monotonic())
        if action.kind == BudgetTickAction.CONTINUE:
            continue
        if action.kind == BudgetTickAction.EMIT_WARNING:
            try:
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.BUDGET_WARNING,
                        minutes_remaining=action.minutes_remaining,
                    ),
                )
            except (WebSocketDisconnect, RuntimeError):
                return _PumpOutcome(
                    end_reason=VoiceEndReason.CLIENT_DISCONNECT,
                    close_code=CLOSE_CODE_NORMAL,
                    close_reason="client disconnected",
                )
            continue
        if action.kind == BudgetTickAction.TERMINATE:
            assert action.reason is not None
            assert action.close_code is not None
            reason = action.reason
            try:
                await _send_server(
                    websocket,
                    ServerControl(
                        type=ServerControl.ERROR,
                        code=terminate_reason_to_code(reason),
                        message=terminate_reason_to_message(reason),
                    ),
                )
            except (WebSocketDisconnect, RuntimeError):
                pass
            end_reason_value = terminate_reason_to_end_reason(reason)
            return _PumpOutcome(
                end_reason=VoiceEndReason(end_reason_value),
                close_code=action.close_code,
                close_reason=terminate_reason_to_message(reason),
            )

    return _PumpOutcome(
        end_reason=VoiceEndReason.GRACEFUL,
        close_code=CLOSE_CODE_NORMAL,
        close_reason="cancelled",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _StartTimeout(Exception):
    """Raised by :func:`_read_start_frame` when no frame arrives in
    :data:`DEFAULT_START_TIMEOUT_SECONDS`."""


class _StartMalformed(Exception):
    """Raised by :func:`_read_start_frame` when the first frame doesn't
    parse as a control envelope."""


class _StartDisconnect(Exception):
    """Raised by :func:`_read_start_frame` when the client hangs up
    before sending any frame."""


async def _read_start_frame(
    websocket: WebSocket, timeout_seconds: float
) -> tuple[ClientControl, ClientControl | None]:
    """Read the mandatory first control frame.

    Returns ``(start_frame, deferred)`` where ``deferred`` is a non-
    ``start`` control frame that was received first and must be replayed
    to the inbound pump. The Rust analogue allows a non-start first
    frame as a tolerated protocol violation; we forward it instead of
    closing the socket.
    """
    try:
        msg = await asyncio.wait_for(websocket.receive(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise _StartTimeout("no start frame within timeout") from exc
    except WebSocketDisconnect as exc:
        raise _StartDisconnect("disconnected before start") from exc

    msg_type = msg.get("type")
    if msg_type == "websocket.disconnect":
        raise _StartDisconnect("disconnected before start")
    if msg_type != "websocket.receive":
        raise _StartMalformed(f"unexpected first message type: {msg_type}")

    text = msg.get("text")
    if text is None:
        # The Rust handler treats binary-before-start as a protocol
        # error too. We're stricter than the Rust path here for safety;
        # a future iter can fall back to "buffer + forward".
        raise _StartMalformed("first frame must be a `start` control text frame")

    try:
        control = parse_client_control(text)
    except ControlParseError as exc:
        raise _StartMalformed(str(exc)) from exc

    if control.type == ClientControl.START:
        return control, None
    # Non-start control frame: synthesise a default `start` so the
    # session can proceed, and forward the original frame to the inbound
    # pump as `deferred`.
    return (
        ClientControl(
            type=ClientControl.START,
            session_key=None,
            agent_id=None,
            sample_rate_hz=None,
            format=None,
        ),
        control,
    )


async def _handle_client_control(
    control: ClientControl,
    *,
    websocket: WebSocket,
    provider_session: VoiceProviderSession,
) -> bool:
    """Dispatch one parsed control frame to the provider.

    Returns ``True`` when the client requested an ``end`` (so the
    inbound pump should exit gracefully)."""
    ty = control.type
    if ty == ClientControl.START:
        # Mid-session `start` is a no-op per the Rust bridge contract.
        return False
    if ty == ClientControl.INTERRUPT:
        try:
            await provider_session.push_command(ProviderCommand.interrupt())
        except Exception:  # noqa: BLE001
            logger.exception("voice: provider interrupt push failed")
        return False
    if ty == ClientControl.APPROVE_TOOL:
        # The approval bridge owns the upstream `approve_tool` push for
        # gate-driven approvals. A client-initiated `approve_tool` is
        # only meaningful when the gate is wired and the client is the
        # approver — forward straight through so the provider sees the
        # decision regardless.
        if control.approval_id and control.approve is not None:
            try:
                await provider_session.push_command(
                    ProviderCommand.approve_tool(
                        control.approval_id, approve=control.approve
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("voice: provider approve_tool push failed")
        return False
    if ty == ClientControl.END:
        try:
            await provider_session.push_command(ProviderCommand.close())
        except Exception:  # noqa: BLE001
            logger.debug("voice: provider close push errored", exc_info=True)
        return True
    return False


async def _apply_approval_outcome(
    outcome: ApprovalOutcome,
    *,
    websocket: WebSocket,
    provider_session: VoiceProviderSession,
) -> None:
    """Apply an :class:`ApprovalOutcome` to the WebSocket + provider."""
    for frame in outcome.server_frames:
        try:
            await _send_server(websocket, frame)
        except (WebSocketDisconnect, RuntimeError):
            logger.debug("voice: client gone during approval flush")
            return
    for command in outcome.provider_commands:
        try:
            await provider_session.push_command(command)
        except Exception:  # noqa: BLE001
            logger.exception(
                "voice: provider push_command failed during approval flush"
            )


async def _send_server(websocket: WebSocket, control: ServerControl) -> None:
    """Encode a :class:`ServerControl` and send it as a WebSocket text
    frame. Centralised so a future migration to a different wire shape
    only changes one call-site."""
    await websocket.send_text(encode_server_control(control))


async def _safe_close(
    websocket: WebSocket, code: int, reason: str
) -> None:
    """Close the WebSocket iff it isn't already closed. Swallows the
    redundant-close errors starlette raises when both halves try to
    close simultaneously."""
    try:
        state = websocket.client_state
    except AttributeError:
        state = WebSocketState.CONNECTED  # type: ignore[assignment]
    if state == WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        # Already closed by the peer or a concurrent close — fine.
        logger.debug("voice: safe_close swallowed RuntimeError", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("voice: safe_close errored", exc_info=True)


def _consume_pump_outcome(task: asyncio.Task[Any]) -> _PumpOutcome | None:
    """Pull a :class:`_PumpOutcome` from a finished pump task. Returns
    ``None`` on cancellation / unexpected exception so the caller can
    fall back to its own defaults."""
    try:
        result = task.result()
    except asyncio.CancelledError:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("voice: pump task raised")
        return _PumpOutcome(
            end_reason=VoiceEndReason.PROVIDER_ERROR,
            close_code=CLOSE_CODE_PROVIDER_ERROR,
            close_reason="pump errored",
        )
    if isinstance(result, _PumpOutcome):
        return result
    return None


async def _close_budget_exhausted(
    websocket: WebSocket,
    subprotocol: str,
    reason: BudgetDenyReason | None,
    reset_at: int,
) -> None:
    """Close path for a pre-upgrade budget-gate refusal. We have to
    accept first so the close code is sent on the WebSocket channel
    (clients can't read an HTTP body once the upgrade is in flight)."""
    await websocket.accept(subprotocol=subprotocol)
    code = "budget_exhausted"
    if reason is None or reason.kind == BudgetDenyReason.BUDGET_IS_ZERO:
        message = (
            "voice.budget_minutes_per_tenant_per_day is set to 0; "
            "voice is administratively disabled for this tenant"
        )
    else:
        message = (
            f"tenant has used {reason.used_seconds}s of the "
            f"{reason.cap_seconds}s daily voice budget"
        )
    payload = json.dumps(
        {
            "type": "error",
            "code": code,
            "message": message,
            "reset_at": reset_at,
        }
    )
    try:
        await websocket.send_text(payload)
    except Exception:  # noqa: BLE001
        logger.debug(
            "voice: budget_exhausted text send failed", exc_info=True
        )
    await _safe_close(websocket, CLOSE_CODE_BUDGET, message)


async def _record_session_start_end(
    state: VoiceState,
    session_id: str,
    tenant: str,
    session_key: str,
    agent_id: str | None,
    provider_alias: str,
    *,
    started_at_unix: int,
    ended_at_unix: int,
    duration_secs: int,
    audio_path: str | None,
    transcript_text: str | None,
    end_reason: VoiceEndReason,
) -> None:
    """Persist a start + immediate end row in one shot. Used by the
    provider-open-failed path so the audit table shows the attempt with
    the right ``end_reason``."""
    if state.session_store is None:
        return
    try:
        await state.session_store.record_start(
            VoiceSessionStart(
                id=session_id,
                tenant_id=tenant,
                session_key=session_key,
                agent_id=agent_id,
                provider_alias=provider_alias,
                started_at=started_at_unix,
            )
        )
        await state.session_store.record_end(
            VoiceSessionEnd(
                id=session_id,
                ended_at=ended_at_unix,
                duration_secs=duration_secs,
                audio_path=audio_path,
                transcript_text=transcript_text,
                end_reason=end_reason,
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "voice: persistence of start_failed audit row errored"
        )
