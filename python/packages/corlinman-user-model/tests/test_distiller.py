"""Unit tests for :mod:`corlinman_user_model.distiller`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from corlinman_user_model.distiller import (
    DistillerConfig,
    distill_session,
    redact_text,
)
from corlinman_user_model.store import UserModelStore
from corlinman_user_model.traits import TraitKind

from .conftest import insert_turn

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_phone_number() -> None:
    text = "我的电话是 13800138000，请联系我"
    out = redact_text(text)
    assert "13800138000" not in out
    assert "[REDACTED]" in out


def test_redact_email() -> None:
    text = "给我发邮件 user@example.com"
    out = redact_text(text)
    assert "user@example.com" not in out
    assert "[REDACTED]" in out


def test_redact_chinese_id_number() -> None:
    text = "身份证 110101199003078888 是测试号"
    out = redact_text(text)
    assert "110101199003078888" not in out
    assert "[REDACTED]" in out


def test_redact_url() -> None:
    text = "see https://example.com/path?x=1 for more"
    out = redact_text(text)
    assert "https://example.com" not in out
    assert "[REDACTED]" in out


def test_redact_leaves_safe_text_alone() -> None:
    text = "I love Rust async runtimes."
    assert redact_text(text) == text


def test_redact_handles_multiple_pii_in_one_string() -> None:
    text = "phone 13800138000, email a@b.co, url http://x.y"
    out = redact_text(text)
    assert "13800138000" not in out
    assert "a@b.co" not in out
    assert "http://x.y" not in out
    assert out.count("[REDACTED]") == 3


# ---------------------------------------------------------------------------
# Phase 3.1: expanded redaction surface
# ---------------------------------------------------------------------------


def test_redact_international_phone_with_separators() -> None:
    """``+86 138-0013-8000`` is what real chats send; the v0 11-digit
    regex missed it because of the separators."""
    text = "联系我 +86 138-0013-8000"
    out = redact_text(text)
    assert "138" not in out or "[REDACTED]" in out
    assert "[REDACTED]" in out


def test_redact_chinese_id_x_check_digit() -> None:
    """Chinese ID with ``X`` check digit (17 digits + X)."""
    text = "ID 11010119900307875X 是测试"
    out = redact_text(text)
    assert "11010119900307875X" not in out
    assert "[REDACTED]" in out


def test_redact_bank_card_with_luhn() -> None:
    """A 16-digit bank-card PAN that passes Luhn must be redacted."""
    # 4111111111111111 — well-known Luhn-valid Visa test number.
    text = "card 4111111111111111 expires next year"
    out = redact_text(text)
    assert "4111111111111111" not in out
    assert "[REDACTED]" in out


def test_redact_bank_card_keeps_invalid_luhn() -> None:
    """Random 16-digit run that fails Luhn (e.g. an order id) is left
    alone — that's the whole point of running Luhn instead of a bare
    digit regex."""
    # 1234567890123456 fails Luhn.
    text = "order 1234567890123456 ships today"
    out = redact_text(text)
    assert "1234567890123456" in out, (
        "Luhn-invalid run must NOT be redacted; out={out!r}"
    )


def test_redact_ipv4() -> None:
    text = "see 192.168.1.1 for the gateway"
    out = redact_text(text)
    assert "192.168.1.1" not in out
    assert "[REDACTED]" in out


def test_redact_ipv6() -> None:
    text = "v6 addr 2001:0db8:85a3:0000:0000:8a2e:0370:7334 right here"
    out = redact_text(text)
    assert "2001:0db8" not in out
    assert "[REDACTED]" in out


def test_redact_qq_number() -> None:
    text = "QQ:1234567 加我聊聊"
    out = redact_text(text)
    assert "1234567" not in out
    assert "[REDACTED]" in out


def test_redact_url_with_ip_does_not_get_double_redacted() -> None:
    """URL ordering: the URL pattern eats the whole thing first so the
    embedded IP doesn't leak through as a half-redacted URL stub."""
    text = "fetch http://192.168.1.1/health for status"
    out = redact_text(text)
    assert "192.168" not in out
    # Exactly one [REDACTED] for the URL — not "[REDACTED]/health" or similar
    # post-ip-redaction breakage.
    assert out.count("[REDACTED]") == 1


