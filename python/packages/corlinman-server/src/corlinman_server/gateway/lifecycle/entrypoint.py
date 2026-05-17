"""``corlinman-gateway`` console-script entrypoint.

Python port of ``rust/crates/corlinman-gateway/src/main.rs``.

Boot sequence (parity with the Rust binary, simplified for an
ASGI-on-uvicorn deployment):

1. Parse ``--config <path>`` / ``--host`` / ``--port`` / ``--data-dir``
   from the CLI (also accepts ``CORLINMAN_CONFIG`` / ``BIND`` / ``PORT``
   / ``CORLINMAN_DATA_DIR`` env vars to keep deployments that already
   set them working).
2. Initialise telemetry (OTLP exporter + structlog binding) via
   :mod:`corlinman_server.telemetry`.
3. Run the one-shot legacy data-file migration when the config gates it.
4. Emit the Rust→Python config handshake JSON drop (so any in-process
   consumer that watches ``CORLINMAN_PY_CONFIG`` sees a non-empty
   registry from the first request).
5. Build the FastAPI :class:`fastapi.FastAPI` app via :func:`build_app`
   (lazy-imports the routes/middleware/core/grpc submodules being landed
   by sibling agents).
6. Run uvicorn programmatically with graceful shutdown wired to
   SIGTERM/SIGINT.

Sibling-module wiring
---------------------

Other agents own ``gateway/core/``, ``gateway/middleware/``,
``gateway/routes/``, ``gateway/grpc/``, ``gateway/services/``,
``gateway/evolution/``. Their modules may not exist when this file is
imported, so the FastAPI app factory uses :func:`_lazy_import` to swallow
:class:`ImportError` and log the missing wiring. The expected contract:

* ``gateway.core.AppState.build(config=...)`` → returns an ``AppState``
  bundle (analogue of the Rust ``AppState`` struct).
* ``gateway.middleware.install(app, state)`` → installs every
  cross-cutting middleware (tracing, approval gate, tenant resolution).
* ``gateway.routes.mount(app, state)`` → mounts every HTTP route
  (chat / admin / channels / canvas / …).
* ``gateway.grpc.serve_placeholder_in_background(state, cancel)`` →
  spawns the Rust→Python placeholder UDS server (returns an awaitable).
* ``gateway.services.bootstrap(state)`` → spins up channel adapters,
  plugin supervisors, hot reloaders.
* ``gateway.evolution.bootstrap(state)`` → spawns the evolution observer
  + scheduler.

Each hook is best-effort: a missing sibling logs ``warning`` and the
gateway boots in degraded mode so a partial port can still serve.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.gateway.lifecycle.admin_seed import (
    ensure_admin_credentials,
    resolve_admin_config_path,
)
from corlinman_server.gateway.lifecycle.legacy_migration import (
    migrate_legacy_data_files,
)
from corlinman_server.gateway.lifecycle.py_config import (
    default_py_config_path,
    write_py_config_sync,
)

logger = structlog.get_logger(__name__)

#: Mirrors ``corlinman_gateway::main::resolve_addr`` — same defaults so a
#: deployment-script that sets ``PORT`` / ``BIND`` against the Rust
#: binary keeps working against the Python port.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 6005
SIGTERM_EXIT_CODE: int = 143


# ---------------------------------------------------------------------------
# Lazy-import helper for sibling modules
# ---------------------------------------------------------------------------


def _lazy_import(dotted: str) -> Any | None:
    """Import ``dotted`` and return the module; ``None`` on ImportError.

    The siblings populated by parallel agents may not exist when this
    file is imported. Swallowing ``ImportError`` lets ``build_app`` boot
    in degraded mode without leaking partial-port state into a startup
    crash.
    """
    try:
        return importlib.import_module(dotted)
    except ImportError as exc:
        logger.warning(
            "gateway.sibling_missing",
            module=dotted,
            error=str(exc),
            detail=(
                "sibling submodule not present; gateway will boot in "
                "degraded mode without it"
            ),
        )
        return None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _resolve_config_path(cli_value: str | None) -> Path | None:
    """``--config`` > ``$CORLINMAN_CONFIG`` > ``None``. Mirrors the
    Rust ``main::load_config`` precedence."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_CONFIG")
    if env:
        return Path(env)
    return None


