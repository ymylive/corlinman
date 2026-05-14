"""Configuration for the Agent Brain Memory Curator.

Mirrors the pattern from ``corlinman_episodes.config`` — a frozen
dataclass with sensible defaults so tests and CLI can construct one
without a TOML file.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Default tenant for single-tenant deployments and tests.
DEFAULT_TENANT_ID = "default"


@dataclass(frozen=True)
class CuratorConfig:
    """Tunables for the memory curator pipeline.

    Frozen so a running curator can stash one and trust it through
    the entire pass.
    """

    enabled: bool = True

    # Write policy: "draft_first" | "semi_auto" | "auto"
    write_policy: str = "semi_auto"

    # Session reader limits
    max_messages_per_session: int = 200
    max_sessions_per_run: int = 10

    # Time window for pulling sessions (hours)
    lookback_hours: float = 24.0

    # Minimum messages to consider a session worth curating
    min_messages_for_curation: int = 4

    # Sanitization
    redact_emails: bool = True
    redact_phone_numbers: bool = True
    redact_api_keys: bool = True

    # LLM provider for extraction
    llm_provider_alias: str = "default-summary"

    # Vault paths
    vault_root: str = "knowledge/agent-brain"

    # Confidence thresholds
    auto_write_min_confidence: float = 0.7
    draft_min_confidence: float = 0.3

    # Risk thresholds for semi_auto policy
    auto_write_max_risk: str = "low"

    # Link planner similarity thresholds
    similarity_threshold_update: float = 0.8
    similarity_threshold_merge: float = 0.6
    similarity_threshold_link: float = 0.4

    # Maximum nodes to retrieve per candidate during link planning
    max_retrieval_results: int = 5


__all__ = ["DEFAULT_TENANT_ID", "CuratorConfig"]
