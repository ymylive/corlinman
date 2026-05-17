"""Verification-phrase exchange protocol.

Direct port of ``rust/crates/corlinman-identity/src/verification.rs``.
Cross-channel unification by deliberate human action: an operator
issues a phrase from one channel, the human pastes it on the other,
the server unifies the two ``user_id``s into one. The friction is the
point — automatic merging based on fuzzy signals (same display name,
same timezone) is a privacy hazard. The phrase makes the human prove
they own both channels.

Protocol
--------

1. Operator triggers ``issue_phrase(user_id, channel,
   channel_user_id)`` from the source channel. Server stores the phrase
   with ``expires_at = now + DEFAULT_TTL_MIN`` and returns it.
2. The chat plugin echoes the phrase to the user on the source channel.
3. User types the phrase on the target channel. The plugin sees the
   message and calls ``redeem_phrase(phrase, target_channel,
   target_channel_user_id)``.
4. Server reattributes the target alias's ``user_id`` to the source
   ``user_id``, deletes the orphaned target user (cascade clears any
   other aliases bound to it), and marks the phrase consumed.

Phrase format
-------------

Three Crockford-base32 syllables joined by hyphens, e.g. ``K8M-3PX-Q2R``.
~32^9 ≈ 3.5 × 10^13 combinations; with a 30-min TTL and ~1k phrases/day
max, collision risk is negligible.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from corlinman_identity.error import (
    InvalidInputError,
    PhraseAlreadyConsumedError,
    PhraseExpiredError,
    PhraseUnknownError,
    StorageError,
)
from corlinman_identity.resolver import _now_utc_rfc3339, _parse_rfc3339, _to_rfc3339
from corlinman_identity.store import SqliteIdentityStore
from corlinman_identity.types import UserId, VerificationPhrase

# Phrase TTL in minutes. 30 minutes balances "long enough for a human
# to switch apps and paste" against "short enough that a leaked phrase
# isn't a long-lived risk".
DEFAULT_TTL_MIN = 30

# Crockford-base32 alphabet (excludes I, L, O, U for readability).
_PHRASE_ALPHABET = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_phrase() -> str:
    """Generate a fresh phrase.

    9 chars total (3 × 3-char syllables) split by hyphens. Matches the
    Rust ``generate_phrase`` byte-for-byte.
    """
    raw = os.urandom(9)
    out: list[str] = []
    for i, b in enumerate(raw):
        out.append(chr(_PHRASE_ALPHABET[b & 0x1F]))
        if i % 3 == 2 and i != 8:
            out.append("-")
    return "".join(out)


# ---------------------------------------------------------------------------
# Inherent methods on SqliteIdentityStore
# ---------------------------------------------------------------------------


async def _issue_phrase(
    self: SqliteIdentityStore,
    user_id: UserId,
    channel: str,
    channel_user_id: str,
) -> VerificationPhrase:
    """Issue a fresh verification phrase for ``user_id`` on
    ``(channel, channel_user_id)``.

    The phrase expires in :data:`DEFAULT_TTL_MIN` minutes. Returns the
    persisted record so the caller can echo the phrase back over the
    chat channel.

    The caller (admin route) is responsible for asserting that
    ``(channel, channel_user_id)`` actually maps to ``user_id`` — this
    method just stores the row.
    """
    if not channel or not channel_user_id:
        raise InvalidInputError("channel and channel_user_id must be non-empty")

    phrase = _generate_phrase()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=DEFAULT_TTL_MIN)
    expires_str = _to_rfc3339(expires_at)

    try:
        await self.conn.execute(
            "INSERT INTO verification_phrases "
            "(phrase, issued_to_user_id, issued_on_channel, "
            " issued_on_channel_user_id, expires_at) "
            "VALUES (?1, ?2, ?3, ?4, ?5)",
            (phrase, str(user_id), channel, channel_user_id, expires_str),
        )
        await self.conn.commit()
    except Exception as exc:
        raise StorageError(op="issue_phrase", source=exc) from exc

    return VerificationPhrase(
        phrase=phrase,
        user_id=user_id,
        issued_on_channel=channel,
        issued_on_channel_user_id=channel_user_id,
        expires_at=expires_at,
    )


async def _redeem_phrase(
    self: SqliteIdentityStore,
    phrase: str,
    redeemed_on_channel: str,
    redeemed_on_channel_user_id: str,
) -> UserId:
    """Redeem a phrase issued on a different channel.

    Reattributes ``(redeemed_on_channel, redeemed_on_channel_user_id)``
    to the phrase's issuing ``user_id``, deletes the orphaned redeemer
    ``user_id`` row (cascade clears its other aliases), and marks the
    phrase consumed.

    Raises:
        PhraseUnknownError: phrase doesn't match any row.
        PhraseExpiredError: past ``expires_at``.
        PhraseAlreadyConsumedError: ``consumed_at IS NOT NULL``.

    Returns the surviving (issuer's) ``UserId`` so the caller can echo
    "identity unified — your QQ traits now apply on Telegram".
    """
    if not phrase:
        raise InvalidInputError("phrase must be non-empty")
    if not redeemed_on_channel or not redeemed_on_channel_user_id:
        raise InvalidInputError(
            "redeemed_on_channel and redeemed_on_channel_user_id must be non-empty"
        )

    # Same single-connection serialisation story as
    # ``resolve_or_create`` and ``merge_users`` — the ``BEGIN..COMMIT``
    # block must be held against interleaved coroutines.
    async with self.tx_lock:
        return await _redeem_phrase_locked(
            self,
            phrase,
            redeemed_on_channel,
            redeemed_on_channel_user_id,
        )


async def _redeem_phrase_locked(
    self: SqliteIdentityStore,
    phrase: str,
    redeemed_on_channel: str,
    redeemed_on_channel_user_id: str,
) -> UserId:
    try:
        await self.conn.execute("BEGIN")
    except Exception as exc:
        raise StorageError(op="begin_redeem", source=exc) from exc

    try:
        # 1. Look up the phrase row.
        try:
            cursor = await self.conn.execute(
                "SELECT issued_to_user_id, expires_at, consumed_at "
                "FROM verification_phrases WHERE phrase = ?1",
                (phrase,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception as exc:
            raise StorageError(op="redeem_lookup", source=exc) from exc

        if row is None:
            raise PhraseUnknownError()

        issued_to_user_id = str(row[0])
        expires_at_str = str(row[1])
        consumed_at = row[2]

        if consumed_at is not None:
            raise PhraseAlreadyConsumedError()
        expires_at = _parse_rfc3339(expires_at_str)
        if datetime.now(timezone.utc) >= expires_at:
            raise PhraseExpiredError()

        now_str = _now_utc_rfc3339()

        # 2. Find the redeemer's current user_id, if any.
        try:
            cursor = await self.conn.execute(
                "SELECT user_id FROM user_aliases "
                "WHERE channel = ?1 AND channel_user_id = ?2",
                (redeemed_on_channel, redeemed_on_channel_user_id),
            )
            redeemer_row = await cursor.fetchone()
            await cursor.close()
        except Exception as exc:
            raise StorageError(op="redeem_find_redeemer", source=exc) from exc

        redeemer_user_id = None if redeemer_row is None else str(redeemer_row[0])

        if redeemer_user_id is None:
            # First-ever message on the target channel happens to be
            # the redeem itself — bind a fresh ``verified`` alias.
            try:
                await self.conn.execute(
                    "INSERT INTO user_aliases "
                    "(channel, channel_user_id, user_id, created_at, binding_kind) "
                    "VALUES (?1, ?2, ?3, ?4, 'verified')",
                    (
                        redeemed_on_channel,
                        redeemed_on_channel_user_id,
                        issued_to_user_id,
                        now_str,
                    ),
                )
            except Exception as exc:
                raise StorageError(op="redeem_bind_fresh_alias", source=exc) from exc
        elif redeemer_user_id == issued_to_user_id:
            # Already unified — fall through to the "mark consumed"
            # step. No-op merge.
            pass
        else:
            # Reattribute every alias on the redeemer to the issuer.
            try:
                await self.conn.execute(
                    "UPDATE user_aliases "
                    "SET user_id = ?1, binding_kind = 'verified' "
                    "WHERE user_id = ?2",
                    (issued_to_user_id, redeemer_user_id),
                )
            except Exception as exc:
                raise StorageError(
                    op="redeem_reattribute_aliases", source=exc
                ) from exc
            try:
                await self.conn.execute(
                    "DELETE FROM user_identities WHERE user_id = ?1",
                    (redeemer_user_id,),
                )
            except Exception as exc:
                raise StorageError(
                    op="redeem_delete_orphan_user", source=exc
                ) from exc

        # 3. Mark the phrase consumed. The ``WHERE consumed_at IS NULL``
        # guard makes a duplicate-redeem race deterministic — only one
        # transaction's UPDATE matches.
        try:
            cursor = await self.conn.execute(
                "UPDATE verification_phrases "
                "SET consumed_at = ?1, consumed_on_channel = ?2, "
                "    consumed_on_channel_user_id = ?3 "
                "WHERE phrase = ?4 AND consumed_at IS NULL",
                (
                    now_str,
                    redeemed_on_channel,
                    redeemed_on_channel_user_id,
                    phrase,
                ),
            )
            rowcount = cursor.rowcount
            await cursor.close()
        except Exception as exc:
            raise StorageError(op="redeem_mark_consumed", source=exc) from exc

        if rowcount == 0:
            # A concurrent redeemer won the race. Surface the
            # already-consumed error so the UX path is consistent.
            raise PhraseAlreadyConsumedError()

        try:
            await self.conn.commit()
        except Exception as exc:
            raise StorageError(op="redeem_commit", source=exc) from exc
    except BaseException:
        await self.conn.rollback()
        raise

    return UserId(issued_to_user_id)


async def _sweep_expired_phrases(self: SqliteIdentityStore) -> int:
    """Garbage-collect expired, unconsumed phrases.

    Returns the number of rows removed. Operators can wire this into a
    cron once the Python scheduler surface is on this branch; the
    package ships the helper so consumers don't need to know the schema.
    """
    now_str = _now_utc_rfc3339()
    try:
        cursor = await self.conn.execute(
            "DELETE FROM verification_phrases "
            "WHERE consumed_at IS NULL AND expires_at < ?1",
            (now_str,),
        )
        deleted = int(cursor.rowcount or 0)
        await cursor.close()
        await self.conn.commit()
    except Exception as exc:
        raise StorageError(op="sweep_expired_phrases", source=exc) from exc
    return deleted


# ---------------------------------------------------------------------------
# Bind onto SqliteIdentityStore
# ---------------------------------------------------------------------------

SqliteIdentityStore.issue_phrase = _issue_phrase  # type: ignore[attr-defined]
SqliteIdentityStore.redeem_phrase = _redeem_phrase  # type: ignore[attr-defined]
SqliteIdentityStore.sweep_expired_phrases = _sweep_expired_phrases  # type: ignore[attr-defined]


__all__ = [
    "DEFAULT_TTL_MIN",
]
