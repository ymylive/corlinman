"""Tests for :class:`UserCorrectionApplier`.

Covers the routing contract documented on the class:

* USER_CORRECTION signal → :class:`BackgroundReviewReport` (0 writes
  when the provider returns no tool_calls).
* Low-weight signals → ``None``.
* Rate-limit per ``(profile, session)``.
* Non-USER_CORRECTION signal → ``None`` (defensive guard).
* Provider exceptions inside :func:`spawn_background_review` → the
  applier still returns a report (because spawn_background_review never
  raises by contract).
* Resolver exceptions → ``None`` (the applier short-circuits without
  ever calling the spawner).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from corlinman_evolution_store import EVENT_USER_CORRECTION, EvolutionSignal, SignalSeverity
from corlinman_providers.mock import MockProvider
from corlinman_server.gateway.evolution.applier_user_correction import (
    UserCorrectionApplier,
)
from corlinman_server.gateway.evolution.background_review import (
    BackgroundReviewReport,
)
from corlinman_skills_registry import SkillRegistry


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    """Empty profile root with skills/ subdir."""
    root = tmp_path / "profiles" / "alice"
    (root / "skills").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def registry(profile_root: Path) -> SkillRegistry:
    return SkillRegistry.load_from_dir(profile_root / "skills")


@pytest.fixture
def base_signal() -> EvolutionSignal:
    """A canonical USER_CORRECTION signal at weight 0.85."""
    return EvolutionSignal(
        id=42,
        event_kind=EVENT_USER_CORRECTION,
        severity=SignalSeverity.INFO,
        payload_json={
            "text": "Stop using bullet points please",
            "matched_pattern": r"\bstop\b",
            "kind": "imperative",
            "weight": 0.85,
            "snippet": "stop",
        },
        observed_at=1_700_000_000_000,
        target="alice",
        session_id="sess-123",
        tenant_id="default",
    )


def _make_applier(
    *,
    profile_root: Path,
    registry: SkillRegistry,
    provider: Any | None = None,
    model: str = "mock",
    min_weight: float = 0.7,
    rate_limit_seconds: int = 30,
    spawn_fn=None,
    now_fn=None,
    registry_raises: bool = False,
    profile_root_raises: bool = False,
    provider_raises: bool = False,
) -> UserCorrectionApplier:
    """Build a UserCorrectionApplier with deterministic resolvers."""

    def _registry(slug: str) -> SkillRegistry:
        if registry_raises:
            raise RuntimeError("registry boom")
        return registry

    def _root(slug: str) -> Path:
        if profile_root_raises:
            raise RuntimeError("root boom")
        return profile_root

    def _provider(slug: str) -> tuple[Any, str]:
        if provider_raises:
            raise RuntimeError("provider boom")
        return (provider or MockProvider(), model)

    return UserCorrectionApplier(
        registry_for_profile=_registry,
        profile_root_for_profile=_root,
        provider_for_profile=_provider,
        min_weight=min_weight,
        rate_limit_seconds=rate_limit_seconds,
        spawn_fn=spawn_fn,
        now_fn=now_fn,
    )


# ─── Happy path ──────────────────────────────────────────────────────


async def test_apply_returns_report_for_user_correction_signal(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """Mock provider yields no tool_calls → empty report, no error."""
    applier = _make_applier(profile_root=profile_root, registry=registry)
    report = await applier.apply(base_signal)
    assert isinstance(report, BackgroundReviewReport)
    assert report.error is None
    assert report.writes == []
    assert report.applied_count == 0
    assert report.profile_slug == "alice"
    assert report.kind == "user-correction"


async def test_apply_uses_target_as_profile_slug(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """``signal.target`` is preferred over ``signal.tenant_id``."""
    base_signal.target = "alice"
    base_signal.tenant_id = "different"
    applier = _make_applier(profile_root=profile_root, registry=registry)
    report = await applier.apply(base_signal)
    assert report is not None
    assert report.profile_slug == "alice"


async def test_apply_falls_back_to_tenant_when_target_missing(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    base_signal.target = None
    base_signal.tenant_id = "fallback-tenant"
    applier = _make_applier(profile_root=profile_root, registry=registry)
    report = await applier.apply(base_signal)
    assert report is not None
    assert report.profile_slug == "fallback-tenant"


# ─── Weight gate ─────────────────────────────────────────────────────


async def test_low_weight_signal_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """Reformulation-tier weight (0.55) is below the default 0.7 floor."""
    base_signal.payload_json = dict(base_signal.payload_json)
    base_signal.payload_json["weight"] = 0.55
    applier = _make_applier(profile_root=profile_root, registry=registry)
    assert await applier.apply(base_signal) is None


async def test_custom_min_weight_threshold(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """Lowering ``min_weight`` lets weaker signals through."""
    base_signal.payload_json = dict(base_signal.payload_json)
    base_signal.payload_json["weight"] = 0.55
    applier = _make_applier(profile_root=profile_root, registry=registry, min_weight=0.5)
    report = await applier.apply(base_signal)
    assert report is not None


async def test_missing_weight_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    base_signal.payload_json = {"text": "Stop"}
    applier = _make_applier(profile_root=profile_root, registry=registry)
    assert await applier.apply(base_signal) is None


async def test_non_dict_payload_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    base_signal.payload_json = "not a dict"  # type: ignore[assignment]
    applier = _make_applier(profile_root=profile_root, registry=registry)
    assert await applier.apply(base_signal) is None


# ─── Rate-limit ──────────────────────────────────────────────────────


async def test_rate_limit_suppresses_second_call(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """Two rapid signals in the same (profile, session) → second is None."""
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    times = iter([now, now + timedelta(seconds=1)])

    def _now() -> datetime:
        return next(times)

    applier = _make_applier(
        profile_root=profile_root,
        registry=registry,
        rate_limit_seconds=30,
        now_fn=_now,
    )
    r1 = await applier.apply(base_signal)
    r2 = await applier.apply(base_signal)
    assert r1 is not None
    assert r2 is None


async def test_rate_limit_does_not_cross_sessions(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """A signal from a different session is not gated by the prior."""
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    times = iter([now, now])

    def _now() -> datetime:
        return next(times)

    applier = _make_applier(
        profile_root=profile_root,
        registry=registry,
        rate_limit_seconds=30,
        now_fn=_now,
    )
    sig_a = base_signal
    sig_b = EvolutionSignal(
        event_kind=sig_a.event_kind,
        severity=sig_a.severity,
        payload_json=dict(sig_a.payload_json),
        observed_at=sig_a.observed_at,
        target=sig_a.target,
        session_id="sess-OTHER",
        tenant_id=sig_a.tenant_id,
    )
    r1 = await applier.apply(sig_a)
    r2 = await applier.apply(sig_b)
    assert r1 is not None
    assert r2 is not None


async def test_rate_limit_window_expiry(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """After ``rate_limit_seconds`` have passed, a follow-up fires."""
    base_time = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    times = iter([base_time, base_time + timedelta(seconds=120)])

    def _now() -> datetime:
        return next(times)

    applier = _make_applier(
        profile_root=profile_root,
        registry=registry,
        rate_limit_seconds=30,
        now_fn=_now,
    )
    r1 = await applier.apply(base_signal)
    r2 = await applier.apply(base_signal)
    assert r1 is not None
    assert r2 is not None


async def test_null_session_id_falls_back_to_global_bucket(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """Two signals with ``session_id=None`` share the ``"global"`` bucket."""
    base_signal.session_id = None
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    times = iter([now, now + timedelta(seconds=1)])

    def _now() -> datetime:
        return next(times)

    applier = _make_applier(
        profile_root=profile_root,
        registry=registry,
        rate_limit_seconds=30,
        now_fn=_now,
    )
    r1 = await applier.apply(base_signal)
    r2 = await applier.apply(base_signal)
    assert r1 is not None
    assert r2 is None


# ─── Defensive gates ─────────────────────────────────────────────────


async def test_non_user_correction_signal_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """Belt-and-braces guard — the listener should never feed us a
    non-USER_CORRECTION signal but we defend anyway.
    """
    base_signal.event_kind = "tool.call.failed"
    applier = _make_applier(profile_root=profile_root, registry=registry)
    assert await applier.apply(base_signal) is None


async def test_missing_profile_slug_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    base_signal.target = None
    base_signal.tenant_id = ""
    applier = _make_applier(profile_root=profile_root, registry=registry)
    assert await applier.apply(base_signal) is None


async def test_registry_resolver_failure_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    applier = _make_applier(
        profile_root=profile_root, registry=registry, registry_raises=True
    )
    assert await applier.apply(base_signal) is None


async def test_profile_root_resolver_failure_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    applier = _make_applier(
        profile_root=profile_root, registry=registry, profile_root_raises=True
    )
    assert await applier.apply(base_signal) is None


async def test_provider_resolver_failure_returns_none(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    applier = _make_applier(
        profile_root=profile_root, registry=registry, provider_raises=True
    )
    assert await applier.apply(base_signal) is None


# ─── Provider behaviour through spawn_background_review ──────────────


async def test_spawn_exception_returns_none_without_raising(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """If the injected spawner itself raises (which the production
    function would not — it's exception-safe — but a future refactor
    could regress), the applier swallows it and returns ``None``.
    """

    async def _bad_spawn(**kwargs: Any) -> BackgroundReviewReport:
        raise RuntimeError("spawn exploded")

    applier = _make_applier(
        profile_root=profile_root, registry=registry, spawn_fn=_bad_spawn
    )
    assert await applier.apply(base_signal) is None


async def test_provider_failure_surfaces_in_report_error(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """A raising provider routed through real ``spawn_background_review``
    surfaces as a report whose ``error`` field is populated — the
    applier itself never raises.
    """

    class RaisingProvider:
        async def chat(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("upstream is down")

    applier = _make_applier(
        profile_root=profile_root,
        registry=registry,
        provider=RaisingProvider(),
    )
    report = await applier.apply(base_signal)
    assert isinstance(report, BackgroundReviewReport)
    assert report.error is not None
    assert "upstream is down" in report.error


async def test_spawn_invoked_with_user_correction_kind(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """The spawner is called with ``kind="user-correction"`` and the
    detector's text is threaded through as ``user_correction_text``.
    """
    captured: dict[str, Any] = {}

    async def _capture_spawn(**kwargs: Any) -> BackgroundReviewReport:
        captured.update(kwargs)
        return BackgroundReviewReport(
            profile_slug=kwargs["profile_slug"],
            kind="user-correction",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            writes=[],
            error=None,
        )

    applier = _make_applier(
        profile_root=profile_root, registry=registry, spawn_fn=_capture_spawn
    )
    await applier.apply(base_signal)
    assert captured["kind"] == "user-correction"
    assert captured["profile_slug"] == "alice"
    assert captured["user_correction_text"] == "Stop using bullet points please"
    assert captured["recent_messages"] == []
    assert isinstance(captured["profile_root"], Path)
    assert captured["registry"] is registry
    assert captured["model"] == "mock"


# ─── Counted-call sanity ─────────────────────────────────────────────


async def test_rate_limit_marked_even_on_spawn_failure(
    profile_root: Path,
    registry: SkillRegistry,
    base_signal: EvolutionSignal,
) -> None:
    """An exception inside the spawner still marks the rate-limit so
    the burst of corrective messages doesn't retry the LLM repeatedly.
    """

    async def _bad_spawn(**kwargs: Any) -> BackgroundReviewReport:
        raise RuntimeError("spawn exploded")

    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    times = iter([now, now + timedelta(seconds=1)])

    def _now() -> datetime:
        return next(times)

    applier = _make_applier(
        profile_root=profile_root,
        registry=registry,
        spawn_fn=_bad_spawn,
        now_fn=_now,
        rate_limit_seconds=30,
    )
    r1 = await applier.apply(base_signal)
    r2 = await applier.apply(base_signal)
    assert r1 is None  # spawner failure
    assert r2 is None  # rate-limited
