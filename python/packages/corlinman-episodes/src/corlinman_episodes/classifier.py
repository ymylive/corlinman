"""Pure heuristic ``EpisodeKind`` picker for a :class:`SourceBundle`.

The rule precedence is taken verbatim from
``docs/design/phase4-w4-d1-design.md`` §Distillation job step 2:

1. Any ``auto_rollback_fired`` hook event in the bundle → ``INCIDENT``.
2. Otherwise any history apply row → ``EVOLUTION``.
3. Otherwise any operator hook (``tool_approved`` / ``tool_denied`` /
   ``evolution_applied`` triggered by an operator) → ``OPERATOR``.
4. Otherwise the per-user "first N sessions" rule → ``ONBOARDING``.
5. Otherwise → ``CONVERSATION``.

The classifier is deliberately a pure function: same bundle → same
kind. The runner relies on that for the iter-2 natural-key probe
(if a crashed re-run reproduces the same bundle, the kind must match
or the second insert will succeed and double-mint).

Kept independent of the importance scorer — kind selection is
discrete; importance is continuous. They share inputs but their
weights drift independently.
"""

from __future__ import annotations

from corlinman_episodes.sources import SourceBundle
from corlinman_episodes.store import EpisodeKind

# Hook ``kind`` values that count as "operator action present" when
# upstream provenance isn't carried on the row. The design lists these
# under §"What gets distilled" item 4 — keep the set narrow so a
# system-emitted ``evolution_applied`` for an auto-applied skill_update
# doesn't false-positive into ``OPERATOR``.
_OPERATOR_HOOK_KINDS: frozenset[str] = frozenset(
    {"tool_approved", "tool_denied"}
)


def classify(
    bundle: SourceBundle,
    *,
    is_onboarding: bool = False,
) -> EpisodeKind:
    """Return the :class:`EpisodeKind` the runner should distill under.

    ``is_onboarding`` is computed by the runner from the
    ``ONBOARDING_FIRST_N`` config (per-user-id session count); the
    classifier doesn't read sessions DB on its own. Defaults False so
    the test matrix can pin the rule precedence without a runner.
    """
    # 1. Incident — any auto-rollback fire dominates the bundle.
    if any(h.kind == "auto_rollback_fired" for h in bundle.hooks):
        return EpisodeKind.INCIDENT

    # 2. Evolution — at least one history apply row in-window.
    if bundle.history:
        return EpisodeKind.EVOLUTION

    # 3. Operator — explicit approve / deny in hook events.
    if any(h.kind in _OPERATOR_HOOK_KINDS for h in bundle.hooks):
        return EpisodeKind.OPERATOR

    # 4. Onboarding — runner-supplied flag.
    if is_onboarding:
        return EpisodeKind.ONBOARDING

    # 5. Default — plain conversation.
    return EpisodeKind.CONVERSATION


__all__ = ["classify"]
