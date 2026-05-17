"""Shared fixtures for ``tests/plugins/``.

Defines a ``no_docker`` flag so Docker-dependent tests skip cleanly on
machines without a daemon. Lifted from the spec's
``@pytest.mark.skipif(no_docker)`` shape.
"""

from __future__ import annotations

import contextlib
import shutil


def _docker_unreachable() -> bool:
    """Return ``True`` when no usable Docker daemon is detectable.

    The check is conservative — we only try to import the SDK and ping
    the daemon; any failure (missing binary, missing socket, refused
    connection) marks Docker as unreachable.
    """
    if shutil.which("docker") is None:
        return True
    try:
        import docker  # type: ignore[import-not-found]
    except ImportError:
        return True
    client = None
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        return True
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
    return False


# Exposed as a module-level constant so individual tests can reference it
# via ``pytest.mark.skipif(no_docker, reason=...)``.
no_docker = _docker_unreachable()
