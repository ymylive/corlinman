"""``/admin/onboard*`` — stateless onboard-wizard endpoints.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/onboard.rs``.

Three routes:

* ``POST /admin/onboard/newapi/probe``    — UI step 2 connect-check.
* ``POST /admin/onboard/newapi/channels`` — UI step 3 channel picker.
* ``POST /admin/onboard/finalize``        — UI step 4 confirm; atomic
  write of ``[providers.newapi]`` + ``[models]`` + ``[embedding]`` and
  hot-swap of the in-memory snapshot.

The wizard is intentionally stateless server-side; the UI carries the
full triple ``(base_url, token, admin_token?)`` on every call.

Reuses :mod:`corlinman_newapi_client` (W1 port).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.newapi import (
    _atomic_swap_provider_entry,
    _build_client,
    _map_newapi_error,
)
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class ProbeBody(BaseModel):
    base_url: str
    token: str
    admin_token: str | None = None


class ProbeResponse(BaseModel):
    next: str = "models"
    base_url: str
    user: dict[str, Any]
    server_version: str | None = None
    channels_available: int = 0


class ChannelsBody(BaseModel):
    base_url: str
    token: str
    admin_token: str | None = None
    type: str = Field(..., description="llm | embedding | tts")


class ModelPick(BaseModel):
    channel_id: int | None = None
    model: str


class EmbeddingPick(BaseModel):
    channel_id: int | None = None
    model: str
    dimension: int = 1536


class TtsPick(BaseModel):
    channel_id: int | None = None
    model: str
    voice: str | None = None


class FinalizeBody(BaseModel):
    base_url: str
    token: str
    admin_token: str | None = None
    llm: ModelPick
    embedding: EmbeddingPick
    tts: TtsPick | None = None


class FinalizeResponse(BaseModel):
    ok: bool = True
    redirect: str = "/login"


def _bad(code: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "onboard"])

    @r.post("/admin/onboard/newapi/probe", response_model=ProbeResponse)
    async def post_probe(body: ProbeBody):
        client, err = await _build_client(body.base_url, body.token, body.admin_token)
        if err is not None:
            return err
        try:
            probe = await client.probe()
        except Exception as exc:  # noqa: BLE001
            return _bad(_map_newapi_error(exc))
        try:
            from corlinman_newapi_client import ChannelType  # noqa: PLC0415
            channels = await client.list_channels(getattr(ChannelType, "LLM", "llm"))
            count = len(list(channels))
        except Exception:  # noqa: BLE001
            count = 0
        return ProbeResponse(
            base_url=str(getattr(probe, "base_url", body.base_url)),
            user=dict(getattr(probe, "user", {}) or {}),
            server_version=getattr(probe, "server_version", None),
            channels_available=count,
        )

    @r.post("/admin/onboard/newapi/channels")
    async def post_channels(body: ChannelsBody):
        try:
            from corlinman_newapi_client import ChannelType  # noqa: PLC0415
        except ImportError:
            return _bad("invalid_channel_type")
        ct_map = {
            "llm": getattr(ChannelType, "LLM", "llm"),
            "embedding": getattr(ChannelType, "EMBEDDING", "embedding"),
            "tts": getattr(ChannelType, "TTS", "tts"),
        }
        ct = ct_map.get(body.type)
        if ct is None:
            return _bad("invalid_channel_type")
        client, err = await _build_client(body.base_url, body.token, body.admin_token)
        if err is not None:
            return err
        try:
            channels = await client.list_channels(ct)
        except Exception as exc:  # noqa: BLE001
            return _bad(_map_newapi_error(exc))
        return {
            "channels": [
                ch if isinstance(ch, dict) else getattr(ch, "__dict__", {"value": str(ch)})
                for ch in channels or []
            ]
        }

    @r.post("/admin/onboard/finalize", response_model=FinalizeResponse)
    async def post_finalize(body: FinalizeBody):
        state = get_admin_state()

        # Re-probe before persisting (rejects stale credentials).
        client, err = await _build_client(body.base_url, body.token, body.admin_token)
        if err is not None:
            return err
        try:
            await client.probe()
        except Exception as exc:  # noqa: BLE001
            return _bad(_map_newapi_error(exc))

        if state.config_path is None:
            return JSONResponse(
                status_code=503,
                content={"error": "config_path_unset"},
            )

        # Build the newapi provider entry.
        admin_url = body.base_url.rstrip("/") + "/api"
        params: dict[str, Any] = {"newapi_admin_url": admin_url}
        if body.admin_token is not None:
            params["newapi_admin_key"] = {"value": body.admin_token}
        if body.tts is not None:
            params["newapi_tts_model"] = body.tts.model
            if body.tts.voice is not None:
                params["newapi_tts_voice"] = body.tts.voice
            if body.tts.channel_id is not None:
                params["newapi_tts_channel_id"] = body.tts.channel_id
        if body.llm.channel_id is not None:
            params["newapi_llm_channel_id"] = body.llm.channel_id
        if body.embedding.channel_id is not None:
            params["newapi_embedding_channel_id"] = body.embedding.channel_id

        new_entry = {
            "kind": "newapi",
            "api_key": {"value": body.token},
            "base_url": body.base_url,
            "enabled": True,
            "params": params,
        }

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            providers["newapi"] = new_entry
            cfg["providers"] = providers

            # [models] default + alias.
            models_cfg = dict(cfg.get("models") or {})
            models_cfg["default"] = body.llm.model
            aliases = dict(models_cfg.get("aliases") or {})
            aliases[body.llm.model] = {
                "model": body.llm.model,
                "provider": "newapi",
                "params": {},
            }
            models_cfg["aliases"] = aliases
            cfg["models"] = models_cfg

            # [embedding].
            cfg["embedding"] = {
                "provider": "newapi",
                "model": body.embedding.model,
                "dimension": body.embedding.dimension,
                "enabled": True,
                "params": {},
            }

            ok, resp = await _atomic_swap_provider_entry(state, "newapi", new_entry)
            if not ok:
                return resp
            # The helper only swapped providers; finalize also writes
            # models + embedding which require a full re-dump. Do a
            # second pass using the same TOML writer + atomic rename.
            try:
                try:
                    import tomli_w  # noqa: PLC0415
                except ImportError:  # pragma: no cover
                    import toml as tomli_w  # type: ignore  # noqa: PLC0415
                serialised = tomli_w.dumps(cfg)  # type: ignore[attr-defined]
                path = state.config_path
                tmp = path.with_suffix(path.suffix + ".new")
                tmp.write_text(serialised, encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                return JSONResponse(
                    status_code=500,
                    content={"error": "write_failed", "message": str(exc)},
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    status_code=500,
                    content={"error": "serialise_failed", "message": str(exc)},
                )

        return FinalizeResponse()

    return r