def _resolve_data_dir(cli_value: str | None) -> Path:
    """``--data-dir`` > ``$CORLINMAN_DATA_DIR`` > ``~/.corlinman`` >
    ``./.corlinman``. Mirrors ``corlinman_gateway::server::resolve_data_dir``."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    try:
        return Path.home() / ".corlinman"
    except (RuntimeError, OSError):
        return Path(".corlinman")


def _load_config(path: Path | None) -> Any | None:
    """Best-effort config load.

    Sibling agents populate ``gateway.core.config`` (a Python port of
    ``corlinman_core::config::Config``). We import lazily so the
    entrypoint module stays importable without it. A missing file or
    missing loader returns ``None`` and the gateway boots with whatever
    defaults the downstream modules carry.
    """
    if path is None:
        return None
    if not path.exists():
        logger.warning("gateway.config.missing", path=str(path))
        return None
    core_config = _lazy_import("corlinman_server.gateway.core.config")
    if core_config is None:
        logger.warning(
            "gateway.config.no_loader",
            path=str(path),
            detail="gateway.core.config not present; skipping load",
        )
        return None
    loader: Callable[[Path], Any] | None = (
        getattr(core_config, "load_from_path", None)
        or getattr(core_config, "Config", None)
    )
    if loader is None:
        logger.warning("gateway.config.no_loader_symbol", path=str(path))
        return None
    try:
        cfg = loader(path)  # type: ignore[misc]
        # ``Config(path)`` returning a class is fine — the duck-typed
        # downstream code only reads attributes off whatever we hand it.
    except Exception as exc:
        logger.warning(
            "gateway.config.load_failed", path=str(path), error=str(exc)
        )
        return None
    logger.info("gateway.config.loaded", path=str(path))
    return cfg


def _should_run_legacy_migration(cfg: Any | None) -> bool:
    """Mirror the Rust gate: ``[tenants].enabled && [tenants].migrate_legacy_paths``.

    Default off — pre-Phase-4 deployments keep their flat layout unless
    the operator opts in.
    """
    if cfg is None:
        return False
    tenants = getattr(cfg, "tenants", None)
    if tenants is None and isinstance(cfg, dict):
        tenants = cfg.get("tenants")
    if tenants is None:
        return False
    enabled = (
        getattr(tenants, "enabled", None)
        if not isinstance(tenants, dict)
        else tenants.get("enabled")
    )
    migrate = (
        getattr(tenants, "migrate_legacy_paths", None)
        if not isinstance(tenants, dict)
        else tenants.get("migrate_legacy_paths")
    )
    return bool(enabled) and bool(migrate)


def _emit_py_config_drop(cfg: Any | None) -> None:
    """Best-effort write of the JSON handshake file.

    No-op when ``cfg`` is ``None`` — there's nothing to render and the
    Python AI plane falls back to the legacy prefix table in that case
    (matches the Rust behaviour).
    """
    if cfg is None:
        return
    target = Path(
        os.environ.get("CORLINMAN_PY_CONFIG") or str(default_py_config_path())
    )
    try:
        write_py_config_sync(cfg, target)
        logger.info("gateway.py_config.written", path=str(target))
    except Exception as exc:
        logger.warning(
            "gateway.py_config.write_failed",
            path=str(target),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# AppState bridge
# ---------------------------------------------------------------------------


def _build_state(cfg: Any | None, data_dir: Path) -> Any:
    """Construct the shared ``AppState`` bundle.

    Delegates to ``gateway.core.AppState`` when available; falls back to
    a minimal :class:`_DegradedAppState` so degraded-mode boots still
    have *some* object to pass into route handlers / tests.
    """
    core = _lazy_import("corlinman_server.gateway.core")
    if core is not None:
        builder: Any = (
            getattr(core, "build_app_state", None)
            or getattr(core, "AppState", None)
        )
        if builder is not None:
            built: Any = None
            try:
                built = builder(config=cfg, data_dir=data_dir)  # type: ignore[misc]
            except TypeError:
                # The real ``AppState`` doesn't accept ``data_dir`` as a
                # kwarg (it's a free-form attribute the gateway wires
                # at boot). Fall back to a ``config``-only call and
                # stamp ``data_dir`` afterwards so downstream code can
                # still do ``getattr(state, "data_dir", None)``.
                try:
                    built = builder(config=cfg)  # type: ignore[misc]
                except TypeError:
                    try:
                        built = builder()  # type: ignore[misc]
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning(
                            "gateway.state.builder_failed",
                            builder=type(builder).__name__,
                            error=str(exc),
                        )
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.state.builder_failed",
                        builder=type(builder).__name__,
                        error=str(exc),
                    )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "gateway.state.builder_failed",
                    builder=type(builder).__name__,
                    error=str(exc),
                )
            if built is not None:
                # AppState is a dataclass without __slots__ — safe to
                # set attributes dynamically. This is the contract
                # ``_mount_routes`` reads via ``getattr(state,
                # "data_dir", None)`` to wire the profile store.
                try:
                    setattr(built, "data_dir", data_dir)
                except (AttributeError, TypeError):  # pragma: no cover
                    pass
                if getattr(built, "config", None) is None and cfg is not None:
                    try:
                        setattr(built, "config", cfg)
                    except (AttributeError, TypeError):  # pragma: no cover
                        pass
                return built
    return _DegradedAppState(config=cfg, data_dir=data_dir)


class _DegradedAppState:
    """Minimal stand-in used when ``gateway.core`` isn't ported yet.

    Carries just enough state for the placeholder resolvers + a basic
    ``/health`` route to function. Sibling agents will replace this with
    the real ``AppState`` bundle.
    """

    __slots__ = ("config", "data_dir")

    def __init__(self, *, config: Any | None, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"_DegradedAppState(data_dir={self.data_dir!r})"


# ---------------------------------------------------------------------------
# Routes composition (parallel-agent contracts diverge per submodule)
# ---------------------------------------------------------------------------


def _mount_routes(
    app: Any, state: Any, *, admin_config_path: Path | None = None
) -> Any:
    """Mount every gateway routes submodule onto ``app``.

    Each W4 submodule exposes a different composition surface; this
    helper wires them all in one place so ``build_app`` stays compact:

    * ``routes.register.build_app_router(state)`` — top-level / endpoint set
    * ``routes_voice.mod.router(voice_state)`` — /v1/voice WebSocket
    * ``routes_admin_a.build_router()`` — admin A bundle (9 sub-routers)
    * ``routes_admin_b.build_router()`` — admin B bundle (13 sub-routers)

    Missing submodules log a warning and the gateway continues to boot
    in degraded mode (so a partial port still serves health checks).

    Returns a ``(admin_a_state, admin_b_state)`` tuple — each entry is
    the registered ``AdminState`` instance for that subtree (or ``None``
    when the submodule isn't present). The lifespan reaches into the
    admin_a slot to populate seeded credentials after
    :func:`ensure_admin_credentials` completes, and into the admin_b
    slot to attach the evolution-store repos opened by W5.0. The state
    objects are registered with ``set_admin_state`` here so test code
    that doesn't run the lifespan still sees a usable singleton.
    """
    routes_top = _lazy_import("corlinman_server.gateway.routes.register")
    if routes_top is not None:
        try:
            gateway_state_cls = getattr(routes_top, "GatewayState", None)
            build_app_router = getattr(routes_top, "build_app_router", None)
            if gateway_state_cls is not None and build_app_router is not None:
                # GatewayState is a dataclass of duck-typed optional deps;
                # we hand it the AppState handle so route handlers can
                # downcast as they need.
                gw_state = (
                    gateway_state_cls(app_state=state)
                    if hasattr(gateway_state_cls, "__dataclass_fields__")
                    and "app_state" in gateway_state_cls.__dataclass_fields__
                    else gateway_state_cls()
                )
                app.include_router(build_app_router(gw_state))
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes.top.mount_failed", error=str(exc))

    routes_voice_mod = _lazy_import("corlinman_server.gateway.routes_voice.mod")
    if routes_voice_mod is not None:
        try:
            voice_router = routes_voice_mod.router()
            app.include_router(voice_router)
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_voice.mount_failed", error=str(exc))

    admin_a_state: Any | None = None
    admin_a = _lazy_import("corlinman_server.gateway.routes_admin_a")
    if admin_a is not None:
        try:
            admin_a_state_cls = getattr(admin_a, "AdminState", None)
            set_admin_a = getattr(admin_a, "set_admin_state", None)
            if admin_a_state_cls is not None and set_admin_a is not None:
                # Construct an AdminState seeded with what we can know
                # synchronously — admin_username / admin_password_hash /
                # must_change_password are populated by the lifespan once
                # ``ensure_admin_credentials`` resolves the disk state.
                data_dir = getattr(state, "data_dir", None)
                # Wave 3.1: wire the profile registry. Best-effort —
                # if the profiles submodule fails to import we leave
                # ``profile_store=None`` and the /admin/profiles* routes
                # 503 ``profile_store_missing`` rather than crashing the
                # gateway boot.
                profile_store: Any | None = None
                if data_dir is not None:
                    try:
                        from corlinman_server.profiles import ProfileStore

                        profile_store = ProfileStore(
                            Path(data_dir) / "profiles"
                        )
                        # Bootstrap a "default" profile on first run so
                        # the UI's profile-switcher always has at least
                        # one selectable entry.
                        if not profile_store.list():
                            profile_store.create(
                                slug="default",
                                display_name="Default",
                                description="Bootstrap profile",
                            )
                    except Exception as exc:  # pragma: no cover
                        logger.warning(
                            "gateway.routes_admin_a.profile_store_init_failed",
                            error=str(exc),
                        )
                        profile_store = None
                admin_a_state = admin_a_state_cls(
                    data_dir=data_dir,
                    config_path=admin_config_path,
                    admin_write_lock=asyncio.Lock(),
                    profile_store=profile_store,
                )
                set_admin_a(admin_a_state)
            app.include_router(admin_a.build_router())
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_admin_a.mount_failed", error=str(exc))

    admin_b_state: Any | None = None
    admin_b = _lazy_import("corlinman_server.gateway.routes_admin_b")
    if admin_b is not None:
        try:
            admin_b_state_cls = getattr(admin_b, "AdminState", None)
            set_admin_b = getattr(admin_b, "set_admin_state", None)
            if admin_b_state_cls is not None and set_admin_b is not None:
                # W4.6: thread the curator UI handles through to the
                # admin_b state. ``profile_store`` matches the admin_a
                # field so /admin/curator/* can look up profile rows;
                # ``skill_registry_factory`` lazy-loads per-profile
                # SkillRegistry views. ``curator_state_repo`` and
                # ``signals_repo`` are populated by the lifespan once
                # the evolution sqlite is opened — left ``None`` here so
                # the routes 503 cleanly during a partial install.
                _admin_a_config_path = (
                    getattr(admin_a_state, "config_path", None)
                    if admin_a_state is not None
                    else None
                )

                def _admin_b_config_loader() -> dict[str, Any]:
                    """Fresh-read the live config TOML on every snapshot
                    call. Captures :data:`_admin_a_config_path` via
                    closure so the credentials + onboard PUT paths see
                    sections (notably ``[admin]``) other handlers may
                    have rewritten between snapshot reads — without it
                    the write-back collapses to ``{providers: {...}}``
                    and quietly wipes the operator's credentials.
                    """
                    import tomllib  # noqa: PLC0415 — stdlib

                    if (
                        _admin_a_config_path is None
                        or not _admin_a_config_path.exists()
                    ):
                        return {}
                    try:
                        return tomllib.loads(
                            _admin_a_config_path.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        return {}

                _admin_b_state_kwargs: dict[str, Any] = {
                    "profile_store": (
                        getattr(admin_a_state, "profile_store", None)
                        if admin_a_state is not None
                        else None
                    ),
                    # Mirror the admin_a config_path so /admin/credentials*
                    # and /admin/onboard/finalize-skip can persist the
                    # [providers.*] block back to the same TOML the
                    # admin_seed bootstrap wrote. Without this the routes
                    # 503 with ``config_path_unset`` even though the
                    # gateway booted with a perfectly resolvable file.
                    "config_path": _admin_a_config_path,
                    # Fresh-read loader so the credentials and onboard
                    # routes see other sections (``[admin]`` first and
                    # foremost) when they rebuild + atomically rewrite
                    # the TOML.
                    "config_loader": _admin_b_config_loader,
                    # Admin-write lock shared with admin_a so the rotate
                    # / username / credentials writers don't race when
                    # both surfaces try to mutate the same TOML.
                    "admin_write_lock": (
                        getattr(admin_a_state, "admin_write_lock", None)
                        if admin_a_state is not None
                        else None
                    ),
                }
                # Per-profile registry factory: reads
                # ``<data_dir>/profiles/<slug>/skills`` for each call so
                # mid-run SKILL.md edits show up on the next fetch.
                data_dir_for_skills = getattr(state, "data_dir", None)
                if data_dir_for_skills is not None:
                    try:
                        from corlinman_skills_registry import (  # noqa: PLC0415
                            SkillRegistry,
                        )

                        def _skill_registry_factory(slug: str) -> Any:
                            skills_dir = (
                                Path(data_dir_for_skills)
                                / "profiles"
                                / slug
                                / "skills"
                            )
                            return SkillRegistry.load_from_dir(skills_dir)

                        _admin_b_state_kwargs["skill_registry_factory"] = (
                            _skill_registry_factory
                        )
                    except ImportError as exc:  # pragma: no cover
                        logger.warning(
                            "gateway.routes_admin_b.skill_registry_factory_missing",
                            error=str(exc),
                        )
                admin_b_state = admin_b_state_cls(**_admin_b_state_kwargs)
                set_admin_b(admin_b_state)
            app.include_router(admin_b.build_router())
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_admin_b.mount_failed", error=str(exc))

    return admin_a_state, admin_b_state


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def build_app(
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Build the FastAPI app + AppState and wire every sibling module.

    Returns a :class:`fastapi.FastAPI` instance ready to be served by
    uvicorn. The app exposes ``app.state.corlinman_state`` for tests /
    middleware that need the shared handle.

    Sibling agents that haven't landed yet just log a warning and skip
    their wiring step — the app still starts (in degraded mode) so the
    integration step can roll forward iteratively.
    """
    try:
        from fastapi import FastAPI
    except ImportError as exc:  # pragma: no cover — fastapi is a runtime dep
        raise RuntimeError(
            "fastapi is required for the gateway entrypoint; "
            "add it to corlinman-server's dependencies"
        ) from exc

    cfg = _load_config(config_path)
    resolved_data_dir = data_dir or _resolve_data_dir(None)

    # Phase 4 W1 4-1A Item 5: one-shot legacy data-file migration. Gated
    # on tenants config; default-off for back-compat.
    if _should_run_legacy_migration(cfg):
        try:
            migrate_legacy_data_files(resolved_data_dir)
        except OSError as exc:
            logger.warning(
                "gateway.legacy_migration.failed",
                data_dir=str(resolved_data_dir),
                error=str(exc),
            )

    # Feature C last-mile: re-emit the JSON drop so any in-process
    # consumer that mtime-watches CORLINMAN_PY_CONFIG sees a fully-formed
    # registry from boot.
    _emit_py_config_drop(cfg)

    state = _build_state(cfg, resolved_data_dir)

    # Resolve the on-disk path the admin-seed routine writes to / reads
    # back from. Cached on the FastAPI app so the lifespan handler can
    # re-use it after :func:`_mount_routes` already stamped it onto the
    # ``AdminState``. We compute it eagerly here so a missing
    # ``[admin]`` block still has a target path on first boot.
    admin_config_path = resolve_admin_config_path(
        cli_config_path=config_path, data_dir=resolved_data_dir
    )

    @asynccontextmanager
    async def _lifespan(app: Any):  # type: ignore[no-untyped-def]
        # Seed default ``admin``/``root`` credentials before the sibling
        # bootstraps fire — admin routes that load credentials lazily
        # (services / evolution) must see the resolved hash. The
        # ``AdminState`` was already registered with the singleton
        # during ``_mount_routes`` so we mutate it in place; FastAPI
        # only starts accepting requests after this coroutine yields.
        admin_a_state = getattr(app.state, "corlinman_admin_a_state", None)
        admin_b_state = getattr(app.state, "corlinman_admin_b_state", None)
        try:
            seeded = await ensure_admin_credentials(
                config_path=admin_config_path
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("gateway.admin_seed.failed", error=str(exc))
            seeded = None

        if admin_a_state is not None and seeded is not None:
            admin_a_state.admin_username = seeded.username
            admin_a_state.admin_password_hash = seeded.password_hash
            admin_a_state.config_path = seeded.config_path
            admin_a_state.must_change_password = seeded.must_change_password

        # W5.0: open the evolution sqlite + attach the curator / signals
        # repos to admin_b (the /admin/curator/* routes read them from
        # there) and to admin_a (W4.5 applier surfaces consult admin_a's
        # ``signals_repo`` / ``skill_registry_factory`` slots). All
        # best-effort — a sqlite open failure logs at WARN and the
        # gateway still boots, with the curator routes returning their
        # typed 503 envelopes instead.
        evolution_store: Any | None = None
        signals_repo: Any | None = None
        curator_state_repo: Any | None = None
        evolution_db_path = resolved_data_dir / "evolution.sqlite"
        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                CuratorStateRepo,
                EvolutionStore,
                SignalsRepo,
            )

            # ``EvolutionStore.open`` is the async classmethod that
            # creates parents (sqlite makes the file; we make the dir).
            evolution_db_path.parent.mkdir(parents=True, exist_ok=True)
            evolution_store = await EvolutionStore.open(evolution_db_path)
            # The repos share the store's underlying aiosqlite
            # connection — there are no ``store.signals_repo()`` /
            # ``store.curator_state_repo()`` accessors today, so we
            # construct them directly off ``store.conn``.
            signals_repo = SignalsRepo(evolution_store.conn)
            curator_state_repo = CuratorStateRepo(evolution_store.conn)

            if admin_b_state is not None:
                admin_b_state.curator_state_repo = curator_state_repo
                admin_b_state.signals_repo = signals_repo
                # Re-expose the raw store on admin_b too — a couple of
                # legacy /admin/evolution routes look it up from there.
                admin_b_state.evolution_store = evolution_store

            if admin_a_state is not None:
                # Dataclass allows dynamic attribute writes; the
                # user-correction applier reads ``signals_repo`` /
                # ``skill_registry_factory`` from admin_a so its
                # background-review fork can resolve a per-profile
                # SkillRegistry view at correction time.
                admin_a_state.signals_repo = signals_repo
                factory = getattr(
                    admin_b_state, "skill_registry_factory", None
                )
                if factory is None and admin_a_state is not None:
                    # Fallback factory mirrors the one wired in
                    # _mount_routes — covers cases where admin_b isn't
                    # mounted but admin_a still wants to spawn reviews.
                    try:
                        from corlinman_skills_registry import (  # noqa: PLC0415
                            SkillRegistry,
                        )

                        def _fallback_skill_registry(slug: str) -> Any:
                            skills_dir = (
                                resolved_data_dir
                                / "profiles"
                                / slug
                                / "skills"
                            )
                            skills_dir.mkdir(parents=True, exist_ok=True)
                            return SkillRegistry.load_from_dir(skills_dir)

                        factory = _fallback_skill_registry
                    except ImportError:  # pragma: no cover
                        factory = None
                admin_a_state.skill_registry_factory = factory

            # Stash the handle so the lifespan-exit ``finally`` can
            # close cleanly and external test code can introspect it.
            app.state._evolution_store = evolution_store
            app.state._evolution_signals_repo = signals_repo
            app.state._evolution_curator_state_repo = curator_state_repo
            logger.info(
                "gateway.evolution.store_opened",
                path=str(evolution_db_path),
            )
        except Exception as exc:  # pragma: no cover — defensive umbrella
            logger.warning(
                "gateway.evolution.store_open_failed",
                path=str(evolution_db_path),
                error=str(exc),
            )

        services = _lazy_import("corlinman_server.gateway.services")
        evolution = _lazy_import("corlinman_server.gateway.evolution")
        grpc_mod = _lazy_import("corlinman_server.gateway.grpc")

        cancel = asyncio.Event()
        background: list[asyncio.Task[Any]] = []

        for sibling, name in (
            (services, "services"),
            (evolution, "evolution"),
        ):
            if sibling is None:
                continue
            bootstrap = getattr(sibling, "bootstrap", None)
            if bootstrap is None:
                continue
            try:
                result = bootstrap(state)
                if isinstance(result, Awaitable):
                    await result
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.sibling.bootstrap_failed",
                    sibling=name,
                    error=str(exc),
                )

        if grpc_mod is not None:
            serve = getattr(
                grpc_mod, "serve_placeholder_in_background", None
            )
            if serve is not None:
                try:
                    task = asyncio.create_task(serve(state, cancel))
                    background.append(task)
                except Exception as exc:  # pragma: no cover — sibling-owned
                    logger.warning(
                        "gateway.grpc.bootstrap_failed", error=str(exc)
                    )

        # W5.0: wire the user-correction HookBus listener. Today no
        # other component constructs a shared HookBus in the gateway
        # boot path, so we build one here and publish it on
        # ``app.state.hook_bus`` for future producers (channels /
        # subagent supervisor / chat service) to reuse. The listener
        # itself only needs ``signals_repo`` and the applier callback;
        # missing either is an opt-out (we log + skip).
        user_correction_task: asyncio.Task[None] | None = None
        user_correction_applier: Any | None = None
        if signals_repo is not None:
            try:
                from corlinman_hooks import HookBus  # noqa: PLC0415

                bus = getattr(app.state, "hook_bus", None)
                if bus is None:
                    # Capacity mirrors the default the observer / other
                    # subscribers expect — 256 events of slack per tier
                    # before a slow handler trips ``Lagged``.
                    bus = HookBus(capacity=256)
                    app.state.hook_bus = bus

                from corlinman_server.gateway.evolution import (  # noqa: PLC0415
                    UserCorrectionApplier,
                    register_user_correction_listener,
                )

                def _resolve_provider(slug: str) -> tuple[Any, str]:
                    """Resolve ``(provider_instance, model_name)`` for a
                    profile. Today the gateway does not expose a stable
                    provider-lookup surface; we degrade to ``(None, "")``
                    and let ``UserCorrectionApplier`` short-circuit on
                    the resolver-failure gate. Wired here as a hook so
                    a sibling provider-wiring agent can later swap in
                    the real lookup without touching the listener.
                    """
                    return (None, "")

                # Closures over the just-attached admin_a slots — read
                # via getattr so a missing piece collapses to ``None``
                # rather than NameError. The applier's resolver
                # failure paths already log + gate gracefully.
                def _registry_for_profile(slug: str) -> Any:
                    fn = getattr(
                        admin_a_state, "skill_registry_factory", None
                    )
                    if fn is None:
                        raise RuntimeError("skill_registry_factory not wired")
                    return fn(slug)

                def _profile_root_for_profile(slug: str):
                    pstore = getattr(admin_a_state, "profile_store", None)
                    if pstore is None:
                        # Fall back to the conventional layout under
                        # ``<data_dir>/profiles/<slug>``.
                        return resolved_data_dir / "profiles" / slug
                    return Path(pstore.profiles_dir) / slug

                user_correction_applier = UserCorrectionApplier(
                    registry_for_profile=_registry_for_profile,
                    profile_root_for_profile=_profile_root_for_profile,
                    provider_for_profile=_resolve_provider,
                    rate_limit_seconds=30,
                    min_weight=0.7,
                )

                async def _on_signal(sig: Any) -> None:
                    # Fire-and-forget bridge — the listener already
                    # spawns ``asyncio.create_task`` around this
                    # callback, so a direct await is fine and keeps the
                    # ``last_fired`` map updates serialised.
                    await user_correction_applier.apply(sig)

                user_correction_task = register_user_correction_listener(
                    bus,
                    signals_repo,
                    on_signal=_on_signal,
                )
                background.append(user_correction_task)
                app.state._user_correction_applier = (
                    user_correction_applier
                )
                logger.info(
                    "gateway.evolution.user_correction_listener_registered"
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "gateway.evolution.user_correction_listener_failed",
                    error=str(exc),
                )

        try:
            yield
        finally:
            cancel.set()
            for task in background:
                task.cancel()
            for task in background:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            # W5.0 teardown: close the evolution sqlite cleanly so the
            # WAL file is checkpointed and tests don't leave stale
            # file handles open on Windows.
            store = getattr(app.state, "_evolution_store", None)
            if store is not None:
                try:
                    await store.close()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.evolution.store_close_failed",
                        error=str(exc),
                    )
                app.state._evolution_store = None

    app = FastAPI(lifespan=_lifespan)
    app.state.corlinman_state = state
    app.state.corlinman_config = cfg
    app.state.corlinman_data_dir = resolved_data_dir

    # Middleware before routes — order matters for ASGI stack walks.
    middleware = _lazy_import("corlinman_server.gateway.middleware")
    if middleware is not None:
        install = getattr(middleware, "install", None)
        if install is not None:
            try:
                install(app, state)
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.middleware.install_failed", error=str(exc)
                )

    # Mount every routes submodule. Each submodule exposes a different
    # composition surface (per the parallel-agent contracts); we wire them
    # individually here to keep entrypoint.py the single composition root.
    admin_a_state, admin_b_state = _mount_routes(
        app, state, admin_config_path=admin_config_path
    )
    # Stash the admin state handles on ``app.state`` so the lifespan
    # closure (defined above ``_mount_routes``'s call) can populate the
    # seeded credentials once :func:`ensure_admin_credentials` runs, and
    # so W5.0's evolution-store wiring can stamp the curator/signals
    # repos onto admin_b once the sqlite handle is open.
    app.state.corlinman_admin_a_state = admin_a_state
    app.state.corlinman_admin_b_state = admin_b_state
    app.state.corlinman_admin_config_path = admin_config_path

    # Degraded-mode safety net: if no routes module mounted a ``/health``
    # path, expose a trivial liveness endpoint so probes succeed.
    _have_health = any(
        getattr(r, "path", None) == "/health" for r in app.routes
    )
    if not _have_health:

        @app.get("/health")
        async def _health() -> dict[str, str]:  # pragma: no cover — trivial
            return {"status": "ok", "mode": "degraded"}

    # UI static fall-through. The docker image bakes the Next.js static
    # export into ``/app/ui-static``; this mount serves it for any path
    # not already claimed by an API route. SPA-style HTML routes
    # (/account/security, /profiles, /credentials, /evolution …) resolve
    # via the pre-rendered ``<route>.html`` files Next emits. Without
    # this mount the gateway answers every browser hit with 404 even
    # when the bundle is present on disk.
    ui_dir_env = os.environ.get("CORLINMAN_UI_DIR")
    if ui_dir_env:
        ui_path = Path(ui_dir_env)
        if ui_path.is_dir():
            try:
                from fastapi.staticfiles import StaticFiles  # noqa: PLC0415

                # Mount last so all explicit API routes (incl. /health,
                # /admin/*, /v1/*, /onboard) win in route resolution.
                app.mount(
                    "/",
                    StaticFiles(directory=str(ui_path), html=True),
                    name="ui",
                )
                logger.info(
                    "gateway.ui.static_mounted", path=str(ui_path)
                )
            except Exception as exc:  # pragma: no cover — best effort
                logger.warning(
                    "gateway.ui.static_mount_failed",
                    path=str(ui_path),
                    error=str(exc),
                )
        else:
            logger.warning(
                "gateway.ui.static_dir_missing", path=str(ui_path)
            )

    return app


