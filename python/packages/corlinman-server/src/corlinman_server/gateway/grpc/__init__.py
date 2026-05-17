"""``corlinman_server.gateway.grpc`` — Rust-hosted gRPC service surfaces.

Most corlinman gRPC services are owned by the Python plane (Agent /
Embedding / Vector / LLM on ``/tmp/corlinman-py.sock`` over
``grpc.aio``). This module is the **reverse direction**: services that
the Python gateway hosts so other clients — historically the Rust
gateway, now the in-process callers and the Python ``context_assembler``
— can dial against it without re-implementing the resolver registry.

Currently hosts:

* :mod:`corlinman_server.gateway.grpc.placeholder` — wraps the
  ``PlaceholderEngine`` so a Python client can expand
  ``{{namespace.name}}`` tokens without re-implementing the resolver
  registry. Ports :rust:`corlinman_gateway::grpc::placeholder`.

Why a Python-side placeholder gRPC server?
------------------------------------------
The Rust gateway used to host this so the Python
``context_assembler`` could dial in. After the gateway crate's slow
migration to Python the Python side now both produces and consumes
``Placeholder.Render`` calls — keeping the same on-the-wire contract
means any external tool (admin shell, future Rust subsystems, replay
harness) can still dial the same UDS and get the same answers.
"""

from __future__ import annotations

from corlinman_server.gateway.grpc.placeholder import (
    DEFAULT_RUST_SOCKET,
    ENV_RUST_SOCKET,
    PlaceholderService,
    serve as serve_placeholder,
)

__all__ = [
    "DEFAULT_RUST_SOCKET",
    "ENV_RUST_SOCKET",
    "PlaceholderService",
    "serve_placeholder",
]
