"""Candidate memory extractor - SessionBundle -> list[MemoryCandidate].

Phase 4 of the Memory Curator pipeline. Takes a session bundle and
extracts candidate memories worth persisting long-term. Uses an LLM
to identify topics, then structures them as MemoryCandidate instances
with evidence, confidence scores, and kind classification.

Design principles (Karpathy Guidelines):
- Explicit over implicit: every candidate traces back to evidence.
- Simple first: rule-based pre-filter before LLM call.
- Composable: the extractor is a pure function (bundle + config -> candidates).

Provider injection follows the same Protocol pattern as
corlinman-episodes distiller - tests inject a stub, the runner
injects the real LLM adapter.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.models import (
    BundleMessage,
    MemoryCandidate,
    MemoryKind,
    RiskLevel,
    SessionBundle,
)


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class ExtractionProvider(Protocol):
    """Async callable contract for the LLM extraction step.

    The runner injects an implementation backed by the provider
    registry. Tests inject a deterministic stub. Returns raw JSON
    string containing the extracted candidates array.
    """

    async def __call__(self, *, prompt: str) -> str: ...


# Convenience alias
ExtractionFn = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a memory curator for an AI agent system. Your job is to\n"
    "extract knowledge worth remembering long-term from a conversation "
    "transcript.\n"
    "\n"
    "For each piece of knowledge, output a JSON object with these fields:\n"
    "- topic: short title (3-8 words)\n"
    "- kind: one of [project_context, user_preference, agent_persona, "
    "decision, task_state, concept, relationship, conflict]\n"
    "- summary: 1-2 sentence distillation (NOT a copy-paste from the "
    "transcript)\n"
    "- evidence: array of 1-3 short quotes from the transcript that "
    "support this\n"
    "- confidence: float 0.0-1.0 (how certain this is worth remembering)\n"
    "- tags: array of 1-3 relevant tags\n"
    "- discard: boolean (true if this is too ephemeral or context-specific "
    "to remember)\n"
    "- discard_reason: string (why it should not be saved, empty if "
    "discard=false)\n"
    "\n"
    "Rules:\n"
    "1. Do NOT extract trivial greetings or small talk.\n"
    "2. Do NOT copy-paste raw messages as summaries - distill them.\n"
    "3. Prefer fewer high-quality candidates over many low-quality ones.\n"
    "4. Mark as discard=true anything that is only relevant for the "
    "current session.\n"
    "5. A complex session should yield 2-8 candidates, not 20.\n"
    "6. Evidence quotes should be short (under 100 chars each).\n"
    "\n"
    "Output ONLY a JSON array of objects. No markdown fences, no "
    "explanation."
)


def _render_session_prompt(bundle: SessionBundle, *, max_messages: int) -> str:
    """Render a SessionBundle into a user-prompt for the LLM."""
    lines: list[str] = []
    lines.append(f"Session: {bundle.session_id}")
    lines.append(f"Time range: {bundle.started_at_ms} - {bundle.ended_at_ms}")
    lines.append("")
    lines.append("## Transcript")
    lines.append("")

    messages = bundle.messages[:max_messages]
    for msg in messages:
        role_tag = msg.role.upper()
        content_preview = msg.content
        # Truncate very long messages for the prompt
        if len(content_preview) > 2000:
            content_preview = content_preview[:2000] + " [...truncated]"
        lines.append(f"[{role_tag}]: {content_preview}")

    if len(bundle.messages) > max_messages:
        lines.append(
            f"\n[...{len(bundle.messages) - max_messages} more messages truncated]"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pre-filter: skip sessions not worth curating
# ---------------------------------------------------------------------------


def should_skip_session(bundle: SessionBundle, config: CuratorConfig) -> bool:
    """Return True if the session is too short or trivial to curate.

    Checks:
    - Minimum message count.
    - At least one user message with substantive content.
    """
    if len(bundle.messages) < config.min_messages_for_curation:
        return True

    # Must have at least one user message with >20 chars of content
    user_messages = [
        m for m in bundle.messages
        if m.role in ("user", "human") and len(m.content.strip()) > 20
    ]
    if not user_messages:
        return True

    return False


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL,
)


def _parse_extraction_response(
    raw: str,
    *,
    session_id: str,
    tenant_id: str,
    agent_id: str,
) -> list[MemoryCandidate]:
    """Parse the LLM JSON response into MemoryCandidate instances.

    Tolerant of markdown fences and minor formatting issues.
    Returns an empty list on parse failure rather than raising.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Try to find a JSON array
    if not text.startswith("["):
        # Maybe the LLM wrapped it in an object
        bracket_idx = text.find("[")
        if bracket_idx >= 0:
            text = text[bracket_idx:]
        else:
            return []

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []

    if not isinstance(items, list):
        return []

    candidates: list[MemoryCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = _item_to_candidate(
            item,
            session_id=session_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        if candidate is not None:
            candidates.append(candidate)

    return candidates


def _item_to_candidate(
    item: dict[str, Any],
    *,
    session_id: str,
    tenant_id: str,
    agent_id: str,
) -> MemoryCandidate | None:
    """Convert a single parsed JSON item to a MemoryCandidate.

    Returns None if required fields are missing or invalid.
    """
    topic = item.get("topic", "").strip()
    if not topic:
        return None

    # Parse kind with fallback
    kind_raw = item.get("kind", "concept").strip().lower()
    try:
        kind = MemoryKind(kind_raw)
    except ValueError:
        kind = MemoryKind.CONCEPT

    summary = item.get("summary", "").strip()
    if not summary:
        return None

    # Evidence: list of strings
    evidence_raw = item.get("evidence", [])
    if isinstance(evidence_raw, list):
        evidence = [str(e).strip() for e in evidence_raw if str(e).strip()]
    else:
        evidence = []

    # Confidence: clamp to [0, 1]
    confidence_raw = item.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.5

    # Tags
    tags_raw = item.get("tags", [])
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []

    # Discard flag
    discard = bool(item.get("discard", False))
    discard_reason = str(item.get("discard_reason", "")).strip()

    return MemoryCandidate(
        candidate_id=_generate_candidate_id(),
        topic=topic,
        kind=kind,
        summary=summary,
        evidence=evidence,
        confidence=confidence,
        risk=RiskLevel.LOW,  # Risk classification happens in Phase 5
        source_session_id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        tags=tags,
        discard=discard,
        discard_reason=discard_reason,
    )


def _generate_candidate_id() -> str:
    """Generate a unique candidate ID."""
    return f"mc-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------


async def extract_candidates(
    *,
    bundle: SessionBundle,
    config: CuratorConfig,
    provider: ExtractionProvider,
) -> list[MemoryCandidate]:
    """Extract memory candidates from a session bundle.

    Pipeline:
    1. Pre-filter: skip trivial sessions.
    2. Render the session into a prompt.
    3. Call the LLM via the injected provider.
    4. Parse the structured response.
    5. Apply post-extraction filters (max candidates, min confidence).

    Returns an empty list if the session is skipped or extraction fails.
    """
    if should_skip_session(bundle, config):
        return []

    # Render prompt
    user_prompt = _render_session_prompt(
        bundle, max_messages=config.max_messages_per_session
    )
    full_prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{user_prompt}"

    # Call LLM
    raw_response = await provider(prompt=full_prompt)

    # Parse response
    candidates = _parse_extraction_response(
        raw_response,
        session_id=bundle.session_id,
        tenant_id=bundle.tenant_id,
        agent_id=bundle.agent_id,
    )

    # Post-filter: drop below minimum confidence
    candidates = [
        c for c in candidates
        if c.confidence >= config.draft_min_confidence
    ]

    # Cap total candidates to avoid fragmentation explosion
    max_candidates = 8
    if len(candidates) > max_candidates:
        # Keep highest confidence ones
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        candidates = candidates[:max_candidates]

    return candidates


# ---------------------------------------------------------------------------
# Batch extraction (multiple sessions)
# ---------------------------------------------------------------------------


async def extract_candidates_batch(
    *,
    bundles: list[SessionBundle],
    config: CuratorConfig,
    provider: ExtractionProvider,
) -> list[MemoryCandidate]:
    """Extract candidates from multiple session bundles.

    Processes sequentially to respect rate limits. Returns the
    combined candidate list from all sessions.
    """
    all_candidates: list[MemoryCandidate] = []
    for bundle in bundles[: config.max_sessions_per_run]:
        candidates = await extract_candidates(
            bundle=bundle,
            config=config,
            provider=provider,
        )
        all_candidates.extend(candidates)
    return all_candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ExtractionFn",
    "ExtractionProvider",
    "SYSTEM_PROMPT",
    "extract_candidates",
    "extract_candidates_batch",
    "should_skip_session",
]
