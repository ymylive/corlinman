"""User-correction detector — heuristic-only (no LLM).

Spots corrective phrases in user chat messages so we can fire an
``EVENT_USER_CORRECTION`` signal that the applier later routes to a
background-review fork to patch the implicated skill's body.

This is deliberately a heuristic. Future iterations may swap in a small
intent classifier. The detector is fast (sub-millisecond), deterministic,
and explainable — operators can grep the ``matched_pattern`` field in the
signal payload to see why it fired.

Wired into the gateway via :func:`register_user_correction_listener`,
which subscribes a fire-and-forget handler to the shared
:class:`corlinman_hooks.HookBus`. The handler:

1. Listens for :class:`corlinman_hooks.HookEvent.MessageReceived` (or any
   variant that exposes a user-authored ``content`` field).
2. Calls :func:`detect_correction` on the text.
3. On a match, inserts an :class:`corlinman_evolution_store.EvolutionSignal`
   with ``event_kind = EVENT_USER_CORRECTION``.
4. Hands the freshly-built signal to a downstream applier callback
   (typically :class:`UserCorrectionApplier`) which decides whether to
   spawn a background-review fork.

Critically, every disk/network operation is wrapped in
``asyncio.create_task`` so the chat hot path is never blocked. Failures
log + drop; the chat experience is never degraded by curator failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Pattern

from corlinman_evolution_store import (
    EVENT_USER_CORRECTION,
    EvolutionSignal,
    SignalSeverity,
    SignalsRepo,
)
from corlinman_hooks import HookBus, HookEvent, HookPriority
from corlinman_hooks.error import Closed, Lagged

__all__ = [
    "CorrectionMatch",
    "detect_correction",
    "register_user_correction_listener",
]


log = logging.getLogger(__name__)


# ─── Heuristic patterns ──────────────────────────────────────────────
#
# Ordered by specificity (most specific first). Each tuple is
# ``(regex, kind, weight)``:
#
# * ``kind`` subclassifies the match so later detectors / UI can render
#   the *reason* the signal fired without re-parsing.
# * ``weight`` is the detector's confidence (0.0-1.0), surfaced in the
#   signal payload so the applier's threshold gate can filter weak hits.
#
# The list is short and explicit — keep it grep-able. Each new pattern
# should come with a unit test case in ``test_user_correction_detector``.

_PATTERNS: list[tuple[Pattern[str], str, float]] = [
    # Negation of prior assistant behavior ("no, I said …", "that's not
    # what I …"). Most specific signal of correction we can spot
    # heuristically — the user is telling us the previous reply was
    # wrong in a *targeted* way.
    (
        re.compile(
            r"\b(no,?\s+I\s+(said|asked|wanted)|that'?s\s+not\s+what\s+I)\b",
            re.IGNORECASE,
        ),
        "rejection",
        0.90,
    ),
    (
        re.compile(r"\b(I\s+(already\s+)?said|I\s+told\s+you)\b", re.IGNORECASE),
        "rejection",
        0.85,
    ),
    # Imperative correction ("stop", "don't", "cut it out").
    (
        re.compile(r"\b(stop|don'?t|please\s+stop|cut\s+it\s+out)\b", re.IGNORECASE),
        "imperative",
        0.85,
    ),
    # Pattern-of-behavior critique ("you always", "you keep", …).
    (
        re.compile(
            r"\byou\s+(always|keep|never|insist\s+on)\b",
            re.IGNORECASE,
        ),
        "pattern_critique",
        0.80,
    ),
    # Strong negative reaction ("I hate when …", "annoying").
    (
        re.compile(
            r"\b(I\s+hate\s+(it\s+)?when|please\s+don'?t|annoying)\b",
            re.IGNORECASE,
        ),
        "negative_reaction",
        0.75,
    ),
    # Reformulation marker ("actually,", "wait,", "no wait"). Weakest
    # signal — users say "actually" without disapproval all the time, so
    # the applier's ``min_weight`` gate typically suppresses this one.
    (
        re.compile(
            r"\b(actually,?|wait,?\s+|no\s+wait)\b",
            re.IGNORECASE,
        ),
        "reformulation",
        0.55,
    ),
]


@dataclass(frozen=True)
class CorrectionMatch:
    """One detector hit, returned by :func:`detect_correction`.

    Attributes
    ----------
    matched_pattern:
        The regex source that fired. Lets operators grep the audit log
        and trace a signal back to a specific heuristic without
        re-running the detector.
    kind:
        One of ``imperative``, ``rejection``, ``pattern_critique``,
        ``reformulation``, ``negative_reaction``. Surfaced verbatim in
        the signal payload so admin UI can colour-code matches.
    weight:
        Detector confidence in ``[0.0, 1.0]``. Filtered against
        ``UserCorrectionApplier.min_weight`` before a background review
        is spawned.
    span:
        ``(start, end)`` char offsets of the match inside the original
        text. Useful for unit tests and (eventually) for the admin UI
        to highlight the offending phrase.
    snippet:
        The matched substring, lower-cased for stability so the
        downstream payload is hash-friendly.
    """

    matched_pattern: str
    kind: str
    weight: float
    span: tuple[int, int]
    snippet: str


def detect_correction(text: str | None) -> CorrectionMatch | None:
    """Return the highest-weight correction match in ``text``, or
    ``None`` if no pattern fires.

    Multiple patterns may match a single sentence; the function returns
    the most specific one (highest weight). Ties are broken by earliest
    position in the text so the result is deterministic.

    Parameters
    ----------
    text:
        The user's chat message. ``None`` and whitespace-only strings
        return ``None`` without scanning.
    """
    if not text or not text.strip():
        return None

    best: CorrectionMatch | None = None
    best_key: tuple[float, int] | None = None
    for pat, kind, weight in _PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        candidate = CorrectionMatch(
            matched_pattern=pat.pattern,
            kind=kind,
            weight=weight,
            span=(m.start(), m.end()),
            snippet=text[m.start() : m.end()].lower(),
        )
        # Sort key: ``(-weight, start_pos)`` so higher weight wins, and
        # earlier position breaks ties.
        key = (-weight, m.start())
        if best_key is None or key < best_key:
            best = candidate
            best_key = key
    return best


# ─── HookBus listener wiring ─────────────────────────────────────────


# Callable signature for the downstream applier hook the listener
# invokes once a signal is inserted. Defined as a Protocol-style alias
# so the gateway can pass any object with an ``apply(signal)`` coroutine
# (typically :class:`UserCorrectionApplier`).
AppliedCallback = Callable[[EvolutionSignal], Awaitable[Any]]


def _now_ms() -> int:
    """Unix milliseconds — matches the observer's ``now_ms`` helper."""
    return int(time.time() * 1000)


