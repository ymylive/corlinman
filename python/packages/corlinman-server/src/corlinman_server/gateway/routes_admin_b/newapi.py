"""``/admin/newapi*`` — admin connector for the new-api / QuantumNous sidecar.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/newapi.rs``.

Routes:

* ``GET    /admin/newapi``          — masked summary of the active
  ``providers.newapi`` slot.
* ``GET    /admin/newapi/channels?type={llm|embedding|tts}`` — live
  channel list via the new-api admin API.
* ``POST   /admin/newapi/probe``    — validate a candidate connection
  triple without persisting.
* ``POST   /admin/newapi/test``     — 1-token round-trip against the
  active connection.
* ``PATCH  /admin/newapi``          — partial connection update with
  re-probe before write. Requires :attr:`AdminState.config_path` set.

Reuses :mod:`corlinman_newapi_client` (W1 port).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class ConnectionView(BaseModel):
    base_url: str
    token_masked: str
    admin_key_present: bool
    enabled: bool


class Summary(BaseModel):
    connection: ConnectionView
    status: str


class ProbeBody(BaseModel):
    base_url: str
    token: str
    admin_token: str | None = None


class ProbeResponse(BaseModel):
    base_url: str
    user: dict[str, Any]
    server_version: str | None = None


class TestBody(BaseModel):
    model: str


class PatchBody(BaseModel):
    base_url: str | None = None
    token: str | None = None
    admin_token: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def _find_newapi(cfg: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """First enabled ``kind = "newapi"`` slot in the providers map."""
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(providers, dict):
        return None
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "")).lower()
        enabled = bool(entry.get("enabled", True))
        if kind == "newapi" and enabled:
            return name, entry
    return None


def _resolve_secret(secret: Any) -> str | None:
    """Best-effort resolution of a config-shaped secret (env/literal)."""
    if secret is None:
        return None
    if isinstance(secret, str):
        return secret
    if isinstance(secret, dict):
        if "value" in secret:
            return str(secret["value"])
        if "env" in secret:
            import os  # noqa: PLC0415

            return os.environ.get(str(secret["env"]))
    return None


def _map_newapi_error(exc: Exception) -> str:
    """Translate corlinman_newapi_client exceptions to the Rust error code
    taxonomy. Falls back to ``newapi_upstream_error`` for the unknown set."""
    name = type(exc).__name__
    if name == "UpstreamError":
        status = getattr(exc, "status", None)
        if status == 401:
            return "newapi_token_invalid"
        if status == 403:
            return "newapi_admin_required"
        return "newapi_upstream_error"
    if name == "NotNewapiError":
        return "newapi_version_too_old"
    if name == "HttpError":
        return "newapi_unreachable"
    if name == "UrlError":
        return "newapi_bad_url"
    if name == "JsonError":
        return "newapi_upstream_error"
    return "newapi_upstream_error"


def _bad_request(code: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code})


def _no_provider() -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "no_newapi_provider"})


async def _build_client(base_url: str, token: str, admin_token: str | None):
    """Construct a :class:`corlinman_newapi_client.NewapiClient`.

    Returns ``(client, None)`` on success, ``(None, JSONResponse)`` on
    failure — caller short-circuits with the response.
    """
    try:
        from corlinman_newapi_client import NewapiClient  # noqa: PLC0415
    except ImportError:
        return None, JSONResponse(
            status_code=503,
            content={
                "error": "newapi_unreachable",
                "message": "corlinman_newapi_client not installed",
            },
        )
    try:
        client = NewapiClient(base_url=base_url, token=token, admin_token=admin_token)
    except Exception:  # noqa: BLE001
        return None, _bad_request("newapi_bad_url")
    return client, None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "newapi"])

    @r.get("/admin/newapi", response_model=Summary)
    async def get_summary():
        cfg = dict(config_snapshot())
        found = _find_newapi(cfg)
        if found is None:
            return _no_provider()
        _, entry = found
        token = _resolve_secret(entry.get("api_key")) or ""
        params = entry.get("params") or {}
        admin_key_present = bool(params.get("newapi_admin_key"))
        return Summary(
            connection=ConnectionView(
                base_url=str(entry.get("base_url") or ""),
                token_masked=_mask_token(token),
                admin_key_present=admin_key_present,
                enabled=bool(entry.get("enabled", True)),
            ),
            status="ok",
        )

    @r.post("/admin/newapi/probe", response_model=ProbeResponse)
    async def post_probe(body: ProbeBody):
        client, err = await _build_client(body.base_url, body.token, body.admin_token)
        if err is not None:
            return err
        try:
            result = await client.probe()
        except Exception as exc:  # noqa: BLE001
            return _bad_request(_map_newapi_error(exc))
        # Result shape per :class:`ProbeResult`.
        return ProbeResponse(
            base_url=str(getattr(result, "base_url", body.base_url)),
            user=dict(getattr(result, "user", {}) or {}),
            server_version=getattr(result, "server_version", None),
        )

    @r.get("/admin/newapi/channels")
    async def get_channels(type: str = Query(..., alias="type")):
        cfg = dict(config_snapshot())
        found = _find_newapi(cfg)
        if found is None:
            return _no_provider()
        _, entry = found
        base_url = entry.get("base_url")
        if not base_url:
            return _bad_request("newapi_missing_base_url")
        token = _resolve_secret(entry.get("api_key")) or ""
        admin_tok = _resolve_secret((entry.get("params") or {}).get("newapi_admin_key"))

        try:
            from corlinman_newapi_client import ChannelType  # noqa: PLC0415
        except ImportError:
            return _bad_request("invalid_channel_type")

        ct_map = {
            "llm": getattr(ChannelType, "LLM", "llm"),
            "embedding": getattr(ChannelType, "EMBEDDING", "embedding"),
            "tts": getattr(ChannelType, "TTS", "tts"),
        }
        ct = ct_map.get(type)
        if ct is None:
            return _bad_request("invalid_channel_type")

        client, err = await _build_client(str(base_url), token, admin_tok)
        if err is not None:
            return err
        try:
            channels = await client.list_channels(ct)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"error": _map_newapi_error(exc)},
            )
        return {"channels": _serialise_channels(channels)}

    @r.post("/admin/newapi/test")
    async def post_test(body: TestBody):
        cfg = dict(config_snapshot())
        found = _find_newapi(cfg)
        if found is None:
            return _no_provider()
        _, entry = found
        base_url = entry.get("base_url")
        if not base_url:
            return _bad_request("newapi_missing_base_url")
        token = _resolve_secret(entry.get("api_key")) or ""

        client, err = await _build_client(str(base_url), token, None)
        if err is not None:
            return err
        try:
            result = await client.test_round_trip(body.model)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            body_text = getattr(exc, "body", None)
            payload: dict[str, Any] = {"error": "newapi_test_failed"}
            if status is not None:
                payload["upstream_status"] = status
                payload["body"] = body_text
            else:
                payload["detail"] = str(exc)
            return JSONResponse(status_code=502, content=payload)

        return {
            "status": getattr(result, "status", "ok"),
            "latency_ms": getattr(result, "latency_ms", 0),
            "model": getattr(result, "model", body.model),
        }

    @r.patch("/admin/newapi")
    async def patch_connection(body: PatchBody):
        state = get_admin_state()
        cfg = dict(config_snapshot())
        found = _find_newapi(cfg)
        if found is None:
            return _no_provider()
        name, entry = found

        # Apply the partial update onto a shallow clone of the entry.
        new_entry: dict[str, Any] = dict(entry)
        if body.base_url is not None:
            new_entry["base_url"] = body.base_url
        if body.token is not None:
            new_entry["api_key"] = {"value": body.token}
        if body.admin_token is not None:
            params = dict(new_entry.get("params") or {})
            params["newapi_admin_key"] = {"value": body.admin_token}
            new_entry["params"] = params

        # Re-probe before persisting.
        url = str(new_entry.get("base_url") or "")
        token = _resolve_secret(new_entry.get("api_key")) or ""
        admin_tok = _resolve_secret(
            (new_entry.get("params") or {}).get("newapi_admin_key")
        )
        client, err = await _build_client(url, token, admin_tok)
        if err is not None:
            return err
        try:
            await client.probe()
        except Exception as exc:  # noqa: BLE001
            return _bad_request(_map_newapi_error(exc))

        # Persist atomically. Needs a config_path.
        if state.config_path is None:
            return JSONResponse(
                status_code=503,
                content={"error": "config_path_unset"},
            )

        async with state.admin_write_lock:
            ok, resp = await _atomic_swap_provider_entry(state, name, new_entry)
            if not ok:
                return resp
        return {"ok": True}

    return r


# ---------------------------------------------------------------------------
# Helpers (atomic config swap + channel serialisation)
# ---------------------------------------------------------------------------


def _serialise_channels(channels: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ch in channels or []:
        if isinstance(ch, dict):
            out.append(ch)
        else:
            # Dataclass / pydantic model.
            try:
                out.append(dict(ch.__dict__))
            except AttributeError:
                try:
                    out.append(ch.model_dump())  # type: ignore[attr-defined]
                except AttributeError:
                    out.append({"value": str(ch)})
    return out


async def _atomic_swap_provider_entry(
    state: AdminState, name: str, new_entry: dict[str, Any]
) -> tuple[bool, JSONResponse]:
    """Re-serialise the active config TOML with one provider slot
    swapped + atomic rename.

    Returns ``(True, _)`` on success; the second slot is undefined.
    Returns ``(False, JSONResponse)`` on failure.
    """
    cfg = dict(config_snapshot())
    providers = dict(cfg.get("providers") or {})
    providers[name] = new_entry
    cfg["providers"] = providers

    try:
        import tomli_w  # noqa: PLC0415
    except ImportError:
        try:
            import toml as tomli_w  # type: ignore  # noqa: PLC0415,F401
        except ImportError:
            return False, JSONResponse(
                status_code=500,
                content={
                    "error": "serialise_failed",
                    "message": "no TOML writer (tomli_w / toml) available",
                },
            )

    try:
        serialised = tomli_w.dumps(cfg)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return False, JSONResponse(
            status_code=500,
            content={"error": "serialise_failed", "message": str(exc)},
        )

    path = state.config_path
    if path is None:  # pragma: no cover — guarded upstream
        return False, JSONResponse(
            status_code=503, content={"error": "config_path_unset"}
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return False, JSONResponse(
            status_code=500, content={"error": "write_failed", "message": str(exc)}
        )
    return True, JSONResponse(status_code=200, content={"ok": True})
