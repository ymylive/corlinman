"""``gateway.lifecycle`` — boot / shutdown surface of the gateway.

Public API:

* :func:`build_app` — FastAPI app factory.
* :func:`main` — console-script entrypoint (parses ``--config``, builds
  the app, runs uvicorn).
* :func:`migrate_legacy_data_files` — one-shot pre-Phase-4 → per-tenant
  data-file migration.
* :func:`render_py_config` / :func:`write_py_config` — the Rust→Python
  config handshake JSON. Kept available so a Python admin route that
  mutates providers can re-emit the file without an additional
  cross-package dep (and because parity with the Rust API is cheap).
"""

from __future__ import annotations

from corlinman_server.gateway.lifecycle.entrypoint import build_app, main
from corlinman_server.gateway.lifecycle.legacy_migration import (
    LEGACY_DB_NAMES,
    migrate_legacy_data_files,
)
from corlinman_server.gateway.lifecycle.py_config import (
    DEFAULT_PY_CONFIG_FILENAME,
    ENV_PY_CONFIG,
    default_py_config_path,
    render_py_config,
    write_py_config,
    write_py_config_sync,
)

__all__ = [
    "DEFAULT_PY_CONFIG_FILENAME",
    "ENV_PY_CONFIG",
    "LEGACY_DB_NAMES",
    "build_app",
    "default_py_config_path",
    "main",
    "migrate_legacy_data_files",
    "render_py_config",
    "write_py_config",
    "write_py_config_sync",
]
