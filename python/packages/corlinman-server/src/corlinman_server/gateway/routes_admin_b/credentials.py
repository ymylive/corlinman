"""``/admin/credentials*`` — provider-credential management surface.

Wave 2.3 of ``docs/PLAN_EASY_SETUP.md``. Borrows the hermes-agent EnvPage
mental model (provider-grouped rows + masked preview + paste-only edit)
but speaks the corlinman config TOML directly — there is no separate
``.env`` file. Every field is a string-shaped slot inside a
``[providers.<name>]`` block.

Routes:

* ``GET    /admin/credentials``                       — list every well-known
  provider with per-field set/preview/env_ref metadata.
* ``PUT    /admin/credentials/{provider}/{key}``      — write/update a single
  whitelisted field. Sets ``enabled = true`` on first write.
* ``DELETE /admin/credentials/{provider}/{key}``      — drop a field; flips
  ``enabled = false`` if the block ends up without any required fields
  (the block itself stays as a stub for UX continuity).
* ``POST   /admin/credentials/{provider}/enable``     — toggle the
  provider-wide ``enabled`` flag without touching field data.

The endpoint **never** returns plaintext values. ``preview`` is "last 4
chars" (``…xyz9``) when the stored value has 5+ characters, ``"***"`` for
shorter literals, and ``None`` when the slot is empty. When the operator
stored the credential as ``api_key = { env = "FOO" }`` the route surfaces
``env_ref="FOO"`` and ``set=true`` without ever resolving the env var.

The whitelist is intentionally small at launch — extending it later is
just adding entries to :data:`_ALLOWED_FIELDS`. Anything outside the
list gets a clean 400 ``unknown_field`` so the UI can show a precise
error without us needing to round-trip through pydantic.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.onboard import _write_config_atomic
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# Whitelist + provenance metadata
# ---------------------------------------------------------------------------


# Map of (provider name → ordered list of editable string fields). The order
# is the order the UI renders them in (api_key first, base_url next, etc).
_ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "openai": ("api_key", "base_url", "org_id"),
    "anthropic": ("api_key", "base_url"),
    "openrouter": ("api_key", "base_url"),
    "ollama": ("base_url",),
    "mock": (),
    "custom": ("api_key", "base_url", "kind"),
}


# Default kinds for each well-known provider — used when the block is
# absent and we synthesise an empty stub. ``custom`` carries no default
# kind because the operator picks it via the ``kind`` field.
_DEFAULT_KIND: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "openrouter": "openai_compatible",
    "ollama": "openai_compatible",
    "mock": "mock",
    "custom": "openai_compatible",
}


# Map of (provider, field) → conventional env-var name. Surfaces as
# ``env_ref`` in the GET response so operators recognise "I should set
# this in shell" without us probing ``os.environ`` ourselves.
_DEFAULT_ENV_REF: dict[tuple[str, str], str] = {
    ("openai", "api_key"): "OPENAI_API_KEY",
    ("openai", "base_url"): "OPENAI_BASE_URL",
    ("openai", "org_id"): "OPENAI_ORG_ID",
    ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
    ("anthropic", "base_url"): "ANTHROPIC_BASE_URL",
    ("openrouter", "api_key"): "OPENROUTER_API_KEY",
    ("openrouter", "base_url"): "OPENROUTER_BASE_URL",
    ("ollama", "base_url"): "OLLAMA_BASE_URL",
    ("custom", "api_key"): "CUSTOM_API_KEY",
}


# Fields whose value must drive ``enabled = true`` when first written.
# For most providers the API key suffices; ollama is keyless so its
# base_url plays that role.
_PRIMARY_FIELD: dict[str, str] = {
    "openai": "api_key",
    "anthropic": "api_key",
    "openrouter": "api_key",
    "ollama": "base_url",
    "custom": "api_key",
}


# Well-known provider display order — UI walks this list when rendering
# placeholders for not-yet-configured providers.
_WELL_KNOWN_ORDER: tuple[str, ...] = (
    "openai",
    "anthropic",
    "openrouter",
    "ollama",
    "mock",
    "custom",
)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class CredentialField(BaseModel):
    """One editable slot inside a ``[providers.<name>]`` block."""

    key: str
    set: bool = False
    preview: str | None = None
    env_ref: str | None = None


class CredentialProvider(BaseModel):
    name: str
    kind: str
    enabled: bool = False
    fields: list[CredentialField] = Field(default_factory=list)


class CredentialsListResponse(BaseModel):
    providers: list[CredentialProvider]


class SetCredentialBody(BaseModel):
    value: str


class EnableProviderBody(BaseModel):
    enabled: bool


class StatusOk(BaseModel):
    status: str = "ok"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bad(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _mask_preview(value: str) -> str:
    """Return a sanitised display preview of a stored credential.

    The hermes EnvPage convention is "first 4 + '…' + last 4"; we shrink
    that to just "…last4" because most providers stash sk-… style
    prefixes that would leak provider identity if we exposed both ends.
    Literals shorter than 5 characters are rendered as ``***`` so the
    UI never echoes "ab…ab" for a 4-char string.
    """
    if not value:
        return "***"
    if len(value) < 5:
        return "***"
    return "…" + value[-4:]


def _resolve_field_view(
    provider: str,
    key: str,
    raw: Any,
) -> CredentialField:
    """Return the wire-shaped row for one whitelisted field.

    Handles all three storage shapes the config supports today:

    * absent / ``None`` → ``set=false`` (with a default ``env_ref``
      hint so the UI can show "set OPENAI_API_KEY" placeholder text).
    * ``"plain string"`` → ``set=true`` + ``preview="…last4"``.
    * ``{ "env": "FOO" }``  → ``set=true``, ``env_ref="FOO"``, no preview
      (we intentionally don't peek at the env var — that's the operator's
      truth source and reading it through the admin surface would leak it
      to the gateway logs on error paths).
    * ``{ "value": "sk-..." }`` → ``set=true`` + ``preview="…last4"``.
    """
    default_env_ref = _DEFAULT_ENV_REF.get((provider, key))
    if raw is None:
        return CredentialField(
            key=key, set=False, preview=None, env_ref=default_env_ref
        )
    if isinstance(raw, dict):
        if "env" in raw:
            env_name = str(raw["env"])
            return CredentialField(
                key=key,
                set=True,
                preview=None,
                env_ref=env_name or default_env_ref,
            )
        if "value" in raw:
            literal = str(raw.get("value") or "")
            return CredentialField(
                key=key,
                set=bool(literal),
                preview=_mask_preview(literal) if literal else None,
                env_ref=default_env_ref,
            )
        # Unknown dict shape — surface as set without preview so the
        # operator at least sees "something is here" and can replace it.
        return CredentialField(
            key=key, set=True, preview=None, env_ref=default_env_ref
        )
    # Plain literal (or any non-dict like int) — coerce to str for preview.
    literal = str(raw)
    return CredentialField(
        key=key,
        set=bool(literal),
        preview=_mask_preview(literal) if literal else None,
        env_ref=default_env_ref,
    )


def _has_primary_set(provider: str, block: dict[str, Any]) -> bool:
    """Is the provider's primary field present + non-empty in the block?

    Drives the auto-flip of ``enabled``: writing the primary field for
    the first time turns the provider on; deleting it turns the provider
    off (but the rest of the block stays, so the UI keeps showing the
    placeholder row).
    """
    primary = _PRIMARY_FIELD.get(provider)
    if primary is None:
        # Providers without a primary field (e.g. mock) are always
        # "primed" once the block exists at all.
        return True
    raw = block.get(primary)
    if raw is None:
        return False
    if isinstance(raw, dict):
        if "env" in raw:
            return bool(raw.get("env"))
        if "value" in raw:
            return bool(raw.get("value"))
        return False
    return bool(str(raw))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "credentials"]
    )

    @r.get("/admin/credentials", response_model=CredentialsListResponse)
    async def list_credentials() -> CredentialsListResponse:
        cfg = dict(config_snapshot())
        providers_cfg = cfg.get("providers") or {}
        if not isinstance(providers_cfg, dict):
            providers_cfg = {}

        # Walk every well-known provider in the canonical order so the
        # UI always shows the same skeleton — even for providers the
        # operator hasn't touched yet. Operator-added providers (e.g.
        # "newapi" from the onboard wizard) get rendered at the end
        # under the "custom" whitelist so they remain manageable.
        seen: set[str] = set()
        out: list[CredentialProvider] = []
        for name in _WELL_KNOWN_ORDER:
            block = providers_cfg.get(name)
            block_dict = dict(block) if isinstance(block, dict) else {}
            kind = str(
                block_dict.get("kind") or _DEFAULT_KIND.get(name, "openai_compatible")
            )
            enabled = bool(block_dict.get("enabled", False))
            fields = [
                _resolve_field_view(name, k, block_dict.get(k))
                for k in _ALLOWED_FIELDS[name]
            ]
            out.append(
                CredentialProvider(
                    name=name, kind=kind, enabled=enabled, fields=fields
                )
            )
            seen.add(name)

        # Surface any operator-added providers via the "custom" whitelist
        # so they get visible rows + reveal/replace controls without
        # leaking unknown fields. We only expose api_key + base_url +
        # kind for them — anything richer should go through /admin/providers.
        for extra_name in sorted(providers_cfg):
            if extra_name in seen:
                continue
            extra_block = providers_cfg.get(extra_name)
            if not isinstance(extra_block, dict):
                continue
            kind = str(extra_block.get("kind") or "openai_compatible")
            enabled = bool(extra_block.get("enabled", False))
            fields = [
                _resolve_field_view(extra_name, k, extra_block.get(k))
                for k in _ALLOWED_FIELDS["custom"]
            ]
            out.append(
                CredentialProvider(
                    name=extra_name, kind=kind, enabled=enabled, fields=fields
                )
            )

        return CredentialsListResponse(providers=out)

    @r.put("/admin/credentials/{provider}/{key}", response_model=None)
    async def set_credential(
        body: SetCredentialBody,
        provider: str = Path(..., min_length=1),
        key: str = Path(..., min_length=1),
    ) -> JSONResponse | StatusOk:
        # Resolve the whitelist for this provider — fall back to the
        # ``custom`` set for unknown names so operator-added providers
        # remain editable through this surface.
        allowed = _ALLOWED_FIELDS.get(provider, _ALLOWED_FIELDS["custom"])
        if key not in allowed:
            return _bad("unknown_field")

        value = body.value
        if not isinstance(value, str):
            return _bad("invalid_value")

        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(
                status_code=503, content={"error": "config_path_unset"}
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(provider)
            block = dict(existing) if isinstance(existing, dict) else {}

            # Ensure kind is set so downstream consumers can build a
            # provider spec without a follow-up write. Operators can
            # override via the ``kind`` field when allowed.
            if "kind" not in block:
                block["kind"] = _DEFAULT_KIND.get(provider, "openai_compatible")

            # Store as a plain string. The provider registry already
            # accepts both literal strings and the ``{value=...}`` shape,
            # so this keeps the on-disk TOML readable for humans grepping
            # the file.
            block[key] = value

            if _has_primary_set(provider, block):
                block["enabled"] = True
            elif "enabled" not in block:
                block["enabled"] = False

            providers[provider] = block
            cfg["providers"] = providers

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        return StatusOk()

    @r.delete("/admin/credentials/{provider}/{key}", response_model=None)
    async def delete_credential(
        provider: str = Path(..., min_length=1),
        key: str = Path(..., min_length=1),
    ) -> Response | JSONResponse:
        allowed = _ALLOWED_FIELDS.get(provider, _ALLOWED_FIELDS["custom"])
        if key not in allowed:
            return _bad("unknown_field")

        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(
                status_code=503, content={"error": "config_path_unset"}
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(provider)
            if not isinstance(existing, dict):
                # Nothing to delete — return 204 anyway so the UI's
                # optimistic update doesn't have to special-case the
                # "field never existed" race.
                return Response(status_code=204)
            block = dict(existing)
            if key in block:
                del block[key]

            # If the primary field went away, flip enabled to false but
            # keep the block as a stub so the UI keeps showing it.
            if not _has_primary_set(provider, block):
                block["enabled"] = False

            providers[provider] = block
            cfg["providers"] = providers

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        return Response(status_code=204)

    @r.post("/admin/credentials/{provider}/enable", response_model=None)
    async def enable_provider(
        body: EnableProviderBody,
        provider: str = Path(..., min_length=1),
    ) -> JSONResponse | StatusOk:
        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(
                status_code=503, content={"error": "config_path_unset"}
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = providers.get(provider)
            block = dict(existing) if isinstance(existing, dict) else {}

            if "kind" not in block:
                block["kind"] = _DEFAULT_KIND.get(provider, "openai_compatible")
            block["enabled"] = bool(body.enabled)

            providers[provider] = block
            cfg["providers"] = providers

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        return StatusOk()

    return r
