"""``corlinman`` CLI — Python port of the Rust ``corlinman-cli`` crate.

The root entry point is :func:`corlinman_server.cli.main.main`, wired into
``[project.scripts]`` as the ``corlinman`` console script.

Subcommand tree mirrors the Rust binary 1:1:

* ``onboard``  — first-run wizard, ``--non-interactive`` + ``--accept-risk``
* ``doctor``   — diagnostic checks, ``--json`` / ``--module``
* ``plugins``  — list / inspect / invoke / doctor
* ``config``   — show / get / set / validate / init / diff / migrate-sub2api
* ``dev``      — watch / gen-proto / check (stubs)
* ``qa``       — run / bench (stubs)
* ``vector``   — stats / query / rebuild / namespaces (stubs)
* ``tenant``   — create / list (full)
* ``replay``   — deterministic session replay (full)
* ``migrate``  — evolution-store migrations (stub)
* ``rollback`` — auto-rollback run-once (stub)
* ``skills``   — skill registry list/inspect (stub)
* ``identity`` — channel-alias resolver inspect (stub)

For commands that aren't fully ported (marked as STUBs) the dispatch
function prints ``TODO: not yet ported in Python migration`` to stderr
and exits with status ``2`` so callers see a uniform "not implemented"
contract.
"""

from __future__ import annotations

from corlinman_server.cli.main import main

__all__ = ["main"]
