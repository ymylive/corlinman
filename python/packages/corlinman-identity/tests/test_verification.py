"""Unit tests for :mod:`corlinman_identity.verification`.

Ports the Rust ``verification::tests`` module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from corlinman_identity import (
    PhraseAlreadyConsumedError,
    PhraseExpiredError,
    PhraseUnknownError,
    SqliteIdentityStore,
)
from corlinman_identity.verification import _generate_phrase


def test_generate_phrase_format_is_3x3_with_hyphens() -> None:
    p = _generate_phrase()
    assert len(p) == 11, "9 chars + 2 hyphens"
    parts = p.split("-")
    assert len(parts) == 3
    for part in parts:
        assert len(part) == 3
        for c in part:
            assert c.isascii()
            assert c.isupper() or c.isdigit(), (
                "phrase chars must be Crockford base32"
            )
            # I, L, O, U deliberately excluded for readability.
            assert c not in "ILOU", f"ambiguous char {c} must not appear"


def test_generate_phrase_is_unique_across_many_calls() -> None:
    seen: set[str] = set()
    for _ in range(1024):
        p = _generate_phrase()
        assert p not in seen
        seen.add(p)


async def test_issue_phrase_persists_row_and_returns_record(
    fresh_store: SqliteIdentityStore,
) -> None:
    uid = await fresh_store.resolve_or_create("qq", "1234", None)
    p = await fresh_store.issue_phrase(uid, "qq", "1234")
    assert p.user_id == uid
    assert p.issued_on_channel == "qq"
    assert p.expires_at > datetime.now(timezone.utc)

    cursor = await fresh_store.conn.execute(
        "SELECT COUNT(*) FROM verification_phrases WHERE phrase = ?",
        (p.phrase,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 1


async def test_redeem_phrase_unifies_two_existing_users(
    fresh_store: SqliteIdentityStore,
) -> None:
    qq_uid = await fresh_store.resolve_or_create("qq", "1234", None)
    tg_uid = await fresh_store.resolve_or_create("telegram", "9876", None)
    assert qq_uid != tg_uid

    p = await fresh_store.issue_phrase(qq_uid, "qq", "1234")

    surviving = await fresh_store.redeem_phrase(p.phrase, "telegram", "9876")
    assert surviving == qq_uid, "issuer's user_id wins"

    # Telegram alias now resolves to qq_uid.
    tg_now = await fresh_store.lookup("telegram", "9876")
    assert tg_now == qq_uid

    # Orphan tg_uid is gone.
    cursor = await fresh_store.conn.execute(
        "SELECT COUNT(*) FROM user_identities WHERE user_id = ?",
        (str(tg_uid),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 0

    # Reattributed alias is marked verified.
    cursor = await fresh_store.conn.execute(
        "SELECT binding_kind FROM user_aliases "
        "WHERE channel = 'telegram' AND channel_user_id = '9876'"
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and str(row[0]) == "verified"


async def test_redeem_phrase_binds_fresh_alias_when_redeemer_has_none(
    fresh_store: SqliteIdentityStore,
) -> None:
    qq_uid = await fresh_store.resolve_or_create("qq", "1234", None)
    p = await fresh_store.issue_phrase(qq_uid, "qq", "1234")

    surviving = await fresh_store.redeem_phrase(p.phrase, "telegram", "9876")
    assert surviving == qq_uid

    tg_now = await fresh_store.lookup("telegram", "9876")
    assert tg_now == qq_uid


async def test_redeem_phrase_unknown_errors(
    fresh_store: SqliteIdentityStore,
) -> None:
    with pytest.raises(PhraseUnknownError):
        await fresh_store.redeem_phrase("XXX-XXX-XXX", "qq", "1234")


async def test_redeem_phrase_already_consumed_errors(
    fresh_store: SqliteIdentityStore,
) -> None:
    qq_uid = await fresh_store.resolve_or_create("qq", "1234", None)
    p = await fresh_store.issue_phrase(qq_uid, "qq", "1234")

    await fresh_store.redeem_phrase(p.phrase, "telegram", "9876")
    with pytest.raises(PhraseAlreadyConsumedError):
        await fresh_store.redeem_phrase(p.phrase, "telegram", "9876")


async def test_redeem_phrase_expired_errors(
    fresh_store: SqliteIdentityStore,
) -> None:
    qq_uid = await fresh_store.resolve_or_create("qq", "1234", None)
    p = await fresh_store.issue_phrase(qq_uid, "qq", "1234")
    # Forcibly age past expiry via direct SQL — short-circuits the 30-min wait.
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    past = past.replace("+00:00", "Z")
    await fresh_store.conn.execute(
        "UPDATE verification_phrases SET expires_at = ? WHERE phrase = ?",
        (past, p.phrase),
    )
    await fresh_store.conn.commit()

    with pytest.raises(PhraseExpiredError):
        await fresh_store.redeem_phrase(p.phrase, "telegram", "9876")


async def test_sweep_expired_removes_only_unconsumed_past_phrases(
    fresh_store: SqliteIdentityStore,
) -> None:
    uid = await fresh_store.resolve_or_create("qq", "1234", None)

    live = await fresh_store.issue_phrase(uid, "qq", "1234")
    expired_unconsumed = await fresh_store.issue_phrase(uid, "qq", "1234")
    expired_consumed = await fresh_store.issue_phrase(uid, "qq", "1234")

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    past = past.replace("+00:00", "Z")

    await fresh_store.conn.execute(
        "UPDATE verification_phrases SET expires_at = ? WHERE phrase = ?",
        (past, expired_unconsumed.phrase),
    )
    await fresh_store.conn.execute(
        "UPDATE verification_phrases SET expires_at = ?, consumed_at = ? "
        "WHERE phrase = ?",
        (past, past, expired_consumed.phrase),
    )
    await fresh_store.conn.commit()

    removed = await fresh_store.sweep_expired_phrases()
    assert removed == 1, "only expired-unconsumed should be removed"

    cursor = await fresh_store.conn.execute(
        "SELECT COUNT(*) FROM verification_phrases WHERE phrase IN (?, ?)",
        (live.phrase, expired_consumed.phrase),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 2
