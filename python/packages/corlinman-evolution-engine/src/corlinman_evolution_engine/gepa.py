"""GEPA-lite — deterministic Pareto scoring for prompt-template variants.

Inspired by Nous Research's Hermes Agent self-evolution stack (DSPy +
GEPA — Genetic-Pareto Prompt Evolution). The published GEPA optimises
prompts by genetic mutation under an LLM-judge reward; we don't want
the LLM-judge dependency in v0.7.0 because (a) it costs tokens on every
evolution run, (b) the judge becomes a moving target between provider
upgrades, and (c) our deployment story includes air-gapped operators.

The "lite" version:

1. **Inputs.** A list of candidate variants for one prompt-template
   segment (operator-supplied for v0.7.0; the v0.8 conversation is
   auto-generating them) plus a sample of historical episodes (each
   carrying its system prompt at the time + the success outcome).
2. **Scoring.** Each variant is scored on two axes:
   - ``success_overlap`` — token Jaccard between the variant and the
     *successful* episodes' prompts. Captures "does this variant look
     like the prompts that worked?"
   - ``token_cost`` — variant length in whitespace-separated tokens.
     Captures "is this variant cheap to evaluate at inference time?"
3. **Pareto frontier.** A variant survives if no other variant
   strictly dominates it (higher success_overlap AND lower token_cost).
   The frontier becomes the operator's short-list.

No LLM calls, no provider dependency, no training. Pure deterministic
scoring on data already in ``episodes.sqlite``. The operator still
makes the final pick from the frontier — this module narrows their
choice, it doesn't usurp it.

The module is intentionally pure (functions over plain dataclasses) so
it composes with the ShadowTester for downstream verification: the
Pareto frontier feeds shortlisted variants into ``corlinman-shadow-
tester`` which replays them against a small fixture set to surface
regressions before the proposal reaches the operator queue.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EpisodeSample:
    """One historical episode the scorer sees. Frozen so the same
    episode can be reused across many variant evaluations without
    fearing accidental mutation.

    ``prompt_text`` is the system-prompt-at-the-time the episode used.
    ``succeeded`` is the binary outcome (operator thumbs / quality
    score above floor / etc — the evolution observer is the source of
    truth for what "succeeded" means in any given deployment).
    """

    prompt_text: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class VariantScore:
    """Per-variant score record. ``index`` is the variant's position
    in the input list (preserved through scoring so the operator UI
    can highlight "variant #2 survived")."""

    index: int
    text: str
    success_overlap: float
    """Average token-Jaccard against successful episode prompts. 0..1."""
    token_cost: int
    """Variant length in whitespace-separated tokens."""
    on_frontier: bool
    """``True`` iff no other variant strictly dominates this one."""


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenise(text: str) -> frozenset[str]:
    """Lower-cased word-token set. ``frozenset`` so the Jaccard
    intersection is O(min(|a|, |b|)) and we can cache by id."""
    return frozenset(m.lower() for m in _TOKEN_RE.findall(text))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def score_variants(
    variants: Sequence[str],
    episodes: Iterable[EpisodeSample],
) -> list[VariantScore]:
    """Score each variant against the historical episodes; return
    one :class:`VariantScore` per input variant in input order.

    A variant with zero successful episodes to compare against scores
    ``success_overlap = 0.0`` — the operator should see this as "no
    historical evidence" rather than a misleading "perfect overlap"
    or a hard error. The frontier flag still applies.

    Parameters
    ----------
    variants
        Candidate prompt-template texts. Order is preserved in the
        output; an empty list returns an empty result.
    episodes
        Historical episodes. May be a generator — we consume it once
        and discard. Episodes with ``succeeded=False`` are still
        loaded (cheap) but only successful episodes contribute to the
        score.

    Notes
    -----
    The dual-axis Pareto check is strict: variant ``a`` dominates
    variant ``b`` iff ``a.success_overlap > b.success_overlap`` AND
    ``a.token_cost < b.token_cost`` (both strict). Equal scores never
    dominate each other — they coexist on the frontier. This matches
    the published GEPA paper's tie-handling and means a variant only
    drops off the frontier when there's an unambiguous winner.
    """
    materialised: list[str] = list(variants)
    if not materialised:
        return []

    successful_tokens: list[frozenset[str]] = [
        _tokenise(ep.prompt_text) for ep in episodes if ep.succeeded
    ]

    raw: list[tuple[int, str, float, int]] = []
    for i, text in enumerate(materialised):
        tok = _tokenise(text)
        if successful_tokens:
            overlap = sum(_jaccard(tok, s) for s in successful_tokens) / len(
                successful_tokens
            )
        else:
            # No historical signal — flag as "no evidence" rather than
            # bias the operator toward whichever variant is shortest.
            overlap = 0.0
        cost = len(text.split())
        raw.append((i, text, overlap, cost))

    frontier = _pareto_frontier(raw)
    return [
        VariantScore(
            index=i,
            text=text,
            success_overlap=overlap,
            token_cost=cost,
            on_frontier=i in frontier,
        )
        for (i, text, overlap, cost) in raw
    ]


def _pareto_frontier(
    raw: Sequence[tuple[int, str, float, int]],
) -> frozenset[int]:
    """Return the indices that survive strict 2D Pareto dominance.

    ``raw`` carries ``(index, text, success_overlap, token_cost)``. The
    scorer maximises ``success_overlap`` and minimises ``token_cost``.
    Strict dominance: a beats b iff ``a.overlap > b.overlap`` AND
    ``a.cost < b.cost``. Equal-on-both pairs both survive.
    """
    survivors: set[int] = set()
    for i, _, oi, ci in raw:
        dominated = False
        for j, _, oj, cj in raw:
            if i == j:
                continue
            if oj > oi and cj < ci:
                dominated = True
                break
        if not dominated:
            survivors.add(i)
    return frozenset(survivors)


__all__ = [
    "EpisodeSample",
    "VariantScore",
    "score_variants",
]
