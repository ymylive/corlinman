"""Trait kind enum + ``UserTrait`` dataclass.

A trait is one observation about a user that is stable across
conversations: an interest, a tonal preference, a recurring topic, or
an explicit preference. Volatile state ("the user is currently angry",
"the user just asked about X") does not belong here — that is the
persona / session-recall layer.

Trait kinds are intentionally a small fixed enum: an open string field
would let the LLM hallucinate kinds and break the placeholder resolver
(which keys off ``trait_kind``). The four kinds match
``docs/design/phase3-roadmap.md`` §5.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TraitKind(StrEnum):
    """Closed set of trait kinds. Anything else is rejected at parse time."""

    INTEREST = "interest"
    TONE = "tone"
    TOPIC = "topic"
    PREFERENCE = "preference"

    @classmethod
    def parse(cls, raw: str) -> TraitKind | None:
        """Best-effort parse. Returns ``None`` for unknown values.

        Permissive on case and whitespace so the LLM can be sloppy without
        nuking the whole batch.
        """
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class UserTrait:
    """One distilled trait. Immutable; stores get to mutate confidence on upsert."""

    user_id: str
    trait_kind: TraitKind
    trait_value: str
    confidence: float
    first_seen: int  # unix ms
    last_seen: int  # unix ms
    session_ids: tuple[str, ...]


__all__ = ["TraitKind", "UserTrait"]