def _is_user_message_event(event: Any) -> bool:
    """``True`` when ``event`` is a user-authored chat message variant.

    We accept the canonical :class:`HookEvent.MessageReceived` plus its
    transcribed / preprocessed cousins so the detector still fires when
    voice transcription or upstream normalisation has already run.
    """
    msg_received = getattr(HookEvent, "MessageReceived", None)
    msg_transcribed = getattr(HookEvent, "MessageTranscribed", None)
    msg_preprocessed = getattr(HookEvent, "MessagePreprocessed", None)
    candidates = tuple(c for c in (msg_received, msg_transcribed, msg_preprocessed) if c is not None)
    return bool(candidates) and isinstance(event, candidates)


def _extract_text(event: Any) -> str | None:
    """Pull the user-authored text out of a message-shaped event.

    Different message variants name the field differently:

    * :class:`HookEvent.MessageReceived` → ``content``
    * :class:`HookEvent.MessageTranscribed` → ``transcript``
    * :class:`HookEvent.MessagePreprocessed` → ``transcript``

    Returns ``None`` if no recognisable text payload is present.
    """
    for attr in ("content", "transcript"):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_session_id(event: Any) -> str | None:
    """Best-effort session id extraction.

    The hook variants expose the session under ``session_key_`` (the
    underscore-suffix dance documented in ``corlinman_hooks.event``).
    The :func:`session_key` accessor returns the public string form.
    """
    accessor = getattr(event, "session_key", None)
    if callable(accessor):
        try:
            value = accessor()
        except Exception:  # noqa: BLE001
            value = None
        if isinstance(value, str) and value:
            return value
    raw = getattr(event, "session_key_", None)
    if isinstance(raw, str) and raw:
        return raw
    return None


def _extract_tenant_id(event: Any) -> str:
    """Return ``event.tenant_id`` if present, else ``"default"``.

    The message variants don't currently carry a ``tenant_id`` field —
    we keep the lookup defensive so future-added fields work without a
    detector update.
    """
    tenant = getattr(event, "tenant_id", None)
    if isinstance(tenant, str) and tenant:
        return tenant
    return "default"


def _build_signal(
    *,
    text: str,
    match: CorrectionMatch,
    target: str | None,
    session_id: str | None,
    tenant_id: str,
) -> EvolutionSignal:
    """Assemble the :class:`EvolutionSignal` row from a heuristic hit.

    ``payload_json`` is intentionally kept compact (≤500 char text
    snippet) so the SQLite row stays small even on very long pasted
    messages — the background-review fork re-reads the full text from
    its own conversation context anyway.
    """
    payload: dict[str, Any] = {
        "text": text[:500],
        "matched_pattern": match.matched_pattern,
        "kind": match.kind,
        "weight": match.weight,
        "snippet": match.snippet,
    }
    return EvolutionSignal(
        event_kind=EVENT_USER_CORRECTION,
        target=target,
        severity=SignalSeverity.INFO,
        payload_json=payload,
        observed_at=_now_ms(),
        session_id=session_id,
        tenant_id=tenant_id,
    )