# ---------------------------------------------------------------------------
# CLI / uvicorn driver
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="corlinman-gateway",
        description=(
            "Run the corlinman gateway (Python port of the Rust "
            "corlinman-gateway binary)."
        ),
    )
    p.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to the gateway config TOML. Falls back to "
        "$CORLINMAN_CONFIG, then no config (defaults).",
    )
    p.add_argument(
        "--host",
        dest="host",
        default=None,
        help=f"Bind host. Default: $BIND or {DEFAULT_HOST}.",
    )
    p.add_argument(
        "--port",
        dest="port",
        type=int,
        default=None,
        help=f"Bind port. Default: $PORT or {DEFAULT_PORT}.",
    )
    p.add_argument(
        "--data-dir",
        dest="data_dir",
        default=None,
        help="Override the data directory (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default=os.environ.get("LOG_LEVEL", "info"),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level. Default: $LOG_LEVEL or info.",
    )
    return p


def _resolve_bind(cli_host: str | None, cli_port: int | None) -> tuple[str, int]:
    host = cli_host or os.environ.get("BIND") or DEFAULT_HOST
    if cli_port is not None:
        port = cli_port
    else:
        env_port = os.environ.get("PORT")
        port = int(env_port) if env_port and env_port.isdigit() else DEFAULT_PORT
    return host, port


