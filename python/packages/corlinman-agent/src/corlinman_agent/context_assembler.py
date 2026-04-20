"""Context assembler — expands template placeholders before calling a provider.

Responsibility: resolve ``{{namespace.name}}`` placeholders (DailyNote,
weather, detector, tool-catalog, etc.) by calling into the Rust vector
service (gRPC reverse-call) and the DailyNote store.

TODO(M2): define a ``Placeholder`` protocol and register resolvers;
dedupe with Rust-side placeholder handling in ``corlinman-core::placeholder``.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)
