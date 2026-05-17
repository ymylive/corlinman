"""``POST /v1/chat/completions/{turn_id}/approve`` — per-turn tool-approval relay.

Python port of
``rust/crates/corlinman-gateway/src/routes/chat_approve.rs``.

When the agent emits an ``AwaitingApproval`` event mid-stream, native
clients answer with a POST to this route — same Bearer token used
for ``/v1/chat/completions`` — instead of needing the admin route.
The handler validates the body, maps ``approved`` /
``deny_message`` into an :class:`ApprovalDecision`, and forwards
the decision to the gateway's approval gate (when wired).

Body shape::

    {
      "call_id": "call_abc123",
      "approved": true,
      "scope": "once",                  // "once" | "session" | "always"
      "deny_message": "explain why..."  // required when approved=false
    }

Response on success::

    { "turn_id": "...", "call_id": "call_abc123", "decision": "approved",
      "scope": "..." }

Error envelopes match the Rust impl byte-for-byte:

* **503 ``approvals_disabled``** — no gate wired.
* **400 ``invalid_request``** — empty ``call_id`` or denied without
  ``deny_message``.
* **404 ``not_found``** — gate doesn't know the call_id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "ApproveBody",
    "ApproveResponse",
    "ChatApproveState",
    "router",
]


# ─── Decision sum type ───────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class ApprovalDecision:
    """Operator decision. Mirrors the Rust ``ApprovalDecision`` enum:

    * ``kind == "approved"`` — let the tool call proceed.
    * ``kind == "denied"`` — short-circuit with ``reason``.
    * ``kind == "timeout"`` — internal-only; never client-supplied.
    """

    kind: Literal["approved", "denied", "timeout"]
    reason: str = ""


# ─── Gate protocol the route forwards to ─────────────────────────────


class ApprovalGate(BaseModel):
    """Minimal interface the route needs. Implementations come from
    sibling modules (``corlinman_server.gateway.middleware.approval``
    in the eventual port).

    Typed as a ``BaseModel`` so adapters can subclass with extra
    state; the route only calls :meth:`resolve`.
    """

    model_config = {"arbitrary_types_allowed": True}

    async def resolve(self, call_id: str, decision: ApprovalDecision) -> None:
        """Record the decision and wake the parked tool call.

        :raises NotFoundError: when ``call_id`` doesn't match a pending row.
        :raises Exception: any other failure is folded into a 500 envelope.
        """
        raise NotImplementedError


class NotFoundError(Exception):
    """Raised by :meth:`ApprovalGate.resolve` when ``call_id`` is unknown."""


# Convenience: callable surface (so tests can pass a lambda instead of
# subclassing :class:`ApprovalGate`).
ApprovalResolver = Callable[[str, ApprovalDecision], Awaitable[None]]


@dataclass(slots=True)
class ChatApproveState:
    """Route state. ``resolver=None`` → every request returns 503
    ``approvals_disabled`` (matches the Rust ``state.approval_gate ==
    None`` branch).
    """

    resolver: ApprovalResolver | None = None


# ─── Request / response wire shapes ──────────────────────────────────


class ApproveBody(BaseModel):
    """``POST /v1/chat/completions/{turn_id}/approve`` body."""

    call_id: str
    approved: bool
    scope: str | None = None
    deny_message: str | None = None


class ApproveResponse(BaseModel):
    """Successful response shape. Mirrors the Rust ``ApproveResponse`` struct."""

    turn_id: str
    call_id: str
    decision: str
    scope: str | None = Field(default=None)


# ─── Router ──────────────────────────────────────────────────────────


def router(state: ChatApproveState | None = None) -> APIRouter:
    """Build the per-turn approve sub-router."""
    api = APIRouter()
    effective = state or ChatApproveState()

    @api.post("/v1/chat/completions/{turn_id}/approve")
    async def handle_approve(turn_id: str, body: ApproveBody) -> JSONResponse:
        if effective.resolver is None:
            return JSONResponse(
                {
                    "error": "approvals_disabled",
                    "message": "approval gate is not configured on this gateway",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        call_id = body.call_id.strip()
        if not call_id:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "message": "`call_id` is required and must be non-empty",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if not body.approved and not (body.deny_message or "").strip():
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "message": "`deny_message` is required when approved=false",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        decision = (
            ApprovalDecision(kind="approved")
            if body.approved
            else ApprovalDecision(kind="denied", reason=body.deny_message or "")
        )
        label = decision.kind  # "approved" | "denied"

        try:
            await effective.resolver(call_id, decision)
        except NotFoundError:
            return JSONResponse(
                {
                    "error": "not_found",
                    "resource": "approval",
                    "call_id": call_id,
                    "turn_id": turn_id,
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": "approve_failed", "message": str(exc)},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return JSONResponse(
            ApproveResponse(
                turn_id=turn_id,
                call_id=call_id,
                decision=label,
                scope=body.scope,
            ).model_dump()
        )

    return api