def test_redact_full_pii_battery_from_security_review() -> None:
    """Every PII shape the Phase 3 security review flagged. One pass
    must redact every entry — operators reading this test should be
    able to copy the fixture into a real chat and see all six redact
    cleanly without one masking another."""
    fixture = (
        "tel +86 138-0013-8000\n"
        "id  11010119900307875X\n"
        "card 4111111111111111\n"  # Luhn-valid Visa test
        "ipv4 8.8.8.8\n"
        "ipv6 2001:db8::1\n"
        "QQ:1234567"
    )
    out = redact_text(fixture)
    for token in [
        "+86",
        "11010119900307875X",
        "4111111111111111",
        "8.8.8.8",
        "2001:db8",
        "1234567",
    ]:
        assert token not in out, f"{token!r} survived redaction in {out!r}"


# ---------------------------------------------------------------------------
# Phase 3.1: LLM output PII filter + evidence drop
# ---------------------------------------------------------------------------


async def test_distill_drops_traits_with_pii_in_value(
    tmp_path: Path, sessions_db: Path
) -> None:
    """Even when the LLM ignores the prompt and stuffs an email into a
    ``value`` field, the post-parse redaction filter drops that trait
    rather than persisting it."""
    _seed_long_session(sessions_db)
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )
    caller = _make_llm_caller(
        [
            {
                "kind": "interest",
                "value": "Rust 异步运行时",
                "confidence": 0.85,
            },
            # Simulated prompt-injection: value contains an email.
            {
                "kind": "tone",
                "value": "user is admin@example.com",
                "confidence": 0.9,
            },
        ]
    )

    traits = await distill_session(config, "qq:42", llm_caller=caller, now_ms=1)
    # The clean trait survives; the PII-bearing one is dropped.
    assert len(traits) == 1
    assert traits[0].trait_value == "Rust 异步运行时"


async def test_distill_does_not_persist_evidence_field(
    tmp_path: Path, sessions_db: Path
) -> None:
    """An ``evidence`` field on the LLM response (legacy / regression)
    must never reach the store. The store has no column for it, so
    the smoke test is "the trait still lands but the JSON column body
    doesn't carry the literal evidence string"."""
    _seed_long_session(sessions_db)
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )
    caller = _make_llm_caller(
        [
            {
                "kind": "interest",
                "value": "Rust",
                "confidence": 0.85,
                "evidence": "用户原文：联系我 13800138000",
            },
        ]
    )

    traits = await distill_session(config, "qq:42", llm_caller=caller, now_ms=1)
    assert len(traits) == 1
    # Round-trip via store: evidence must not show up in any column.
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        rows = await s.list_traits_for_user("qq:42", min_confidence=0.0)
    assert len(rows) == 1
    # Trait carries no evidence; the dataclass doesn't even have a
    # field for it. Belt-and-suspenders: the captured trait_value is
    # only the short phrase, never the evidence body.
    assert "13800138000" not in rows[0].trait_value
    assert "联系我" not in rows[0].trait_value


async def test_distill_system_prompt_does_not_request_evidence() -> None:
    """The new prompt drops the ``evidence`` field — pin it so a future
    edit can't silently re-introduce the PII ingress."""
    from corlinman_user_model.distiller import DISTILL_SYSTEM_PROMPT

    assert "evidence" not in DISTILL_SYSTEM_PROMPT
    assert "PII" in DISTILL_SYSTEM_PROMPT or "电话" in DISTILL_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# distill_session — wire test with a mocked LLM caller
# ---------------------------------------------------------------------------


def _make_llm_caller(payload: list[dict[str, object]]) -> object:
    """Build an ``async def`` LLM caller that returns ``payload`` JSON-encoded."""

    async def caller(_system: str, _transcript: str) -> str:
        return json.dumps(payload, ensure_ascii=False)

    return caller


