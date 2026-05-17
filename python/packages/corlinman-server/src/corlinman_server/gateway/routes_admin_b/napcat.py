"""``/admin/channels/qq/*`` — NapCat webui proxy + account history.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/napcat.rs``.

Routes:

* ``POST /admin/channels/qq/qrcode``        — fetch QR from NapCat.
* ``GET  /admin/channels/qq/qrcode/status`` — poll login status.
* ``GET  /admin/channels/qq/accounts``      — history from
  ``<data_dir>/qq-accounts.json``.
* ``POST /admin/channels/qq/quick-login``   — re-use a stored session.

NapCat URL resolution order matches Rust:

1. ``[channels.qq].napcat_url`` from config.
2. ``CORLINMAN_NAPCAT_URL`` env.
3. 503 ``napcat_not_configured``.

Authentication: NapCat 2.x exchanges
``POST /api/auth/login {"hash": sha256(token + ".napcat")}`` for a
short-lived ``Credential`` we then send as ``Bearer``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json as json_lib
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)

ACCOUNTS_FILE = "qq-accounts.json"
NAPCAT_TIMEOUT = 6.0


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class QqAccount(BaseModel):
    uin: str
    nickname: str | None = None
    avatar_url: str | None = None
    last_login_at: int


class QrcodeOut(BaseModel):
    token: str
    image_base64: str | None = None
    qrcode_url: str | None = None
    expires_at: int


class StatusOut(BaseModel):
    status: str
    account: QqAccount | None = None
    message: str | None = None


class AccountsOut(BaseModel):
    accounts: list[QqAccount]


class QuickLoginBody(BaseModel):
    uin: str


# ---------------------------------------------------------------------------
# NapCat client
# ---------------------------------------------------------------------------


class NapcatError(Exception):
    """Generic NapCat call failure with optional upstream metadata."""

    def __init__(self, code: str, message: str = "", status: int | None = None):
        super().__init__(message or code)
        self.code = code
        self.upstream_status = status

    def response(self) -> JSONResponse:
        status = self.upstream_status if self.upstream_status else 502
        return JSONResponse(
            status_code=status,
            content={
                "error": self.code,
                "message": str(self),
            },
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _resolve_napcat_url(cfg: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(url, access_token)`` or ``(None, _)`` when not configured."""
    qq = ((cfg.get("channels") or {}).get("qq")) or {}
    url = qq.get("napcat_url")
    if not url or not str(url).strip():
        url = os.environ.get("CORLINMAN_NAPCAT_URL")
    if not url or not str(url).strip():
        return None, None
    url = str(url).rstrip("/")
    access_token: str | None = None
    sec = qq.get("napcat_access_token")
    if isinstance(sec, dict):
        if "value" in sec:
            access_token = str(sec["value"])
        elif "env" in sec:
            access_token = os.environ.get(str(sec["env"]))
    elif isinstance(sec, str) and sec:
        access_token = sec
    return url, access_token


def _resolve_data_dir(state: AdminState, cfg: dict[str, Any]) -> Path:
    if state.data_dir is not None:
        return state.data_dir
    server = cfg.get("server") or {}
    if isinstance(server.get("data_dir"), str):
        return Path(server["data_dir"])
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".corlinman"


def _accounts_path(state: AdminState, cfg: dict[str, Any]) -> Path:
    return _resolve_data_dir(state, cfg) / ACCOUNTS_FILE


def _classify_qr(qr: str) -> tuple[str | None, str | None]:
    trimmed = qr.strip()
    if trimmed.startswith("http://") or trimmed.startswith("https://"):
        return None, trimmed
    for prefix in ("data:image/png;base64,", "data:image/jpeg;base64,"):
        if trimmed.startswith(prefix):
            return trimmed[len(prefix):], None
    return trimmed, None


