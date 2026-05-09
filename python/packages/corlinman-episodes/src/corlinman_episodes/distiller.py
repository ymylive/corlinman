"""LLM distillation pass — :class:`SourceBundle` → ``summary_text``.

Per ``docs/design/phase4-w4-d1-design.md`` §"Distillation job" step 3:

1. Pick a per-kind prompt segment (``episodes/prompts/<kind>.md``).
2. Render the bundle into a prompt — sessions / signals / history
   payloads, kind-aware framing.
3. PII-redact the prompt input (Phase 3.1 Tier 3 / S-1 redactor).
4. Call the LLM via ``corlinman-providers::registry::resolve(alias)``.
5. PII-redact the LLM output as defence-in-depth.
6. Return the redacted summary string.

The provider is wired through a narrow :class:`SummaryProvider`
``Protocol`` so tests pass a deterministic stub without touching the
real ``corlinman-providers`` registry. Iter 7 will glue the real
adapter; iter 3 just lands the call shape + the per-kind prompts +
the redactor.

Mock pattern matches the dependency-inversion style used by
``corlinman-evolution-engine``: the distiller takes a callable, the
runner injects it. No ``import corlinman_providers`` here.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from corlinman_episodes.sources import SourceBundle
from corlinman_episodes.store import EpisodeKind

# ---------------------------------------------------------------------------
# Prompt segments
# ---------------------------------------------------------------------------

#: Per-kind system-prompt segments. Loaded from in-source dict for
#: iter 3 — iter 5 (per the design doc's implementation order) would
#: lift these into ``episodes/prompts/<kind>.md`` files for operator
#: editing. Keeping them inline now avoids a cross-cutting filesystem
#: dependency that makes the distiller harder to test.
#:
#: Each segment is the *prefix* the prompt builder concatenates with
#: the rendered bundle. The shape is small and tonally distinct so
#: the resulting summary text reads like the right kind of episode.
PROMPT_SEGMENTS: dict[EpisodeKind, str] = {
    EpisodeKind.CONVERSATION: (
        "You are summarising a conversation between an agent and a user. "
        "Focus on what the user asked for and what the agent did about it. "
        "1-3 short paragraphs. Avoid bulleted lists."
    ),
    EpisodeKind.EVOLUTION: (
        "You are summarising an evolution-apply episode. The agent "
        "approved or auto-applied a self-modification (skill_update, "
        "prompt_template, tool_policy, memory_op). Lead with the "
        "outcome (what changed and why); cite the trigger signals."
    ),
    EpisodeKind.INCIDENT: (
        "You are summarising an incident. An auto-rollback fired or "
        "a critical-severity signal cluster landed. Lead with the "
        "failure mode, then the operator/agent response, then the "
        "current state."
    ),
    EpisodeKind.ONBOARDING: (
        "You are summarising an onboarding episode — the user's "
        "first sessions. Capture stated goals, channels they verified, "
        "and any preferences worth remembering long-term."
    ),
    EpisodeKind.OPERATOR: (
        "You are summarising an operator action episode. An admin "
        "approved or denied a tool invocation, or merged identities, "
        "or rolled back an evolution. Cite the proposal id and the "
        "operator's reasoning if recorded."
    ),
}


# ---------------------------------------------------------------------------
# PII redactor
# ---------------------------------------------------------------------------

# Pattern application is ordered: more-specific patterns first so a
# greedy phone-number regex doesn't swallow IPs and credit-card
# digit runs. The Phase 3.1 redactor lives in ``corlinman-server``
# proper; we re-implement a thin version here so the distiller stays
# self-contained for iter 3. Iter 7 swaps in the canonical redactor.
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Email — least likely to overlap with other shapes.
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    # IPv4 dotted quad — must run before phone (which would match
    # the digit-and-dot run).
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    # Credit-card-shaped 13-19-digit run — runs before phone for the
    # same reason.
    (re.compile(r"\b\d{13,19}\b"), "[cc]"),
    # Phone — last; conservative (mandatory non-digit separator) so
    # plain integer ms timestamps don't false-match. Requires a +
    # prefix or whitespace/parenthesis between digit groups.
    (re.compile(r"\+\d[\d\s\-().]{6,}\d"), "[phone]"),
    (
        re.compile(r"\b\d{2,4}[\s\-.()]\d{2,4}[\s\-.()]\d{2,5}\b"),
        "[phone]",
    ),
)


def redact_pii(text: str) -> str:
    """Apply the iter-3 PII patterns to ``text`` and return the
    redacted version.

    Idempotent — running redaction twice on already-redacted text is
    a no-op (placeholder tokens don't match the patterns again). The
    distiller runs this both pre- and post-LLM as defence in depth
    against a model that hallucinates a phone number.
    """
    if not text:
        return text
    out = text
    for pat, repl in _PII_PATTERNS:
        out = pat.sub(repl, out)
    return out


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class SummaryProvider(Protocol):
    """Async callable contract for the LLM distillation step.

    The runner injects an implementation backed by
    ``corlinman-providers::registry::resolve(alias)`` (iter 7); tests
    inject a deterministic stub. Returning a plain string keeps the
    contract narrow — no streaming, no tool-use, no system/user
    distinction (the prompt is pre-assembled here).
    """

    async def __call__(self, *, prompt: str, kind: EpisodeKind) -> str: ...


# Convenience type alias — matches what most call sites end up using.
SummaryFn = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------------------
# Result + bundle rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistilledSummary:
    """What :func:`distill` returns to the runner.

    ``summary_text`` is the redacted final string for the row;
    ``prompt_redacted`` and ``raw_output`` are surfaced for debugging
    and for the test that pins "the LLM never sees a raw phone".
    """

    summary_text: str
    prompt_redacted: str
    raw_output: str
    distilled_by: str


def _render_bundle(bundle: SourceBundle, *, max_messages: int) -> str:
    """Compact human-readable bundle dump for the LLM prompt.

    Capped at ``max_messages`` per the ``[episodes]
    max_messages_per_call`` config — the doc's "shard a 30-day window
    in chunks" test uses this to assert memory-bounded behaviour.
    Order: messages → signals → history → hooks → identity merges.
    """
    lines: list[str] = []
    lines.append(f"## bundle: session_key={bundle.session_key!r}")
    lines.append(
        f"window: started_at={bundle.started_at} ended_at={bundle.ended_at}"
    )
    if bundle.messages:
        lines.append("")
        lines.append("### messages")
        for m in bundle.messages[:max_messages]:
            lines.append(f"- [{m.ts_ms}] {m.role}: {m.content}")
        if len(bundle.messages) > max_messages:
            lines.append(
                f"- ...({len(bundle.messages) - max_messages} more truncated)"
            )
    if bundle.signals:
        lines.append("")
        lines.append("### signals")
        for s in bundle.signals:
            lines.append(
                f"- [{s.observed_at_ms}] {s.event_kind} target={s.target!r} "
                f"severity={s.severity} payload={s.payload_json}"
            )
    if bundle.history:
        lines.append("")
        lines.append("### evolution_history")
        for h in bundle.history:
            rb = (
                f" rolled_back_at={h.rolled_back_at_ms}"
                f" reason={h.rollback_reason!r}"
                if h.rolled_back_at_ms is not None
                else ""
            )
            lines.append(
                f"- [{h.applied_at_ms}] {h.kind} {h.target} "
                f"proposal={h.proposal_id} signals={list(h.signal_ids)}{rb}"
            )
    if bundle.hooks:
        lines.append("")
        lines.append("### hook_events")
        for e in bundle.hooks:
            lines.append(
                f"- [{e.occurred_at_ms}] {e.kind} payload={e.payload_json}"
            )
    if bundle.identity_merges:
        lines.append("")
        lines.append("### identity_merges")
        for im in bundle.identity_merges:
            lines.append(
                f"- [{im.occurred_at_ms}] {im.user_a} == {im.user_b} "
                f"(channel={im.channel!r})"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def distill(
    bundle: SourceBundle,
    *,
    kind: EpisodeKind,
    provider: SummaryProvider | SummaryFn,
    provider_alias: str = "default-summary",
    max_messages_per_call: int = 60,
) -> DistilledSummary:
    """Run the iter-3 distillation pipeline on ``bundle``.

    Steps:
      1. Render bundle to a textual dump.
      2. Concatenate the per-kind prompt segment.
      3. PII-redact the *prompt* — the LLM never sees raw
         phone/email/cc/ip.
      4. Call the injected provider.
      5. PII-redact the LLM output.

    Returns a :class:`DistilledSummary` carrying both the final
    summary and the intermediate strings (debug / test asserts).
    """
    rendered = _render_bundle(bundle, max_messages=max_messages_per_call)
    segment = PROMPT_SEGMENTS.get(kind, PROMPT_SEGMENTS[EpisodeKind.CONVERSATION])
    full_prompt = f"{segment}\n\n{rendered}"
    prompt_redacted = redact_pii(full_prompt)

    raw_output = await provider(prompt=prompt_redacted, kind=kind)
    summary_redacted = redact_pii(raw_output)

    return DistilledSummary(
        summary_text=summary_redacted,
        prompt_redacted=prompt_redacted,
        raw_output=raw_output,
        distilled_by=provider_alias,
    )


# ---------------------------------------------------------------------------
# Test stubs
# ---------------------------------------------------------------------------


def make_echo_provider(prefix: str = "[summary]") -> SummaryProvider:
    """Deterministic test stub — returns ``f"{prefix} {kind} {prompt}"``.

    Used by the iter-3 tests so an LLM never gets called. The
    real-provider adapter lands in iter 7.
    """

    async def _stub(*, prompt: str, kind: EpisodeKind) -> str:
        return f"{prefix} {kind} :: {prompt}"

    return _stub


def make_constant_provider(text: str) -> SummaryProvider:
    """Stub that always returns ``text``; useful for asserting that
    PII redaction runs over the LLM *output* even when the prompt
    redaction missed.
    """

    async def _stub(*, prompt: str, kind: EpisodeKind) -> str:
        return text

    return _stub


__all__ = [
    "PROMPT_SEGMENTS",
    "DistilledSummary",
    "SummaryFn",
    "SummaryProvider",
    "distill",
    "make_constant_provider",
    "make_echo_provider",
    "redact_pii",
]