def _seed_long_session(sessions_db: Path, *, session_key: str = "qq:42") -> None:
    """Five user/assistant turns — enough to clear the default min-turn floor."""
    insert_turn(sessions_db, session_key=session_key, seq=0, role="user", content="你好")
    insert_turn(
        sessions_db, session_key=session_key, seq=1, role="assistant", content="hi"
    )
    insert_turn(
        sessions_db,
        session_key=session_key,
        seq=2,
        role="user",
        content="想聊聊 Rust 异步运行时",
    )
    insert_turn(
        sessions_db,
        session_key=session_key,
        seq=3,
        role="assistant",
        content="好的，我们从 tokio 开始",
    )
    insert_turn(
        sessions_db,
        session_key=session_key,
        seq=4,
        role="user",
        content="请简洁直接，别废话",
    )


async def test_distill_session_writes_traits(
    tmp_path: Path, sessions_db: Path
) -> None:
    _seed_long_session(sessions_db)
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )
    caller = _make_llm_caller(
        [
            {
                "kind": "interest",
                "value": "Rust 异步运行时",
                "confidence": 0.85,
                "evidence": "想聊聊 Rust 异步运行时",
            },
            {
                "kind": "tone",
                "value": "简洁直接",
                "confidence": 0.75,
                "evidence": "请简洁直接，别废话",
            },
        ]
    )

    traits = await distill_session(
        config, "qq:42", llm_caller=caller, now_ms=10_000
    )
    assert len(traits) == 2
    kinds = {t.trait_kind for t in traits}
    assert kinds == {TraitKind.INTEREST, TraitKind.TONE}

    # Round-trip through the store.
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        persisted = await s.list_traits_for_user("qq:42", min_confidence=0.0)
    assert len(persisted) == 2
    assert all(t.user_id == "qq:42" for t in persisted)
    # Both traits get the session id appended.
    for t in persisted:
        assert t.session_ids == ("qq:42",)


async def test_distill_session_drops_low_confidence(
    tmp_path: Path, sessions_db: Path
) -> None:
    _seed_long_session(sessions_db)
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
        trait_confidence_floor=0.6,
    )
    caller = _make_llm_caller(
        [
            {
                "kind": "interest",
                "value": "high",
                "confidence": 0.9,
                "evidence": "x",
            },
            {
                "kind": "interest",
                "value": "low",
                "confidence": 0.4,  # below floor 0.6
                "evidence": "y",
            },
        ]
    )

    traits = await distill_session(config, "qq:42", llm_caller=caller)
    assert len(traits) == 1
    assert traits[0].trait_value == "high"


async def test_distill_session_skips_short_session(
    tmp_path: Path, sessions_db: Path
) -> None:
    """Fewer than ``distill_after_session_turns`` turns ⇒ no LLM call."""
    insert_turn(sessions_db, session_key="qq:1", seq=0, role="user", content="hi")
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )

    called = False

    async def caller(_s: str, _t: str) -> str:
        nonlocal called
        called = True
        return "[]"

    traits = await distill_session(config, "qq:1", llm_caller=caller)
    assert traits == []
    assert called is False


async def test_distill_session_handles_fenced_json(
    tmp_path: Path, sessions_db: Path
) -> None:
    """Models love wrapping JSON in ``` fences. We must tolerate it."""
    _seed_long_session(sessions_db)
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )

    body = json.dumps(
        [
            {
                "kind": "topic",
                "value": "tokio",
                "confidence": 0.7,
                "evidence": "x",
            }
        ]
    )

    async def caller(_s: str, _t: str) -> str:
        return f"```json\n{body}\n```"

    traits = await distill_session(config, "qq:42", llm_caller=caller)
    assert len(traits) == 1
    assert traits[0].trait_kind is TraitKind.TOPIC


