"""Tool-call approval gate — mediates between model intent and plugin execution.

Responsibility: for each ``ToolCall`` the reasoning loop wants to dispatch,
consult ``toolApprovalConfig.json`` (auto / prompt / deny), and for
``prompt`` mode emit an ``AwaitingApproval`` frame and block until an
``ApprovalDecision`` comes back from the Rust gateway.

TODO(M2): parse ``toolApprovalConfig.json`` (schema lives in plan §7.6);
implement ``session.allow_session_keys`` short-circuit.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)
