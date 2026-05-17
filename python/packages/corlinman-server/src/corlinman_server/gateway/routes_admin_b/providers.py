"""``/admin/providers*`` — provider registry CRUD.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/providers.rs``.

Routes:

* ``GET    /admin/providers``              — list every declared slot
  (kind, api-key source, ``params_schema``).
* ``POST   /admin/providers``              — upsert a provider slot.
* ``PATCH  /admin/providers/{name}``       — partial update.
* ``DELETE /admin/providers/{name}``       — refused with 409 when an
  alias or the ``[embedding]`` block still references it.

JSON-schema for ``params`` is pulled lazily from
``corlinman_providers`` (sibling package) so the Python source stays the
single source of truth — mirrors the Rust note that "Python wins" on
schema drift.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
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


class Capabilities(BaseModel):
    chat: bool = True
    embedding: bool = True


class ProviderView(BaseModel):
    name: str
    kind: str
    enabled: bool
    base_url: str | None = None
    api_key_source: str = "unset"
    api_key_env_name: str | None = None
    params: dict[str, Any] = {}
    params_schema: dict[str, Any] = {}
    capabilities: Capabilities = Capabilities()


class KindDescriptor(BaseModel):
    kind: str
    params_schema: dict[str, Any] = {}
    capabilities: Capabilities = Capabilities()


class ListOut(BaseModel):
    providers: list[ProviderView]
    kinds: list[KindDescriptor]


class ApiKeyEnv(BaseModel):
    env: str


class ApiKeyValue(BaseModel):
    value: str


class ProviderUpsert(BaseModel):
    name: str
    kind: str
    enabled: bool | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class ProviderPatch(BaseModel):
    kind: str | None = None
    enabled: bool | None = None
    base_url: str | None = None
    api_key: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_KNOWN_KINDS = (
    "openai",
    "anthropic",
    "google",
    "openai-compatible",
    "deepseek",
    "glm",
    "qwen",
    "newapi",
    "declarative",
)


def _kind_capabilities(kind: str) -> Capabilities:
    if kind == "anthropic":
        return Capabilities(chat=True, embedding=False)
    return Capabilities(chat=True, embedding=True)


def _params_schema_for(kind: str) -> dict[str, Any]:
    """Lazy lookup of ``corlinman_providers`` schema. Empty dict on miss."""
    try:
        from corlinman_providers import specs  # noqa: PLC0415

        getter = getattr(specs, "params_schema_for", None)
        if getter is not None:
            schema = getter(kind)
            if isinstance(schema, dict):
                return schema
    except (ImportError, AttributeError, Exception):  # noqa: BLE001
        pass
    return {"type": "object", "additionalProperties": True}


def _view_from_entry(name: str, entry: dict[str, Any]) -> ProviderView:
    api_key = entry.get("api_key")
    if api_key is None:
        source, env_name = "unset", None
    elif isinstance(api_key, dict) and "env" in api_key:
        source, env_name = "env", str(api_key["env"])
    elif isinstance(api_key, dict) and "value" in api_key:
        source, env_name = "value", None
    else:
        source, env_name = "value", None
    kind = str(entry.get("kind") or "openai-compatible").lower()
    return ProviderView(
        name=name,
        kind=kind,
        enabled=bool(entry.get("enabled", True)),
        base_url=entry.get("base_url"),
        api_key_source=source,
        api_key_env_name=env_name,
        params=dict(entry.get("params") or {}),
        params_schema=_params_schema_for(kind),
        capabilities=_kind_capabilities(kind),
    )


def _alias_target(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("model", ""))
    return ""


def _alias_provider(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("provider")
    return None


def _find_alias_refs(cfg: dict[str, Any], slot: str) -> list[str]:
    aliases = (cfg.get("models") or {}).get("aliases") or {}
    out: list[str] = []
    for name, entry in aliases.items():
        if _alias_provider(entry) == slot:
            out.append(str(name))
    return out


def _bad(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code, "message": message})


async def _persist(state: AdminState, cfg: dict[str, Any]) -> JSONResponse | None:
    if state.config_path is None:
        return JSONResponse(status_code=503, content={"error": "config_path_unset"})
    try:
        try:
            import tomli_w  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            import toml as tomli_w  # type: ignore  # noqa: PLC0415
        serialised = tomli_w.dumps(cfg)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": "serialise_failed", "message": str(exc)},
        )
    path = state.config_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "write_failed", "message": str(exc)},
        )
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "providers"])

    @r.get("/admin/providers", response_model=ListOut)
    async def list_providers():
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        providers: list[ProviderView] = []
        if isinstance(providers_cfg, dict):
            for name, entry in providers_cfg.items():
                if isinstance(entry, dict):
                    providers.append(_view_from_entry(str(name), entry))
        providers.sort(key=lambda p: p.name)
        kinds = [
            KindDescriptor(
                kind=k, params_schema=_params_schema_for(k), capabilities=_kind_capabilities(k)
            )
            for k in _KNOWN_KINDS
        ]
        return ListOut(providers=providers, kinds=kinds)

    @r.post("/admin/providers")
    async def upsert_provider(body: ProviderUpsert):
        if not body.name:
            return _bad("invalid_name", "provider name must be non-empty")
        if body.kind not in _KNOWN_KINDS:
            return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = dict(providers.get(body.name) or {})
            existing["kind"] = body.kind
            if body.enabled is not None:
                existing["enabled"] = body.enabled
            elif "enabled" not in existing:
                existing["enabled"] = True
            if body.base_url is not None:
                existing["base_url"] = body.base_url
            if body.api_key is not None:
                existing["api_key"] = body.api_key
            if body.params is not None:
                existing["params"] = body.params
            elif "params" not in existing:
                existing["params"] = {}
            providers[body.name] = existing
            cfg["providers"] = providers
            err = await _persist(state, cfg)
            if err is not None:
                return err
        return {"status": "ok", "provider": _view_from_entry(body.name, existing).model_dump()}

    @r.patch("/admin/providers/{name}")
    async def patch_provider(name: str, body: ProviderPatch):
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(name)
            if existing is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": name},
                )
            entry = dict(existing)
            if body.kind is not None:
                if body.kind not in _KNOWN_KINDS:
                    return _bad("invalid_kind", f"unknown provider kind: {body.kind}")
                entry["kind"] = body.kind
            if body.enabled is not None:
                entry["enabled"] = body.enabled
            if body.base_url is not None:
                entry["base_url"] = body.base_url
            if body.api_key is not None:
                entry["api_key"] = body.api_key
            if body.params is not None:
                entry["params"] = body.params
            providers[name] = entry
            cfg["providers"] = providers
            err = await _persist(state, cfg)
            if err is not None:
                return err
        return {"status": "ok", "provider": _view_from_entry(name, entry).model_dump()}

    @r.delete("/admin/providers/{name}")
    async def delete_provider(name: str):
        state = get_admin_state()
        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            if name not in providers:
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "resource": "provider", "id": name},
                )
            alias_refs = _find_alias_refs(cfg, name)
            emb = cfg.get("embedding") or {}
            emb_ref = emb.get("provider") == name
            if alias_refs or emb_ref:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "provider_in_use",
                        "alias_refs": alias_refs,
                        "embedding_uses": emb_ref,
                    },
                )
            providers.pop(name)
            cfg["providers"] = providers
            err = await _persist(state, cfg)
            if err is not None:
                return err
        return {"status": "ok", "removed": name}

    return r
