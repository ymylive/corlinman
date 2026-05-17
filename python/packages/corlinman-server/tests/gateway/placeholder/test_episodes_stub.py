"""Tests for :mod:`corlinman_server.gateway.placeholder.episodes_stub`.

Mirrors the Rust ``corlinman_gateway::placeholder::episodes::tests``
suite. Seeds an ``episodes.sqlite`` file per tenant at the per-tenant
path the resolver expects, then drives the read path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from corlinman_server.gateway.placeholder.episodes_stub import (
    DEFAULT_TENANT_SLUG,
    DEFAULT_TOP_N,
    SUMMARY_CHAR_CAP,
    TENANT_METADATA_KEY,
    VALID_KINDS,
    EpisodeBrief,
    EpisodesResolver,
    _literal_token,
    _parse_token,
    _render_bullets,
    _truncate_summary,
)
from corlinman_server.tenancy import TenantId, tenant_db_path

# Match iter 1 of ``corlinman_episodes.store.SCHEMA_SQL``.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    summary_text        TEXT NOT NULL,
    source_session_keys TEXT NOT NULL DEFAULT '[]',
    source_signal_ids   TEXT NOT NULL DEFAULT '[]',
    source_history_ids  TEXT NOT NULL DEFAULT '[]',
    embedding           BLOB,
    embedding_dim       INTEGER,
    importance_score    REAL NOT NULL DEFAULT 0.5,
    last_referenced_at  INTEGER,
    distilled_by        TEXT NOT NULL,
    distilled_at        INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1
);
"""


async def _open_tenant(root: Path, tenant: str) -> aiosqlite.Connection:
    tid = TenantId.new(tenant)
    path = tenant_db_path(root, tid, "episodes")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path), isolation_level=None)
    await conn.executescript(SCHEMA_SQL)
    return conn


async def _insert_row(
    conn: aiosqlite.Connection,
    *,
    episode_id: str,
    tenant: str,
    kind: str,
    ended_at: int,
    importance: float,
    summary: str,
) -> None:
    await conn.execute(
        """INSERT INTO episodes
             (id, tenant_id, started_at, ended_at, kind, summary_text,
              source_session_keys, source_signal_ids, source_history_ids,
              importance_score, distilled_by, distilled_at, schema_version)
           VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', '[]', ?, 'stub', 0, 1)""",
        (
            episode_id,
            tenant,
            ended_at - 1000,
            ended_at,
            kind,
            summary,
            importance,
        ),
    )


def _ctx(tenant: str) -> SimpleNamespace:
    return SimpleNamespace(metadata={TENANT_METADATA_KEY: tenant})


# ---- token parser --------------------------------------------------------


def test_parse_token_recognises_windows() -> None:
    assert _parse_token("last_24h").window_seconds == 24 * 3600  # type: ignore[union-attr]
    assert _parse_token("last_week").window_seconds == 7 * 24 * 3600  # type: ignore[union-attr]
    assert _parse_token("last_month").window_seconds == 30 * 24 * 3600  # type: ignore[union-attr]


def test_parse_token_recognises_kind_call() -> None:
    for kind in VALID_KINDS:
        parsed = _parse_token(f"kind({kind})")
        assert parsed is not None
        assert parsed.kind == "kind"
        assert parsed.value == kind


def test_parse_token_rejects_unknown_kind() -> None:
    assert _parse_token("kind(banana)") is None


def test_parse_token_recognises_about_id() -> None:
    parsed = _parse_token("about_id(01HXAB)")
    assert parsed is not None
    assert parsed.kind == "about_id"
    assert parsed.value == "01HXAB"


def test_parse_token_unknown_round_trips() -> None:
    assert _parse_token("gibberish") is None
    assert _literal_token("gibberish") == "{{episodes.gibberish}}"


def test_render_bullets_handles_empty_set() -> None:
    assert _render_bullets([]) == ""


def test_long_summary_truncates_with_ellipsis() -> None:
    long = "x" * (SUMMARY_CHAR_CAP + 50)
    out = _truncate_summary(long)
    assert len(out) == SUMMARY_CHAR_CAP + 1
    assert out.endswith("…")


# ---- DB-backed query coverage --------------------------------------------


