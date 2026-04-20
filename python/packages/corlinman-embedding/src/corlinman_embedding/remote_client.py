"""Remote embedding client — HTTP client for hosted embedding APIs.

Responsibility: talk to OpenAI / vendor embedding endpoints via ``httpx``
and normalise responses to ``list[list[float]]``. Lives in the slim
``corlinman:1.0.0`` image (no torch dependency).

TODO(M4): implement with batched requests, retry via
``corlinman-core`` backoff schedule, and ``CorlinmanError`` mapping.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)
