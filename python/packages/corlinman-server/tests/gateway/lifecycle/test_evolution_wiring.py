"""W5.0 — gateway lifespan evolution-store wiring.

Asserts that :func:`build_app` plus its ``_lifespan`` context manager:

* opens (or creates) ``<data_dir>/evolution.sqlite`` at startup;
* attaches :class:`SignalsRepo` + :class:`CuratorStateRepo` to the
  admin_b ``AdminState`` so ``/admin/curator/profiles`` returns 200
  (not 503 ``curator_state_repo_missing``);
* mirrors the same handles + a ``skill_registry_factory`` closure onto
  the admin_a ``AdminState`` so the W4.5 user-correction applier can
  resolve a per-profile :class:`SkillRegistry` view;
* registers the user-correction :class:`HookBus` listener so a chat
  message containing a corrective phrase produces an
  ``EVENT_USER_CORRECTION`` row in ``evolution_signals``;
* closes the store cleanly on lifespan exit (no dangling fd, second
  ``await close()`` is a safe no-op);
* degrades gracefully when the sqlite open fails — the gateway still
  boots and the curator routes return their typed 503 envelopes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from corlinman_evolution_store import (
    EVENT_USER_CORRECTION,
    EvolutionStore,
    SignalsRepo,
)
from corlinman_server.gateway.lifecycle.entrypoint import build_app


# ---------------------------------------------------------------------------
# Happy path: lifespan wires the evolution store and curator routes 200
# ---------------------------------------------------------------------------


def test_lifespan_opens_evolution_store_and_attaches_repos(tmp_path: Path) -> None:
    """After ``with TestClient(app):`` the sqlite file exists, the
    ``_evolution_store`` handle is published on ``app.state``, and the
    admin_b state has the curator + signals repos attached."""
    app = build_app(config_path=None, data_dir=tmp_path)

    with TestClient(app):
        evolution_db = tmp_path / "evolution.sqlite"
        assert evolution_db.exists(), "lifespan should have created the sqlite file"

        # Store handle published for shutdown cleanup + observability.
        store = getattr(app.state, "_evolution_store", None)
        assert store is not None, "lifespan should publish _evolution_store"

        admin_b_state = getattr(app.state, "corlinman_admin_b_state", None)
        assert admin_b_state is not None, "admin_b state must be wired"
        assert admin_b_state.curator_state_repo is not None
        assert admin_b_state.signals_repo is not None

        # admin_a inherits the signals_repo and a skill_registry_factory
        # so the user-correction applier can resolve per-profile views.
        admin_a_state = getattr(app.state, "corlinman_admin_a_state", None)
        assert admin_a_state is not None
        assert getattr(admin_a_state, "signals_repo", None) is not None
        assert getattr(admin_a_state, "skill_registry_factory", None) is not None

    # After the lifespan exits, the close hook clears the slot so a
    # second teardown is a no-op.
    assert getattr(app.state, "_evolution_store", None) is None


def test_curator_profiles_route_no_longer_returns_503(tmp_path: Path) -> None:
    """The headline win — ``/admin/curator/profiles`` returned 503
    ``curator_state_repo_missing`` before W5.0. With the lifespan
    wiring in place it returns 200 and includes the default profile."""
    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/admin/curator/profiles")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "profiles" in body
        slugs = [row["slug"] for row in body["profiles"]]
        # _mount_routes seeds a "default" profile on first boot.
        assert "default" in slugs


def test_evolution_db_dir_created_on_demand(tmp_path: Path) -> None:
    """Lifespan creates the parent of ``evolution.sqlite`` even when
    ``data_dir`` itself is a fresh path that doesn't exist yet."""
    nested = tmp_path / "fresh" / "data"
    app = build_app(config_path=None, data_dir=nested)
    with TestClient(app):
        assert (nested / "evolution.sqlite").exists()