def _extract_ok_data(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise NapcatError("napcat_bad_response", "non-object envelope")
    code = body.get("code", -1)
    if code != 0:
        raise NapcatError(
            "napcat_app_error",
            str(body.get("message") or "napcat returned a non-zero code"),
        )
    data = body.get("data")
    if data is None:
        raise NapcatError("napcat_bad_response", "missing data field")
    return data if isinstance(data, dict) else {"value": data}


def _parse_account(data: dict[str, Any]) -> QqAccount | None:
    uin = data.get("uin")
    if uin is None:
        return None
    uin = str(uin)
    nickname = data.get("nick") or data.get("nickName")
    avatar = data.get("avatarUrl") or data.get("avatar")
    return QqAccount(
        uin=uin,
        nickname=nickname if isinstance(nickname, str) else None,
        avatar_url=avatar if isinstance(avatar, str) else None,
        last_login_at=_now_ms(),
    )


class _NapcatClient:
    def __init__(self, base_url: str, access_token: str | None):
        self.base_url = base_url
        self.access_token = access_token
        self._client = httpx.AsyncClient(timeout=NAPCAT_TIMEOUT)
        self._credential: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> _NapcatClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _login(self) -> str | None:
        if not self.access_token:
            return None
        if self._credential is not None:
            return self._credential
        h = hashlib.sha256()
        h.update(self.access_token.encode("utf-8"))
        h.update(b".napcat")
        hash_hex = h.hexdigest()
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/auth/login",
                json={"hash": hash_hex},
            )
        except httpx.HTTPError as exc:
            raise NapcatError("napcat_unreachable", str(exc), status=503) from exc
        if resp.status_code >= 400:
            raise NapcatError(
                "napcat_unreachable", resp.text, status=503
            )
        try:
            data = _extract_ok_data(resp.json())
        except json_lib.JSONDecodeError as exc:
            raise NapcatError("napcat_bad_response", str(exc)) from exc
        credential = data.get("Credential")
        if not credential:
            raise NapcatError("napcat_bad_response", "missing data.Credential")
        self._credential = str(credential)
        return self._credential

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        credential = await self._login()
        headers = {"Authorization": f"Bearer {credential}"} if credential else {}
        try:
            resp = await self._client.post(
                f"{self.base_url}{path}", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise NapcatError("napcat_unreachable", str(exc), status=503) from exc
        if resp.status_code >= 400:
            raise NapcatError(
                "napcat_upstream_error",
                resp.text,
                status=502,
            )
        try:
            payload = resp.json()
        except json_lib.JSONDecodeError as exc:
            raise NapcatError("napcat_bad_response", str(exc)) from exc
        return _extract_ok_data(payload)

    async def request_qrcode(self) -> QrcodeOut:
        # Force a refresh — older builds will 404; we swallow that.
        try:
            await self.post("/api/QQLogin/RefreshQRcode", {})
        except NapcatError:
            pass
        data = await self.post("/api/QQLogin/GetQQLoginQrcode", {})
        qr = data.get("qrcode")
        if not isinstance(qr, str):
            raise NapcatError("napcat_bad_response", "missing data.qrcode")
        image, url = _classify_qr(qr)
        return QrcodeOut(
            token=str(uuid.uuid4()),
            image_base64=image,
            qrcode_url=url,
            expires_at=_now_ms() + 120_000,
        )

    async def check_status(self) -> StatusOut:
        data = await self.post("/api/QQLogin/CheckLoginStatus", {})
        if data.get("isLogin"):
            return StatusOut(status="confirmed", account=_parse_account(data))
        qr_url = data.get("qrcodeurl") or ""
        return StatusOut(status="expired" if not qr_url else "waiting")

    async def quick_login(self, uin: str) -> StatusOut:
        data = await self.post("/api/QQLogin/SetQuickLogin", {"uin": uin})
        is_login = data.get("isLogin", True)
        account = _parse_account(data) or QqAccount(
            uin=uin, last_login_at=_now_ms()
        )
        return StatusOut(
            status="confirmed" if is_login else "error",
            account=account,
        )


# ---------------------------------------------------------------------------
# Accounts file helpers
# ---------------------------------------------------------------------------


_ACCOUNTS_LOCK = asyncio.Lock()


async def _load_accounts(path: Path) -> list[QqAccount]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        raw = json_lib.loads(text)
    except (OSError, json_lib.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[QqAccount] = []
    for item in raw:
        if isinstance(item, dict) and "uin" in item:
            out.append(
                QqAccount(
                    uin=str(item["uin"]),
                    nickname=item.get("nickname"),
                    avatar_url=item.get("avatar_url"),
                    last_login_at=int(item.get("last_login_at", 0) or 0),
                )
            )
    return out


async def _upsert_account(path: Path, acct: QqAccount) -> None:
    async with _ACCOUNTS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = await _load_accounts(path)
        out: list[QqAccount] = []
        updated = False
        for a in existing:
            if a.uin == acct.uin:
                out.append(
                    QqAccount(
                        uin=a.uin,
                        nickname=acct.nickname or a.nickname,
                        avatar_url=acct.avatar_url or a.avatar_url,
                        last_login_at=acct.last_login_at,
                    )
                )
                updated = True
            else:
                out.append(a)
        if not updated:
            out.append(acct)
        out.sort(key=lambda a: a.last_login_at, reverse=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(
            json_lib.dumps([a.model_dump() for a in out], indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "napcat"])

    def _build_client(state: AdminState) -> tuple[_NapcatClient | None, JSONResponse | None, Path]:
        cfg = dict(config_snapshot())
        url, token = _resolve_napcat_url(cfg)
        path = _accounts_path(state, cfg)
        if url is None:
            return None, JSONResponse(
                status_code=503,
                content={
                    "error": "napcat_not_configured",
                    "message": (
                        "[channels.qq].napcat_url is empty;"
                        " set it in config.toml or export CORLINMAN_NAPCAT_URL"
                    ),
                },
            ), path
        return _NapcatClient(url, token), None, path

    @r.post("/admin/channels/qq/qrcode", response_model=QrcodeOut)
    async def qrcode():
        state = get_admin_state()
        client, err, _path = _build_client(state)
        if err is not None:
            return err
        try:
            async with client:  # type: ignore[union-attr]
                return await client.request_qrcode()
        except NapcatError as exc:
            return exc.response()

    @r.get("/admin/channels/qq/qrcode/status", response_model=StatusOut)
    async def qrcode_status(token: str = Query("")):
        state = get_admin_state()
        client, err, path = _build_client(state)
        if err is not None:
            return err
        try:
            async with client:  # type: ignore[union-attr]
                out = await client.check_status()
        except NapcatError as exc:
            return exc.response()
        if out.account is not None:
            try:
                await _upsert_account(path, out.account)
            except OSError:
                pass
        return out

    @r.get("/admin/channels/qq/accounts", response_model=AccountsOut)
    async def accounts():
        state = get_admin_state()
        cfg = dict(config_snapshot())
        path = _accounts_path(state, cfg)
        try:
            accts = await _load_accounts(path)
        except OSError as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "accounts_read_failed",
                    "message": f"failed to read {path}: {exc}",
                },
            )
        return AccountsOut(accounts=accts)

    @r.post("/admin/channels/qq/quick-login", response_model=StatusOut)
    async def quick_login(body: QuickLoginBody):
        if not body.uin.strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_uin", "message": "uin is required"},
            )
        state = get_admin_state()
        client, err, path = _build_client(state)
        if err is not None:
            return err
        try:
            async with client:  # type: ignore[union-attr]
                out = await client.quick_login(body.uin)
        except NapcatError as exc:
            return exc.response()
        if out.account is not None:
            try:
                await _upsert_account(path, out.account)
            except OSError:
                pass
        return out

    return r
