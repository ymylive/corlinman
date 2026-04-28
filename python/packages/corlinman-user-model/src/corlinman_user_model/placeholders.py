"""``{{user.*}}`` placeholder resolver.

Exposes one class, :class:`UserModelResolver`, that takes a placeholder
key like ``user.interests`` plus a ``user_id`` and returns a
comma-joined string of the top-k traits of the matching kind.

The intended caller is ``corlinman-agent``'s ``context_assembler``.
**This package does not import or modify** ``context_assembler`` —
wiring is a one-line follow-up that lives there. We just expose the
class and let the agent layer decide when to construct it.
"""

from __future__ import annotations

import logging

from corlinman_user_model.store import UserModelStore
from corlinman_user_model.traits import TraitKind

logger = logging.getLogger(__name__)


# Map placeholder suffix → trait kind. Plural noun form because
# {{user.interests}} reads more naturally than {{user.interest}}.
_KEY_TO_KIND: dict[str, TraitKind] = {
    "user.interests": TraitKind.INTEREST,
    "user.tone": TraitKind.TONE,
    "user.topics": TraitKind.TOPIC,
    "user.preferences": TraitKind.PREFERENCE,
}


class UserModelResolver:
    """Resolve ``{{user.*}}`` placeholders against ``user_model.sqlite``.

    Each call to :meth:`resolve` runs a SELECT bounded by ``top_k``;
    the store is responsible for ordering by confidence DESC. We do
    not cache here — the context_assembler is invoked once per turn
    and the table is small enough that the per-call overhead is
    negligible.

    Empty string is the canonical "no data" return; an empty result
    must not blow up the prompt template.
    """

    def __init__(
        self,
        store: UserModelStore,
        *,
        top_k: int = 3,
        min_confidence: float = 0.4,
    ) -> None:
        self._store = store
        self._top_k = top_k
        self._min_confidence = min_confidence

    async def resolve(self, key: str, user_id: str) -> str:
        """Return ``", "``-joined top-k trait values for ``key``.

        Unknown keys, empty ``user_id``, or no traits ⇒ ``""``. We
        never raise from here — a placeholder failing should not
        cascade into a failed prompt render.
        """
        if not user_id:
            return ""
        kind = _KEY_TO_KIND.get(key)
        if kind is None:
            logger.debug(
                "user_model.placeholder.unknown_key", extra={"key": key}
            )
            return ""
        traits = await self._store.list_traits_for_user(
            user_id, kind=kind, min_confidence=self._min_confidence
        )
        if not traits:
            return ""
        top = traits[: self._top_k]
        return ", ".join(t.trait_value for t in top)


__all__ = ["UserModelResolver"]
