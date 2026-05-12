"""Tests for corlinman_agent_brain.extractor module.

Covers:
- should_skip_session: minimum messages, substantive user content
- _parse_extraction_response: valid JSON, markdown fences, malformed input
- extract_candidates: full pipeline with mock provider
- extract_candidates_batch: multiple bundles, max_sessions_per_run cap
- Post-extraction filtering (confidence threshold, max candidates cap)
"""

from __future__ import annotations

import json

import pytest

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.extractor import (
    _parse_extraction_response,
    extract_candidates,
    extract_candidates_batch,
    should_skip_session,
)
from corlinman_agent_brain.models import (
    BundleMessage,
    MemoryKind,
    RiskLevel,
    SessionBundle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> CuratorConfig:
    return CuratorConfig()


@pytest.fixture
def valid_bundle() -> SessionBundle:
    """A session bundle with enough messages to pass pre-filter."""
    return SessionBundle(
        session_id="sess-001",
        tenant_id="tenant-a",
        user_id="user-1",
        agent_id="agent-x",
        messages=[
            BundleMessage(
                seq=1, role="user",
                content="I want to set up a CI/CD pipeline for our project.",
                ts_ms=1000,
            ),
            BundleMessage(
                seq=2, role="assistant",
                content="Sure, I can help with that. What CI system do you prefer?",
                ts_ms=2000,
            ),
            BundleMessage(
                seq=3, role="user",
                content="Let's use GitHub Actions with Docker containers.",
                ts_ms=3000,
            ),
            BundleMessage(
                seq=4, role="assistant",
                content="Great choice. I'll set up the workflow file.",
                ts_ms=4000,
            ),
        ],
        started_at_ms=1000,
        ended_at_ms=4000,
    )


@pytest.fixture
def short_bundle() -> SessionBundle:
    """A session bundle too short to curate (below min_messages_for_curation)."""
    return SessionBundle(
        session_id="sess-short",
        tenant_id="tenant-a",
        user_id="user-1",
        agent_id="agent-x",
        messages=[
            BundleMessage(
                seq=1,
                role="user",
                content="Hi",
                ts_ms=1000),
            BundleMessage(
                seq=2,
                role="assistant",
                content="Hello!",
                ts_ms=2000),
        ],
        started_at_ms=1000,
        ended_at_ms=2000,
    )


@pytest.fixture
def trivial_bundle() -> SessionBundle:
    """A bundle with enough messages but no substantive user content."""
    return SessionBundle(
        session_id="sess-trivial",
        tenant_id="tenant-a",
        user_id="user-1",
        agent_id="agent-x",
        messages=[
            BundleMessage(
                seq=1,
                role="user",
                content="ok",
                ts_ms=1000),
            BundleMessage(
                seq=2,
                role="assistant",
                content="Anything else?",
                ts_ms=2000),
            BundleMessage(
                seq=3,
                role="user",
                content="no",
                ts_ms=3000),
            BundleMessage(
                seq=4,
                role="assistant",
                content="Alright!",
                ts_ms=4000),
        ],
        started_at_ms=1000,
        ended_at_ms=4000,
    )


def _make_provider_response(candidates: list[dict]) -> str:
    """Helper to create a JSON response string."""
    return json.dumps(candidates)


async def _stub_provider(*, prompt: str) -> str:
    """Default stub provider returning a single valid candidate."""
    return json.dumps([
        {
            "topic": "CI/CD Pipeline Setup",
            "kind": "decision",
            "summary": "Team decided to use GitHub Actions with Docker for CI/CD.",
            "evidence": ["user said: Let's use GitHub Actions with Docker containers."],
            "confidence": 0.85,
            "tags": ["ci-cd", "github-actions", "docker"],
            "discard": False,
            "discard_reason": "",
        }
    ])


async def _empty_provider(*, prompt: str) -> str:
    """Provider that returns an empty array."""
    return "[]"


async def _malformed_provider(*, prompt: str) -> str:
    """Provider that returns invalid JSON."""
    return "This is not JSON at all"


async def _fenced_provider(*, prompt: str) -> str:
    """Provider that returns JSON wrapped in markdown fences."""
    candidates = [
        {
            "topic": "Use Pytest",
            "kind": "decision",
            "summary": "Team chose pytest as the test framework.",
            "evidence": ["user: let's use pytest"],
            "confidence": 0.9,
            "tags": ["testing"],
            "discard": False,
            "discard_reason": "",
        }
    ]
    return f"```json\n{json.dumps(candidates)}\n```"


async def _low_confidence_provider(*, prompt: str) -> str:
    """Provider returning candidates below draft_min_confidence."""
    return json.dumps([
        {
            "topic": "Maybe something",
            "kind": "concept",
            "summary": "Vague idea mentioned in passing.",
            "evidence": ["user hinted at something"],
            "confidence": 0.1,
            "tags": ["vague"],
            "discard": False,
            "discard_reason": "",
        }
    ])


async def _many_candidates_provider(*, prompt: str) -> str:
    """Provider returning more than 8 candidates."""
    candidates = []
    for i in range(12):
        candidates.append({
            "topic": f"Topic {i}",
            "kind": "concept",
            "summary": f"Summary for topic {i}.",
            "evidence": [f"evidence {i}"],
            "confidence": 0.5 + (i * 0.04),
            "tags": [f"tag-{i}"],
            "discard": False,
            "discard_reason": "",
        })
    return json.dumps(candidates)