async def test_distill_session_drops_unknown_kind(
    tmp_path: Path, sessions_db: Path
) -> None:
    _seed_long_session(sessions_db)
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )
    caller = _make_llm_caller(
        [
            {"kind": "made_up", "value": "x", "confidence": 0.9},
            {
                "kind": "preference",
                "value": "中文回复",
                "confidence": 0.7,
                "evidence": "x",
            },
        ]
    )

    traits = await distill_session(config, "qq:42", llm_caller=caller)
    assert [t.trait_kind for t in traits] == [TraitKind.PREFERENCE]


async def test_distill_session_redacts_before_llm_call(
    tmp_path: Path, sessions_db: Path
) -> None:
    """The transcript handed to the LLM must contain ``[REDACTED]`` not raw PII."""
    insert_turn(
        sessions_db,
        session_key="qq:42",
        seq=0,
        role="user",
        content="联系我 13800138000",
    )
    insert_turn(
        sessions_db, session_key="qq:42", seq=1, role="assistant", content="ok"
    )
    insert_turn(
        sessions_db,
        session_key="qq:42",
        seq=2,
        role="user",
        content="email: foo@bar.com",
    )
    insert_turn(
        sessions_db, session_key="qq:42", seq=3, role="assistant", content="got it"
    )
    insert_turn(
        sessions_db,
        session_key="qq:42",
        seq=4,
        role="user",
        content="thanks",
    )
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )

    seen_transcript: list[str] = []

    async def caller(_s: str, transcript: str) -> str:
        seen_transcript.append(transcript)
        return "[]"

    await distill_session(config, "qq:42", llm_caller=caller)
    assert len(seen_transcript) == 1
    assert "13800138000" not in seen_transcript[0]
    assert "foo@bar.com" not in seen_transcript[0]
    assert "[REDACTED]" in seen_transcript[0]


async def test_distill_session_weighted_average_on_repeat(
    tmp_path: Path, sessions_db: Path
) -> None:
    """Two distill runs of the same trait should average via the store path."""
    _seed_long_session(sessions_db)
    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )

    first = _make_llm_caller(
        [
            {
                "kind": "interest",
                "value": "Rust 异步运行时",
                "confidence": 0.5,
                "evidence": "x",
            }
        ]
    )
    second = _make_llm_caller(
        [
            {
                "kind": "interest",
                "value": "Rust 异步运行时",
                "confidence": 0.9,
                "evidence": "x",
            }
        ]
    )
    await distill_session(config, "qq:42", llm_caller=first, now_ms=1_000)
    await distill_session(config, "qq:42", llm_caller=second, now_ms=2_000)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        traits = await s.list_traits_for_user("qq:42", min_confidence=0.0)

    assert len(traits) == 1
    assert traits[0].confidence == pytest.approx(0.62, abs=1e-6)


async def test_distill_session_ignores_tool_turns(
    tmp_path: Path, sessions_db: Path
) -> None:
    """Tool / system turns must not appear in the transcript handed to the LLM."""
    insert_turn(sessions_db, session_key="qq:1", seq=0, role="user", content="hello")
    insert_turn(
        sessions_db, session_key="qq:1", seq=1, role="assistant", content="hi"
    )
    insert_turn(
        sessions_db,
        session_key="qq:1",
        seq=2,
        role="tool",
        content="TOOL_PAYLOAD_DO_NOT_LEAK",
    )
    insert_turn(
        sessions_db, session_key="qq:1", seq=3, role="assistant", content="ok"
    )
    insert_turn(sessions_db, session_key="qq:1", seq=4, role="user", content="bye")

    db_path = tmp_path / "user_model.sqlite"
    config = DistillerConfig(
        db_path=db_path,
        sessions_db_path=sessions_db,
        distill_after_session_turns=5,
    )

    captured: list[str] = []

    async def caller(_s: str, transcript: str) -> str:
        captured.append(transcript)
        return "[]"

    await distill_session(config, "qq:1", llm_caller=caller)
    assert captured
    assert "TOOL_PAYLOAD_DO_NOT_LEAK" not in captured[0]
