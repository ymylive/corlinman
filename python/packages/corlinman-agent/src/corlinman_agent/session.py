"""Session state — short-lived per-request context shared by the reasoning loop.

Responsibility: bundle ``session_key`` (from ChannelBinding), trace context,
conversation messages, pending tool calls, and cancellation token so every
helper in :mod:`corlinman_agent` takes a single ``Session`` instead of a
growing parameter list.

TODO(M2): implement as pydantic ``BaseModel`` (strict) with
``model_config = ConfigDict(arbitrary_types_allowed=True)`` for the cancel
token and trace context.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)
