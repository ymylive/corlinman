"""Configuration dataclass for the episodic-memory subsystem.

Mirrors the ``[episodes]`` TOML block documented in
``docs/design/phase4-w4-d1-design.md`` §Configuration. Defaults match
Wave 4's "ship a working surface; tune later" stance — the only knob
that needs operator attention out of the box is
``llm_provider_alias`` (the spec ships ``default-summary`` pointing at
the same provider as the prompt-template handler so a fresh install
works without a custom ``episodes.toml``).

Every field has a default so callers can construct
``EpisodesConfig()`` and exercise the runner against a synthetic DB
without supplying a TOML at all — heavily used in tests.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Default ``tenant_id`` used by single-tenant call sites and tests.
#: Mirrors ``corlinman_persona.store.DEFAULT_TENANT_ID`` so the two
#: per-tenant SQLite stores quote the same legacy bucket.
DEFAULT_TENANT_ID = "default"


@dataclass(frozen=True)
class EpisodesConfig:
    """Tunables for the episodic-memory distillation pass.

    Frozen so a runner can stash one off and trust it through the whole
    job. ``enabled=False`` short-circuits the run with a structured
    skip-summary, same pattern as ``ConsolidationConfig`` in
    ``corlinman-evolution-engine``.
    """

    enabled: bool = True
    schedule: str = "0 6 * * * *"
    distillation_window_hours: float = 24.0
    min_session_count_per_episode: int = 1
    min_window_secs: int = 3600
    max_messages_per_call: int = 60
    llm_provider_alias: str = "default-summary"
    embedding_provider_alias: str = "small"
    max_episodes_per_query: int = 5
    last_week_top_n: int = 5
    cold_archive_days: int = 180
    run_stale_after_secs: int = 1800


__all__ = ["DEFAULT_TENANT_ID", "EpisodesConfig"]
