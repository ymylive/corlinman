"""``POST /v1/channels/telegram/webhook`` — Telegram Bot webhook.

Python port of ``rust/crates/corlinman-gateway/src/routes/channels.rs``.
Mirrors the Rust contract:

* Authenticate the incoming POST with the
  ``X-Telegram-Bot-Api-Secret-Token`` header (constant-time compare).
  Mismatch → 401 ``unauthorized``.
* Decode the body as a Telegram :class:`Update`. Decode failure →
  400 ``invalid_update``.
* Hand the parsed update to
  :func:`corlinman_channels.process_update`. Success → 200 ``{ok: true}``.
* On processing failure we still return 200 ``{ok: false}`` so
  Telegram doesn't retry the same update indefinitely (matches the
  Rust ``warn + 200`` recovery path).

State is bundled in :class:`TelegramWebhookState` (mirrors the Rust
``TelegramWebhookState`` struct). Hot-swapping the secret requires a
gateway restart — same constraint the Rust route documents.

Route URL: the Rust file mounts ``/channels/telegram/webhook``. The
Python port mounts the canonical ``/v1/channels/telegram/webhook``
plus the legacy ``/channels/telegram/webhook`` so existing Telegram
webhook registrations keep working through the port.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse

__all__ = ["SECRET_HEADER", "TelegramWebhookState", "router"]

#: Header Telegram echoes back for webhook authentication.
#: Reference: ``core.telegram.org/bots/api#setwebhook``.
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@dataclass(slots=True)
class TelegramWebhookState:
    """Shared state for the Telegram webhook route.

    Mirrors the Rust ``TelegramWebhookState`` struct one-to-one.

    * ``secret_token`` — expected value from ``[telegram.webhook].secret_token``.
      Empty string disables the check (useful for local dev tunnels
      that strip the header; production boot logs a warning when it
      sees this).
    * ``bot_id`` / ``bot_username`` — used by
      :func:`corlinman_channels.process_update` to classify message
      route (mention / private / DM-only).
    * ``data_dir`` — base data directory; the webhook downloads
      media into ``<data_dir>/media/telegram/...``.
    * ``http`` — :class:`corlinman_channels.TelegramHttp` for media
      download + outbound replies.
    * ``hooks`` — optional :class:`corlinman_hooks.HookBus` for
      observability fan-out.
    """

    secret_token: str
    bot_id: int
    bot_username: str | None
    data_dir: Path
    http: Any  # corlinman_channels.TelegramHttp (Protocol)
    hooks: Any = None


def _verify_secret(configured: str, got: str | None) -> bool:
    """Constant-time secret comparison.

    Empty ``configured`` disables the check (mirrors the Rust
    ``verify_secret`` behaviour).
    """
    if not configured:
        return True
    return hmac.compare_digest(configured.encode(), (got or "").encode())


def router(state: TelegramWebhookState) -> APIRouter:
    """Build the Telegram webhook sub-router."""
    api = APIRouter()

    async def _handle(request: Request, secret_header: str | None) -> JSONResponse:
        if not _verify_secret(state.secret_token, secret_header):
            return JSONResponse(
                {
                    "error": "unauthorized",
                    "message": f"{SECRET_HEADER} mismatch",
                },
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {
                    "error": "invalid_update",
                    "message": f"could not decode JSON body: {exc}",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # Lazy import — the gateway boot path may not have
        # corlinman_channels installed in every test environment, and
        # a missing dep should surface as 503 on this route, not as
        # an import error at gateway startup.
        try:
            from corlinman_channels.telegram import Update  # noqa: PLC0415
            from corlinman_channels.telegram_webhook import (  # noqa: PLC0415
                WebhookCtx,
                process_update,
            )
        except ImportError as exc:
            return JSONResponse(
                {
                    "error": "channel_unavailable",
                    "message": f"corlinman_channels not available: {exc}",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            update = Update.from_json(body) if hasattr(Update, "from_json") else Update(**body)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {
                    "error": "invalid_update",
                    "message": str(exc),
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        ctx = WebhookCtx(
            bot_id=state.bot_id,
            bot_username=state.bot_username,
            data_dir=state.data_dir,
            http=state.http,
            hooks=state.hooks,
        )

        try:
            await process_update(ctx, update)
        except Exception:  # noqa: BLE001
            # Match the Rust behaviour: log + return 200 {ok: false}
            # so Telegram doesn't retry indefinitely.
            return JSONResponse({"ok": False})

        return JSONResponse({"ok": True})

    @api.post("/v1/channels/telegram/webhook")
    async def telegram_webhook_v1(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> JSONResponse:
        """Canonical ``/v1/*`` route."""
        return await _handle(request, x_telegram_bot_api_secret_token)

    @api.post("/channels/telegram/webhook")
    async def telegram_webhook_legacy(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> JSONResponse:
        """Legacy Rust-compatible route alias."""
        return await _handle(request, x_telegram_bot_api_secret_token)

    return api
