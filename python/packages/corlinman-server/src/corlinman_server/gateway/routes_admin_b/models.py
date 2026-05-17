"""``/admin/models*`` — model routing / alias management.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/models.rs``.

Three routes:

* ``GET    /admin/models``                  — provider + alias snapshot.
* ``POST   /admin/models/aliases``          — single upsert *or* bulk
  replace (untagged union body).
* ``DELETE /admin/models/aliases/{name}``   — drop one alias.

Mutation routes atomic-write the active config TOML — requires
:attr:`AdminState.config_path`.
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


class ProviderRow(BaseModel):
    name: str
    enabled: bool
    has_api_key: bool
    api_key_kind: str | None = None
    base_url: str | None = None
    kind: str | None = None


class AliasRow(BaseModel):
    name: str
    model: str
    provider: str | None = None
    params: dict[str, Any] = {}


class ModelsResponse(BaseModel):
    default: str
    aliases: list[AliasRow]
    providers: list[ProviderRow]


class AliasUpsert(BaseModel):
    name: str
    model: str
    provider: str | None = None
    params: dict[str, Any] | None = None


class BulkAliases(BaseModel):
    aliases: dict[str, str]
    default: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bad(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": code, "message": message})


def _alias_entry_to_dict(alias: Any) -> tuple[str, str | None, dict[str, Any]]:
    """Coerce ``aliases[name]`` (which may be a string shorthand or a
    full dict) into ``(model, provider, params)``."""
    if isinstance(alias, str):
        return alias, None, {}
    if isinstance(alias, dict):
        return (
            str(alias.get("model", "")),
            alias.get("provider"),
            dict(alias.get("params") or {}),
        )
    return str(alias), None, {}


def _provider_row(name: str, entry: dict[str, Any]) -> ProviderRow:
    api_key = entry.get("api_key")
    if api_key is None:
        has = False
        kind = None
    elif isinstance(api_key, dict) and "env" in api_key:
        has = True
        kind = "env"
    elif isinstance(api_key, dict) and "value" in api_key:
        has = True
        kind = "literal"
    elif isinstance(api_key, str):
        has = True
        kind = "literal"
    else:
        has = False
        kind = None
    resolved_kind = entry.get("kind") if isinstance(entry.get("kind"), str) else None
    return ProviderRow(
        name=name,
        enabled=bool(entry.get("enabled", True)),
        has_api_key=has,
        api_key_kind=kind,
        base_url=entry.get("base_url"),
        kind=resolved_kind,
    )


async def _persist_alias_swap(state: AdminState, new_models: dict[str, Any]) -> JSONResponse | None:
    """Atomic-write of just the ``[models]`` section. Returns ``None`` on
    success, a ``JSONResponse`` on failure."""
    if state.config_path is None:
        return JSONResponse(
            status_code=503, content={"error": "config_path_unset"}
        )
    try:
        try:
            import tomli_w  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            import toml as tomli_w  # type: ignore  # noqa: PLC0415
    except ImportError:
        return JSONResponse(
            status_code=500,
            content={"error": "serialise_failed", "message": "no toml writer"},
        )

    cfg = dict(config_snapshot())
    cfg["models"] = new_models
    try:
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
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "models"])

    @r.get("/admin/models", response_model=ModelsResponse)
    async def list_models():
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        providers: list[ProviderRow] = []
        if isinstance(providers_cfg, dict):
            for name, entry in providers_cfg.items():
                if isinstance(entry, dict):
                    providers.append(_provider_row(str(name), entry))
        models_cfg = cfg.get("models") or {}
        aliases_map = models_cfg.get("aliases") or {}
        aliases: list[AliasRow] = []
        if isinstance(aliases_map, dict):
            for name, entry in aliases_map.items():
                model, provider, params = _alias_entry_to_dict(entry)
                aliases.append(
                    AliasRow(name=str(name), model=model, provider=provider, params=params)
                )
        aliases.sort(key=lambda a: a.name)
        return ModelsResponse(
            default=str(models_cfg.get("default", "")),
            aliases=aliases,
            providers=providers,
        )

    @r.post("/admin/models/aliases")
    async def upsert_aliases(body: dict[str, Any]):
        # Untagged-union: try single shape first, then bulk.
        if "name" in body and "model" in body:
            try:
                single = AliasUpsert.model_validate(body)
            except Exception as exc:  # noqa: BLE001
                return _bad("invalid_body", str(exc))
            return await _apply_single(single)
        if "aliases" in body:
            try:
                bulk = BulkAliases.model_validate(body)
            except Exception as exc:  # noqa: BLE001
                return _bad("invalid_body", str(exc))
            return await _apply_bulk(bulk)
        return _bad("invalid_body", "body must be either {name, model} or {aliases}")

    @r.delete("/admin/models/aliases/{name}")
    async def delete_alias(name: str):
        state = get_admin_state()
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        aliases = dict(models_cfg.get("aliases") or {})
        if name not in aliases:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "resource": "alias", "id": name},
            )
        aliases.pop(name)
        models_cfg["aliases"] = aliases
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        return {"status": "ok", "removed": name}

    async def _apply_single(up: AliasUpsert) -> Any:
        if not up.name or not up.model:
            return _bad("invalid_alias", "alias name and model must be non-empty")
        if up.provider is not None and not up.provider:
            return _bad("invalid_provider", "alias provider must be non-empty when supplied")
        state = get_admin_state()
        params = up.params or {}
        entry: Any
        if up.provider is not None or params:
            entry = {"model": up.model, "provider": up.provider, "params": params}
        else:
            entry = up.model
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        aliases = dict(models_cfg.get("aliases") or {})
        aliases[up.name] = entry
        models_cfg["aliases"] = aliases
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        model, provider, p = _alias_entry_to_dict(entry)
        return {
            "status": "ok",
            "alias": AliasRow(
                name=up.name, model=model, provider=provider, params=p
            ).model_dump(),
        }

    async def _apply_bulk(bulk: BulkAliases) -> Any:
        for k, v in bulk.aliases.items():
            if not k or not v:
                return _bad(
                    "invalid_alias", "alias name and target must be non-empty"
                )
        if bulk.default is not None and not bulk.default:
            return _bad("invalid_default", "default model must be non-empty")
        state = get_admin_state()
        cfg = dict(config_snapshot())
        models_cfg = dict(cfg.get("models") or {})
        models_cfg["aliases"] = dict(bulk.aliases)
        if bulk.default is not None:
            models_cfg["default"] = bulk.default
        err = await _persist_alias_swap(state, models_cfg)
        if err is not None:
            return err
        return {
            "status": "ok",
            "default": models_cfg.get("default", ""),
            "aliases": dict(models_cfg.get("aliases") or {}),
        }

    return r