# ---------------------------------------------------------------------------
# HookBus + user-correction listener round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_correction_listener_persists_signal_to_sqlite(
    tmp_path: Path,
) -> None:
    """Emit a fake user message containing a corrective phrase through
    the gateway's :class:`HookBus` and assert that a row lands in the
    ``evolution_signals`` table with ``event_kind = user.correction``.

    The applier's downstream ``spawn_background_review`` call requires
    a real LLM provider — :func:`_resolve_provider` returns
    ``(None, "")`` in the gateway boot path so the applier
    short-circuits at the provider-resolve gate. That's fine here; this
    test only asserts the *signal* lands, which is the listener's
    contract.
    """
    from corlinman_hooks import HookEvent

    app = build_app(config_path=None, data_dir=tmp_path)

    # FastAPI's lifespan only fires under TestClient when an HTTP call
    # is made; use the manager directly so we can drive the bus from
    # the same task that owns the loop.
    async with _async_lifespan(app):
        bus = getattr(app.state, "hook_bus", None)
        assert bus is not None, "lifespan should construct app.state.hook_bus"

        # Emit a user-correction-ish message. ``MessageReceived``
        # carries the user-authored text in ``content``; the detector
        # fires on "stop using" via its imperative regex.
        event = HookEvent.MessageReceived(
            channel="test",
            session_key_="sess-w5-test",
            content="please stop using bullets",
            metadata=None,
            user_id="user-1",
        )
        await bus.emit(event)

        # The listener spawns a fire-and-forget task per event; give
        # the loop a couple of ticks to drain detection -> insert.
        for _ in range(20):
            await asyncio.sleep(0.05)
            signals = await _read_signals(tmp_path / "evolution.sqlite")
            if any(s.event_kind == EVENT_USER_CORRECTION for s in signals):
                break
        else:
            pytest.fail(
                "user-correction signal was never persisted "
                f"(saw {[s.event_kind for s in signals]})"
            )

        corrections = [s for s in signals if s.event_kind == EVENT_USER_CORRECTION]
        assert corrections, "expected at least one user.correction row"
        sig = corrections[0]
        assert sig.session_id == "sess-w5-test"
        assert isinstance(sig.payload_json, dict)
        # The detector stamps ``kind`` + ``weight`` + ``text`` into payload.
        assert "kind" in sig.payload_json
        assert sig.payload_json.get("kind") == "imperative"
        assert "stop" in (sig.payload_json.get("snippet") or "").lower()


# ---------------------------------------------------------------------------
# Failure path: bad data_dir still boots (degraded mode)
# ---------------------------------------------------------------------------


def test_bad_data_dir_does_not_crash_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If :meth:`EvolutionStore.open` raises (forced via a patch), the
    lifespan logs a WARN and the gateway still boots — health probe
    still answers, and ``_evolution_store`` is ``None``."""

    async def _boom(path):  # noqa: ANN001
        raise RuntimeError("induced open failure")

    monkeypatch.setattr(
        "corlinman_evolution_store.EvolutionStore.open",
        classmethod(lambda cls, path: _boom(path)),  # type: ignore[arg-type]
    )

    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        # Gateway still serves liveness — degraded-mode resilience.
        resp = client.get("/health")
        assert resp.status_code == 200

        # Store handle is absent → curator routes return their typed
        # 503 envelope rather than crashing the request.
        assert getattr(app.state, "_evolution_store", None) is None
        resp = client.get("/admin/curator/profiles")
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "curator_state_repo_missing"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _async_lifespan:  # noqa: N801 — context-manager helper
    """Tiny shim that drives a FastAPI ``lifespan`` context directly.

    :class:`TestClient` opens the lifespan from a background thread,
    which means our async assertions can't share its ``hook_bus``
    instance (different loop). Driving the lifespan from the current
    coroutine keeps everything on one loop.
    """

    def __init__(self, app):  # noqa: ANN001
        self._app = app
        self._ctx = None

    async def __aenter__(self):
        # FastAPI exposes the lifespan factory at ``router.lifespan_context``.
        self._ctx = self._app.router.lifespan_context(self._app)
        await self._ctx.__aenter__()
        return self._app

    async def __aexit__(self, exc_type, exc, tb):
        assert self._ctx is not None
        await self._ctx.__aexit__(exc_type, exc, tb)


async def _read_signals(db_path: Path):
    """Open a fresh aiosqlite read view and return every row in
    ``evolution_signals``.

    We open a *separate* :class:`EvolutionStore` here (different from
    the one the gateway holds) because the writer keeps its connection
    busy with auto-commit + WAL — both connections see the same on-disk
    state thanks to WAL's reader-isolation.
    """
    store = await EvolutionStore.open(db_path)
    try:
        repo = SignalsRepo(store.conn)
        return await repo.list_since(0, None, 100)
    finally:
        await store.close()
