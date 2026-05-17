"""``corlinman_server.gateway`` — Python port of the Rust ``corlinman-gateway`` crate.

This package is split into focused submodules that are populated by
separate porting agents:

* :mod:`corlinman_server.gateway.lifecycle` — boot / shutdown / argparse,
  FastAPI app factory, legacy data-file migration, Rust→Python config
  handshake helpers.
* :mod:`corlinman_server.gateway.placeholder` — gateway-owned
  ``DynamicResolver`` implementations for the ``{{memory.*}}`` and
  ``{{episodes.*}}`` namespaces. Delegates to ``corlinman-memory-host``
  and ``corlinman-episodes`` where a Python sibling already covers the
  surface.
* (sibling agents) ``core`` / ``middleware`` / ``routes`` / ``grpc`` /
  ``services`` / ``evolution``.

The submodules here keep their imports local so a partial / incomplete
sibling can't break ``import corlinman_server.gateway.lifecycle`` at
boot — the entrypoint resolves the missing wiring via ``try/except
ImportError`` lazy imports.
"""

from __future__ import annotations

__all__: list[str] = []