async def test_last_week_returns_top_by_importance(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    try:
        now = 1_700_000_000_000
        for idx, score in enumerate([0.1, 0.9, 0.4, 0.8, 0.2, 0.99, 0.5]):
            await _insert_row(
                conn,
                episode_id=f"ep-{idx}",
                tenant="default",
                kind="conversation",
                ended_at=now - 1000,
                importance=score,
                summary=f"episode {idx} score {score}",
            )
        # Out-of-window high-score row must not surface.
        await _insert_row(
            conn,
            episode_id="ep-old",
            tenant="default",
            kind="conversation",
            ended_at=now - 30 * 86_400 * 1000,
            importance=1.0,
            summary="ancient",
        )
    finally:
        await conn.close()

    resolver = EpisodesResolver(tmp_path).with_fixed_now_ms(now)
    try:
        out = await resolver.resolve("last_week", ctx=_ctx("default"))
    finally:
        await resolver.close()

    bullets = out.splitlines()
    assert len(bullets) == DEFAULT_TOP_N
    # Top 5 by importance: 0.99, 0.9, 0.8, 0.5, 0.4.
    assert "episode 5 score 0.99" in bullets[0]
    assert "episode 1 score 0.9" in bullets[1]
    assert "episode 3 score 0.8" in bullets[2]
    assert "ancient" not in out


async def test_last_24h_excludes_older_rows(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    try:
        now = 1_700_000_000_000
        await _insert_row(
            conn,
            episode_id="fresh",
            tenant="default",
            kind="conversation",
            ended_at=now - 3600 * 1000,
            importance=0.5,
            summary="fresh chat",
        )
        await _insert_row(
            conn,
            episode_id="stale",
            tenant="default",
            kind="conversation",
            ended_at=now - 3 * 86_400 * 1000,
            importance=0.99,
            summary="stale chat",
        )
    finally:
        await conn.close()

    resolver = EpisodesResolver(tmp_path).with_fixed_now_ms(now)
    try:
        out = await resolver.resolve("last_24h", ctx=_ctx("default"))
    finally:
        await resolver.close()

    assert "fresh chat" in out
    assert "stale chat" not in out


async def test_recent_orders_by_ended_at_regardless_of_score(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    try:
        now = 1_700_000_000_000
        await _insert_row(
            conn,
            episode_id="low-recent",
            tenant="default",
            kind="conversation",
            ended_at=now,
            importance=0.1,
            summary="low recent",
        )
        await _insert_row(
            conn,
            episode_id="high-old",
            tenant="default",
            kind="conversation",
            ended_at=now - 86_400 * 1000,
            importance=0.99,
            summary="high old",
        )
    finally:
        await conn.close()

    resolver = EpisodesResolver(tmp_path).with_fixed_now_ms(now)
    try:
        out = await resolver.resolve("recent", ctx=_ctx("default"))
    finally:
        await resolver.close()

    lines = out.splitlines()
    assert len(lines) == 2
    assert "low recent" in lines[0]
    assert "high old" in lines[1]


async def test_kind_filter_returns_only_matching_kind(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    try:
        now = 1_700_000_000_000
        await _insert_row(
            conn,
            episode_id="evo",
            tenant="default",
            kind="evolution",
            ended_at=now,
            importance=0.5,
            summary="an apply",
        )
        await _insert_row(
            conn,
            episode_id="chat",
            tenant="default",
            kind="conversation",
            ended_at=now,
            importance=0.5,
            summary="a chat",
        )
        await _insert_row(
            conn,
            episode_id="incident",
            tenant="default",
            kind="incident",
            ended_at=now,
            importance=0.5,
            summary="fire!",
        )
    finally:
        await conn.close()

    resolver = EpisodesResolver(tmp_path).with_fixed_now_ms(now)
    try:
        out = await resolver.resolve("kind(incident)", ctx=_ctx("default"))
    finally:
        await resolver.close()

    assert "fire!" in out
    assert "an apply" not in out
    assert "a chat" not in out


async def test_about_id_returns_single_episode(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    try:
        await _insert_row(
            conn,
            episode_id="ep-cite",
            tenant="default",
            kind="conversation",
            ended_at=1,
            importance=0.5,
            summary="cite-me",
        )
    finally:
        await conn.close()

    resolver = EpisodesResolver(tmp_path)
    try:
        out = await resolver.resolve("about_id(ep-cite)", ctx=_ctx("default"))
    finally:
        await resolver.close()
    assert out == "cite-me"


async def test_about_id_missing_returns_empty(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    await conn.close()
    resolver = EpisodesResolver(tmp_path)
    try:
        out = await resolver.resolve("about_id(nope)", ctx=_ctx("default"))
    finally:
        await resolver.close()
    assert out == ""


async def test_unknown_token_round_trips_literal(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    await conn.close()
    resolver = EpisodesResolver(tmp_path)
    try:
        out = await resolver.resolve("gibberish", ctx=_ctx("default"))
    finally:
        await resolver.close()
    assert out == "{{episodes.gibberish}}"


async def test_tenant_isolation_prevents_cross_reads(tmp_path: Path) -> None:
    now = 1_700_000_000_000
    conn_a = await _open_tenant(tmp_path, "acme")
    try:
        await _insert_row(
            conn_a,
            episode_id="ep-a",
            tenant="acme",
            kind="conversation",
            ended_at=now,
            importance=0.9,
            summary="secret-a",
        )
    finally:
        await conn_a.close()
    conn_b = await _open_tenant(tmp_path, "globex")
    try:
        await _insert_row(
            conn_b,
            episode_id="ep-b",
            tenant="globex",
            kind="conversation",
            ended_at=now,
            importance=0.9,
            summary="secret-b",
        )
    finally:
        await conn_b.close()

    resolver = EpisodesResolver(tmp_path).with_fixed_now_ms(now)
    try:
        out_a = await resolver.resolve("recent", ctx=_ctx("acme"))
        out_b = await resolver.resolve("recent", ctx=_ctx("globex"))
    finally:
        await resolver.close()

    assert "secret-a" in out_a
    assert "secret-b" not in out_a
    assert "secret-b" in out_b
    assert "secret-a" not in out_b


async def test_last_referenced_at_updates_on_hit(tmp_path: Path) -> None:
    now = 1_700_000_000_000
    conn = await _open_tenant(tmp_path, "default")
    try:
        await _insert_row(
            conn,
            episode_id="stamp-me",
            tenant="default",
            kind="conversation",
            ended_at=now,
            importance=0.5,
            summary="hit",
        )
        async with conn.execute(
            "SELECT last_referenced_at FROM episodes WHERE id = 'stamp-me'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] is None
    finally:
        await conn.close()

    resolver = EpisodesResolver(tmp_path).with_fixed_now_ms(now + 5_000)
    try:
        await resolver.resolve("recent", ctx=_ctx("default"))
    finally:
        await resolver.close()

    # Re-open to read the post-stamp value.
    conn2 = await aiosqlite.connect(
        str(tenant_db_path(tmp_path, TenantId.new("default"), "episodes"))
    )
    try:
        async with conn2.execute(
            "SELECT last_referenced_at FROM episodes WHERE id = 'stamp-me'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == now + 5_000
    finally:
        await conn2.close()


async def test_missing_tenant_db_returns_empty_for_known_token(tmp_path: Path) -> None:
    resolver = EpisodesResolver(tmp_path)
    try:
        out = await resolver.resolve("recent", ctx=_ctx("never-existed"))
    finally:
        await resolver.close()
    assert out == ""


async def test_missing_tenant_db_round_trips_unknown_literal(tmp_path: Path) -> None:
    resolver = EpisodesResolver(tmp_path)
    try:
        out = await resolver.resolve("gibberish", ctx=_ctx("never-existed"))
    finally:
        await resolver.close()
    assert out == "{{episodes.gibberish}}"


async def test_about_tag_round_trips_literal_for_now(tmp_path: Path) -> None:
    conn = await _open_tenant(tmp_path, "default")
    await conn.close()
    resolver = EpisodesResolver(tmp_path)
    try:
        out = await resolver.resolve("about(skill_update)", ctx=_ctx("default"))
    finally:
        await resolver.close()
    assert out == "{{episodes.about(skill_update)}}"


async def test_invalid_tenant_id_surfaces_resolver_error(tmp_path: Path) -> None:
    resolver = EpisodesResolver(tmp_path)
    try:
        with pytest.raises(RuntimeError) as info:
            await resolver.resolve("recent", ctx=_ctx(".."))
    finally:
        await resolver.close()
    assert "invalid tenant id" in str(info.value)


def test_default_constants_match_rust_contract() -> None:
    assert DEFAULT_TENANT_SLUG == "default"
    assert TENANT_METADATA_KEY == "tenant_id"
    assert SUMMARY_CHAR_CAP == 240
    assert DEFAULT_TOP_N == 5
    assert VALID_KINDS == (
        "conversation",
        "evolution",
        "incident",
        "onboarding",
        "operator",
    )


def test_episode_brief_is_frozen() -> None:
    brief = EpisodeBrief(id="a", summary_text="b")
    with pytest.raises((AttributeError, Exception)):
        brief.id = "c"  # type: ignore[misc]