async def _serve(args: argparse.Namespace) -> int:
    """Build the app and run uvicorn until SIGTERM/SIGINT."""
    # Telemetry init (best-effort — missing OTLP endpoint is a no-op
    # inside the helper).
    try:
        from corlinman_server.telemetry import init_telemetry, shutdown_telemetry

        init_telemetry()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("gateway.telemetry.init_failed", error=str(exc))

        def shutdown_telemetry() -> None:  # type: ignore[misc]
            return None

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover — uvicorn is a runtime dep
        raise RuntimeError(
            "uvicorn is required for the gateway entrypoint; "
            "add it to corlinman-server's dependencies"
        ) from exc

    config_path = _resolve_config_path(args.config)
    data_dir = Path(args.data_dir) if args.data_dir else _resolve_data_dir(None)
    host, port = _resolve_bind(args.host, args.port)

    app = build_app(config_path=config_path, data_dir=data_dir)

    uv_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level=args.log_level,
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)

    # Wire SIGTERM/SIGINT to uvicorn's graceful-shutdown flag. uvicorn
    # installs its own handlers when run via ``uvicorn.run``; we use
    # ``Server.serve`` so we can return the right exit code.
    loop = asyncio.get_running_loop()
    received: list[str] = []

    def _on_signal(name: str) -> None:
        received.append(name)
        logger.info("gateway.shutdown.signal", signal=name)
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except NotImplementedError:
            # Windows / restricted envs — uvicorn's own signal hooks
            # will still trip; we just won't relay the name. Tests on
            # those platforms are not in scope.
            pass

    logger.info("gateway.serve.start", host=host, port=port)
    await server.serve()
    logger.info("gateway.serve.stopped")

    shutdown_telemetry()
    return SIGTERM_EXIT_CODE if any(r == "SIGTERM" for r in received) else 0


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint.

    Registered (when ``pyproject.toml`` is updated by the integration
    step) as ``corlinman-gateway = "corlinman_server.gateway.lifecycle.entrypoint:main"``.
    """
    args = _build_parser().parse_args(argv)
    try:
        code = asyncio.run(_serve(args))
    except KeyboardInterrupt:
        code = SIGTERM_EXIT_CODE
    sys.exit(code)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "SIGTERM_EXIT_CODE",
    "build_app",
    "main",
]
