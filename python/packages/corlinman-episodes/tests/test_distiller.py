"""Iter 3 tests — LLM distillation pipeline (with stub provider).

Pin the per-kind prompt segment selection, the PII redactor (pre-
and post-LLM), and the provider-injection seam. No real
``corlinman-providers`` import — the runner injects the adapter at
iter 7.
"""

from __future__ import annotations

from corlinman_episodes import (
    PROMPT_SEGMENTS,
    EpisodeKind,
    SessionMessage,
    SignalRow,
    SourceBundle,
    distill,
    make_constant_provider,
    make_echo_provider,
    redact_pii,
)


def _bundle_with_pii() -> SourceBundle:
    """Bundle whose messages carry PII the redactor must clean.

    Picks the highest-leverage payloads — phone, email, IP, plus a
    bare digit run. Each test pulls the bundle through the distiller
    and asserts the redactor stripped them on both sides of the LLM.
    """
    b = SourceBundle(tenant_id="t", session_key="sess-A")
    b.messages.append(
        SessionMessage(
            session_key="sess-A",
            seq=0,
            role="user",
            content="My phone is +1 (555) 123-4567 and email is alice@example.com",
            ts_ms=10,
        )
    )
    b.messages.append(
        SessionMessage(
            session_key="sess-A",
            seq=1,
            role="user",
            content="Server at 10.0.0.42, card 4111111111111111",
            ts_ms=20,
        )
    )
    b.signals.append(
        SignalRow(
            id=1,
            event_kind="x",
            target="t",
            severity="warn",
            payload_json="{}",
            session_id="sess-A",
            observed_at_ms=15,
        )
    )
    return b


# ---------------------------------------------------------------------------
# redact_pii — direct unit tests
# ---------------------------------------------------------------------------


def test_redact_pii_strips_email_and_phone() -> None:
    """Both replacements applied; idempotent on re-run."""
    raw = "Email alice@example.com, phone +1 555 123 4567"
    once = redact_pii(raw)
    twice = redact_pii(once)
    assert "alice@example.com" not in once
    assert "555" not in once
    assert "[email]" in once and "[phone]" in once
    # Idempotent — running redaction twice is a no-op.
    assert twice == once


def test_redact_pii_strips_ip_and_cc() -> None:
    raw = "ip 10.0.0.42 card 4111111111111111"
    out = redact_pii(raw)
    assert "10.0.0.42" not in out
    assert "4111111111111111" not in out
    assert "[ip]" in out and "[cc]" in out


def test_redact_pii_handles_empty_string() -> None:
    """Defensive — collector can return empty payload_json."""
    assert redact_pii("") == ""


# ---------------------------------------------------------------------------
# distill — provider call shape
# ---------------------------------------------------------------------------


async def test_distill_picks_kind_specific_prompt_segment() -> None:
    """The LLM is invoked with the kind's prompt segment as prefix.

    The echo stub returns its prompt verbatim so we can assert the
    segment shows up; if the dispatch ever fell through to the
    default, an INCIDENT bundle would get summarised in conversation
    tone — load-bearing for the recall-quality test.
    """
    bundle = SourceBundle(tenant_id="t", session_key="sess-A")
    provider = make_echo_provider()
    result = await distill(
        bundle,
        kind=EpisodeKind.INCIDENT,
        provider=provider,
    )
    incident_segment = PROMPT_SEGMENTS[EpisodeKind.INCIDENT]
    assert incident_segment.split(".")[0] in result.prompt_redacted
    assert "EpisodeKind.INCIDENT" in result.summary_text or (
        "incident" in result.summary_text
    )


async def test_distill_redacts_input_to_provider() -> None:
    """The provider must never see raw PII.

    Pinned by the design test
    ``pii_redactor_runs_pre_and_post_llm`` (matrix row 13).
    """
    bundle = _bundle_with_pii()
    captured: dict[str, str] = {}

    async def capturing(*, prompt: str, kind: EpisodeKind) -> str:
        captured["prompt"] = prompt
        return "ok"

    await distill(bundle, kind=EpisodeKind.CONVERSATION, provider=capturing)
    assert "alice@example.com" not in captured["prompt"]
    assert "[email]" in captured["prompt"]
    assert "+1 (555) 123-4567" not in captured["prompt"]
    assert "[phone]" in captured["prompt"]
    assert "10.0.0.42" not in captured["prompt"]
    assert "4111111111111111" not in captured["prompt"]


