"""Risk Classifier and Write Policy for the Memory Curator system.

Pure-function module that classifies memory candidates by risk level
and decides write actions based on the active write policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.models import MemoryCandidate, MemoryKind, RiskLevel, WritePolicy

# ---------------------------------------------------------------------------
# Sensitive content detection patterns (compiled at module level)
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    # API keys / tokens
    re.compile(r"(?:sk|pk)[-_](?:live|test|prod)?[-_]?[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    re.compile(r"gho_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"xox[bpas]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}"),
    # Email addresses
    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    # Phone numbers (international formats)
    re.compile(r"(?:\+\d{1,3}[\s\-]?)?\(?\d{2,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}"),
    # Credit card numbers (basic 13-19 digit patterns with optional separators)
    re.compile(r"\b(?:\d[ \-]?){12,18}\d\b"),
    # IP addresses (private ranges)
    re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})\b"
    ),
    # URLs with tokens/passwords in query params
    re.compile(
        r"https?://[^\s]*[?&](?:token|password|secret|api_key|apikey|access_token|auth)=[^\s&]+",
        re.IGNORECASE,
    ),
    # Common secret variable names with values
    re.compile(
        r"(?:password|passwd|secret|api_key|apikey|api_secret|access_token|auth_token|private_key)"
        r"\s*[=:]\s*[\"']?[^\s\"',;]{4,}",
        re.IGNORECASE,
    ),
]


# ---------------------------------------------------------------------------
# WriteDecision dataclass
# ---------------------------------------------------------------------------


@dataclass
class WriteDecision:
    """Result of a write-policy evaluation for a memory candidate."""

    action: str  # "auto_write" | "draft" | "block"
    reason: str
    risk: RiskLevel


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


def _contains_sensitive_content(text: str) -> bool:
    """Check if text matches any sensitive content pattern."""
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def classify_risk(candidate: MemoryCandidate, config: CuratorConfig) -> RiskLevel:
    """Classify the risk level of a memory candidate.

    Checks evidence and summary against sensitive patterns, candidate kind,
    and confidence thresholds to determine risk.

    Args:
        candidate: The memory candidate to classify.
        config: Curator configuration with threshold values.

    Returns:
        The determined RiskLevel.
    """
    # Check evidence and summary for sensitive content -> HIGH
    texts_to_check = [candidate.summary, *candidate.evidence]
    for text in texts_to_check:
        if _contains_sensitive_content(text):
            return RiskLevel.HIGH

    # Conflict kind -> HIGH
    if candidate.kind == MemoryKind.CONFLICT:
        return RiskLevel.HIGH

    # Low confidence below draft threshold -> MEDIUM
    if candidate.confidence < config.draft_min_confidence:
        return RiskLevel.MEDIUM

    # Unconfirmed persona inference -> MEDIUM
    if candidate.kind == MemoryKind.AGENT_PERSONA and candidate.confidence < 0.7:
        return RiskLevel.MEDIUM

    return RiskLevel.LOW


# ---------------------------------------------------------------------------
# Batch classification
# ---------------------------------------------------------------------------


def classify_risk_batch(
    candidates: list[MemoryCandidate], config: CuratorConfig
) -> list[MemoryCandidate]:
    """Classify risk for a batch of candidates, mutating in-place.

    Args:
        candidates: List of memory candidates to classify.
        config: Curator configuration with threshold values.

    Returns:
        The same list with each candidate's risk field updated.
    """
    for candidate in candidates:
        candidate.risk = classify_risk(candidate, config)
    return candidates


# ---------------------------------------------------------------------------
# Write policy decision
# ---------------------------------------------------------------------------


def decide_write_action(
    candidate: MemoryCandidate,
    policy: WritePolicy,
    config: CuratorConfig,
) -> WriteDecision:
    """Decide the write action for a candidate based on policy and risk.

    Args:
        candidate: The memory candidate (should already have risk classified).
        policy: The active write policy.
        config: Curator configuration.

    Returns:
        A WriteDecision with action, reason, and risk.
    """
    risk = candidate.risk

    # BLOCKED risk always blocks regardless of policy
    if risk == RiskLevel.BLOCKED:
        return WriteDecision(
            action="block",
            reason="Candidate risk is BLOCKED; cannot be written.",
            risk=risk,
        )

    # DRAFT_FIRST: always draft
    if policy == WritePolicy.DRAFT_FIRST:
        return WriteDecision(
            action="draft",
            reason="Policy is DRAFT_FIRST; all candidates go to draft.",
            risk=risk,
        )

    # AUTO: always auto_write (blocked already handled above)
    if policy == WritePolicy.AUTO:
        return WriteDecision(
            action="auto_write",
            reason="Policy is AUTO; writing automatically.",
            risk=risk,
        )

    # SEMI_AUTO: decision based on risk and confidence
    if policy == WritePolicy.SEMI_AUTO:
        if risk == RiskLevel.LOW:
            if candidate.confidence >= 0.6:
                return WriteDecision(
                    action="auto_write",
                    reason=f"Low risk with sufficient confidence ({candidate.confidence:.2f} >= 0.60).",
                    risk=risk,
                )
            else:
                return WriteDecision(
                    action="draft",
                    reason=f"Low risk but confidence too low ({candidate.confidence:.2f} < 0.60); drafting.",
                    risk=risk,
                )
        elif risk == RiskLevel.MEDIUM:
            return WriteDecision(
                action="draft",
                reason="Medium risk; requires review before writing.",
                risk=risk,
            )
        elif risk == RiskLevel.HIGH:
            return WriteDecision(
                action="draft",
                reason="High risk detected; drafting with warning for manual review.",
                risk=risk,
            )

    # Fallback: conservative default
    return WriteDecision(
        action="draft",
        reason="Fallback: unknown policy or state; defaulting to draft.",
        risk=risk,
    )


__all__ = [
    "WriteDecision",
    "classify_risk",
    "classify_risk_batch",
    "decide_write_action",
]