async def _handle_event(
    event: Any,
    *,
    signals_repo: SignalsRepo,
    on_signal: AppliedCallback | None,
    target_resolver: Callable[[Any], str | None] | None,
) -> None:
    """Detect → insert → dispatch for one chat event.

    Catches every exception: this runs in a fire-and-forget task and the
    chat hot path must never inherit a failure from the curator surface.
    """
    try:
        if not _is_user_message_event(event):
            return
        text = _extract_text(event)
        if not text:
            return
        match = detect_correction(text)
        if match is None:
            return
        target: str | None = None
        if target_resolver is not None:
            try:
                target = target_resolver(event)
            except Exception as err:  # noqa: BLE001 — never let resolver crash us
                log.debug("user_correction.target_resolver_failed err=%s", err)
                target = None
        signal = _build_signal(
            text=text,
            match=match,
            target=target,
            session_id=_extract_session_id(event),
            tenant_id=_extract_tenant_id(event),
        )
        try:
            sid = await signals_repo.insert(signal)
        except Exception as err:  # noqa: BLE001 — log + drop
            log.warning("user_correction.insert_failed err=%s", err)
            return
        signal.id = sid
        if on_signal is None:
            return
        # Dispatch to the downstream applier as fire-and-forget so the
        # insert path returns quickly even if the LLM call is slow.
        try:
            asyncio.create_task(_safe_invoke(on_signal, signal))
        except RuntimeError:
            # No running loop — happens in odd shutdown ordering. Fall
            # back to a direct ``await``: we've already inserted the
            # signal, the dispatch is best-effort either way.
            await _safe_invoke(on_signal, signal)
    except Exception as err:  # noqa: BLE001 — defensive umbrella
        log.warning("user_correction.handle_event_failed err=%s", err)


async def _safe_invoke(cb: AppliedCallback, signal: EvolutionSignal) -> None:
    """Invoke ``cb(signal)`` and swallow exceptions with a warning log."""
    try:
        await cb(signal)
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001
        log.warning(
            "user_correction.dispatch_failed event_kind=%s err=%s",
            signal.event_kind,
            err,
        )


def register_user_correction_listener(
    bus: HookBus,
    signals_repo: SignalsRepo,
    *,
    on_signal: AppliedCallback | None = None,
    target_resolver: Callable[[Any], str | None] | None = None,
    priority: HookPriority = HookPriority.LOW,
) -> asyncio.Task[None]:
    """Subscribe the user-correction detector to ``bus``.

    Returns the long-lived asyncio task that owns the subscription so
    the gateway can ``await task`` on shutdown (or ``task.cancel()``).
    The subscription drains the bus on the LOW priority tier (same tier
    the existing observer uses) so we share fan-out cost.

    Parameters
    ----------
    bus:
        The shared :class:`HookBus` the gateway constructs at boot.
    signals_repo:
        Async repo writer for ``evolution_signals``. The same instance
        the :class:`EvolutionObserver` consumes.
    on_signal:
        Optional async callback invoked **after** the signal has been
        persisted. Typically wired to
        :meth:`UserCorrectionApplier.apply`.
    target_resolver:
        Optional pure function ``event -> skill_name | profile_slug``.
        Lets the gateway thread its session→skill context into the
        signal's ``target`` column without this module needing to
        understand profile internals.
    priority:
        Subscription priority on the bus. Defaults to LOW so we don't
        contend with latency-critical handlers.

    The handler is intentionally tolerant: any failure inside detection,
    insertion or dispatch logs at WARN and drops the event. The chat
    path must never block on this listener.
    """

    async def _loop() -> None:
        sub = bus.subscribe(priority)
        while True:
            try:
                event = await sub.recv()
            except Lagged as err:
                log.warning(
                    "user_correction.subscriber_lagged dropped=%s",
                    getattr(err, "count", 1),
                )
                continue
            except Closed:
                log.debug("user_correction.subscriber_closed")
                return
            except asyncio.CancelledError:
                return
            # Fire-and-forget so a slow insert never backs up the
            # subscription queue.
            try:
                asyncio.create_task(
                    _handle_event(
                        event,
                        signals_repo=signals_repo,
                        on_signal=on_signal,
                        target_resolver=target_resolver,
                    )
                )
            except RuntimeError:
                # No loop (shouldn't happen given we're inside one) —
                # degrade to inline.
                await _handle_event(
                    event,
                    signals_repo=signals_repo,
                    on_signal=on_signal,
                    target_resolver=target_resolver,
                )

    return asyncio.create_task(_loop(), name="user-correction-listener")


# Re-exported only so tests can monkeypatch the clock without reaching
# into a private symbol; not part of the public surface advertised in
# the package __init__.
_test_now_ms = _now_ms
_test_handle_event = _handle_event
_test_extract_text = _extract_text
_test_extract_session_id = _extract_session_id
_test_build_signal = _build_signal
_test_is_user_message_event = _is_user_message_event

# Stable JSON encoder for the payload — exported so a future federation
# rebroadcaster can re-encode rows without re-importing json.
_payload_dumps = json.dumps