async def test_distill_redacts_llm_output() -> None:
    """A misbehaving LLM that emits PII gets the post-pass redactor.

    Defence in depth — the pre-pass is the primary guard; this is
    the second line.
    """
    bundle = SourceBundle(tenant_id="t", session_key="sess-A")
    provider = make_constant_provider(
        "Summary: user gave phone +1 (555) 999-8888 and email evil@example.com"
    )
    result = await distill(
        bundle,
        kind=EpisodeKind.CONVERSATION,
        provider=provider,
    )
    assert "evil@example.com" not in result.summary_text
    assert "+1 (555) 999-8888" not in result.summary_text
    assert "[email]" in result.summary_text
    assert "[phone]" in result.summary_text


async def test_distill_returns_distilled_by_alias() -> None:
    """The returned :class:`DistilledSummary` carries the provider
    alias so the runner can stamp ``distilled_by`` on the row.
    """
    bundle = SourceBundle(tenant_id="t", session_key="sess-A")
    result = await distill(
        bundle,
        kind=EpisodeKind.CONVERSATION,
        provider=make_constant_provider("ok"),
        provider_alias="my-summary-alias",
    )
    assert result.distilled_by == "my-summary-alias"


async def test_distill_truncates_at_max_messages_per_call() -> None:
    """The bundle renderer respects ``max_messages_per_call``.

    Without this the design's
    ``large_window_batches_in_chunks`` test would burn an
    unbounded prompt against a 30-day backfill.
    """
    bundle = SourceBundle(tenant_id="t", session_key="sess-A")
    bundle.messages.extend(
        SessionMessage(
            session_key="sess-A",
            seq=i,
            role="user",
            content=f"msg-{i}",
            ts_ms=100 + i,
        )
        for i in range(20)
    )

    captured: dict[str, str] = {}

    async def capturing(*, prompt: str, kind: EpisodeKind) -> str:
        captured["prompt"] = prompt
        return "ok"

    await distill(
        bundle,
        kind=EpisodeKind.CONVERSATION,
        provider=capturing,
        max_messages_per_call=5,
    )
    # Only the first 5 messages render in detail; the rest are
    # summarised via the truncation marker.
    assert "msg-0" in captured["prompt"]
    assert "msg-4" in captured["prompt"]
    assert "msg-5" not in captured["prompt"]
    assert "more truncated" in captured["prompt"]


async def test_distill_uses_conversation_segment_for_unknown_kind() -> None:
    """Defensive — an unmapped kind falls back to CONVERSATION
    segment rather than raising.

    Future kinds get added with their own segment in the same patch;
    this is the safety net for the in-flight migration window.
    """
    bundle = SourceBundle(tenant_id="t", session_key="sess-A")

    captured: dict[str, str] = {}

    async def capturing(*, prompt: str, kind: EpisodeKind) -> str:
        captured["prompt"] = prompt
        return "ok"

    # Synthesise an "unknown" kind by removing one from the lookup
    # for the duration of the call.
    saved = PROMPT_SEGMENTS.pop(EpisodeKind.OPERATOR)
    try:
        await distill(
            bundle, kind=EpisodeKind.OPERATOR, provider=capturing
        )
    finally:
        PROMPT_SEGMENTS[EpisodeKind.OPERATOR] = saved

    convo_segment = PROMPT_SEGMENTS[EpisodeKind.CONVERSATION]
    assert convo_segment.split(".")[0] in captured["prompt"]


# ---------------------------------------------------------------------------
# Stub provider helpers (smoke)
# ---------------------------------------------------------------------------


async def test_make_echo_provider_returns_callable() -> None:
    """The convenience builder hands back the right Protocol shape."""
    provider = make_echo_provider("[t]")
    out = await provider(prompt="hello", kind=EpisodeKind.CONVERSATION)
    assert "[t]" in out
    assert "hello" in out


async def test_make_constant_provider_ignores_input() -> None:
    """Constant stub yields the same string regardless of prompt."""
    provider = make_constant_provider("always-this")
    a = await provider(prompt="x", kind=EpisodeKind.INCIDENT)
    b = await provider(prompt="y", kind=EpisodeKind.CONVERSATION)
    assert a == b == "always-this"


# Mark async tests — pytest-asyncio's auto mode picks them up but
# we keep one explicit assertion-style call for the synchronous
# ones to keep ruff happy.
pytestmark = []
