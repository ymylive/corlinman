"""Rust→Python config handshake (Feature C last-mile).

Python port of ``rust/crates/corlinman-gateway/src/py_config.rs``.

Once Python *is* the runtime the Rust→Python file drop is partially
redundant — the same process that owns the live config can simply pass
:class:`ProviderRegistry` / alias maps into the agent servicer directly.
We keep the renderer / writer pair anyway, for three reasons:

1. ``corlinman_server.main._load_config`` still reads the
   ``CORLINMAN_PY_CONFIG`` JSON file when it's set — keeping the writer
   in-tree means a Python admin route that mutates ``providers`` /
   ``aliases`` / ``embedding`` can re-emit the file the existing
   in-process resolver watches.
2. External integrations (sidecars, FFI loaders) that grew up against
   the JSON shape stay supported with zero behavioural change.
3. The schema is small and the test suite anchors it — re-implementing
   it later if a sibling regresses the shape would be needless churn.

The JSON shape mirrors the Rust ``render_py_config`` byte-for-byte:

.. code-block:: json

    {
      "providers": [
        { "name": "anthropic", "kind": "anthropic",
          "api_key": "...", "base_url": null,
          "enabled": true, "params": {} }
      ],
      "aliases": {
        "smart": { "provider": "anthropic",
                   "model": "claude-opus-4-7",
                   "params": {"temperature": 0.7} }
      },
      "embedding": {
        "provider": "openai", "model": "text-embedding-3-small",
        "dimension": 1536, "enabled": true, "params": {}
      }
    }

The Python config object can be either a :class:`pydantic.BaseModel`,
a :class:`dict`, or any object whose ``providers`` / ``models.aliases``
/ ``embedding`` attributes behave like the Rust ``Config`` does. The
renderer is duck-typed (``getattr`` + ``hasattr``) so a future config
schema rev doesn't break the handshake.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

#: Env var name the Python AI plane reads to locate the JSON drop.
#: Mirrors ``corlinman_gateway::py_config::ENV_PY_CONFIG``.
ENV_PY_CONFIG: Final[str] = "CORLINMAN_PY_CONFIG"

#: Filename under ``$CORLINMAN_DATA_DIR``.
DEFAULT_PY_CONFIG_FILENAME: Final[str] = "py-config.json"

#: Per-spec the Python ``ProviderSpec.kind`` enum (mirrors
#: ``corlinman_core::config::ProviderKind``). Used to validate provider
#: ``kind`` values when the input config carries an explicit kind.
KNOWN_PROVIDER_KINDS: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "openai_compatible",
        "gemini",
        "ollama",
    }
)


def default_py_config_path() -> Path:
    """Resolve the default JSON drop location.

    Precedence:

    1. ``$CORLINMAN_DATA_DIR/py-config.json``
    2. ``~/.corlinman/py-config.json``
    3. ``/tmp/corlinman-py-config.json`` (container-friendly fallback)
    """
    data_dir = os.environ.get("CORLINMAN_DATA_DIR")
    if data_dir:
        return Path(data_dir) / DEFAULT_PY_CONFIG_FILENAME
    home = Path.home() if _has_home() else None
    if home is not None:
        return home / ".corlinman" / DEFAULT_PY_CONFIG_FILENAME
    return Path("/tmp/corlinman-py-config.json")


def _has_home() -> bool:
    try:
        Path.home()
        return True
    except (RuntimeError, OSError):
        return False


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_py_config(cfg: Any) -> dict[str, Any]:
    """Render the in-process config object as the Python JSON shape.

    Duck-typed against the Rust ``Config``:

    * ``cfg.providers`` — iterable of ``(name, entry)`` *or* a mapping
      ``{name: entry}``. Each ``entry`` exposes ``kind`` / ``api_key`` /
      ``base_url`` / ``enabled`` / ``params``.
    * ``cfg.models.aliases`` — mapping ``{alias_name: entry}``. ``entry``
      is either a full-form spec (``provider`` + ``model``) or a
      shorthand string. Shorthands are dropped — the Python legacy-prefix
      fallback handles them.
    * ``cfg.embedding`` — optional ``EmbeddingConfig`` with
      ``provider`` / ``model`` / ``dimension`` / ``enabled`` / ``params``.

    Invariants kept from the Rust impl:

    * Providers without a resolvable ``kind`` are dropped.
    * ``api_key`` is resolved via :func:`_resolve_secret`. Unresolved
      secrets render as ``None`` (Python ``ProviderSpec`` treats this as
      "no auth", same as the Rust side does for ``Option<String>``).
    * Shorthand / provider-less aliases are omitted from the output.
    """
    providers_in = _iter_providers(cfg)
    providers: list[dict[str, Any]] = []
    for name, entry in providers_in:
        kind = _kind_for(name, entry)
        if kind is None:
            continue
        providers.append(
            {
                "name": str(name),
                "kind": kind,
                "api_key": _resolve_secret(_attr(entry, "api_key", None)),
                "base_url": _attr(entry, "base_url", None),
                "enabled": bool(_attr(entry, "enabled", True)),
                "params": _params_to_json(_attr(entry, "params", {})),
            }
        )

    aliases_out: dict[str, Any] = {}
    for alias_name, alias_entry in _iter_aliases(cfg).items():
        rendered = _render_alias(alias_entry)
        if rendered is None:
            continue
        aliases_out[str(alias_name)] = rendered

    embedding = _render_embedding(_attr(cfg, "embedding", None))

    return {
        "providers": providers,
        "aliases": aliases_out,
        "embedding": embedding,
    }


def _iter_providers(cfg: Any) -> Iterable[tuple[str, Any]]:
    src = _attr(cfg, "providers", None)
    if src is None:
        return []
    if hasattr(src, "items") and callable(src.items):
        return list(src.items())
    if isinstance(src, Mapping):
        return list(src.items())
    # Try iterable of pairs.
    try:
        return [(n, e) for n, e in src]  # noqa: C416 — explicit unpack
    except TypeError:
        return []


def _iter_aliases(cfg: Any) -> Mapping[str, Any]:
    models = _attr(cfg, "models", None)
    aliases = _attr(models, "aliases", None) if models is not None else None
    if aliases is None:
        return {}
    if isinstance(aliases, Mapping):
        return aliases
    if hasattr(aliases, "items") and callable(aliases.items):
        return dict(aliases.items())
    return {}


def _render_alias(entry: Any) -> dict[str, Any] | None:
    # Shorthand: bare string.
    if isinstance(entry, str):
        return None
    provider = _attr(entry, "provider", None)
    model = _attr(entry, "model", None)
    # Provider-less full-form alias — same treatment as shorthand: omit.
    if not provider or not model:
        return None
    return {
        "provider": str(provider),
        "model": str(model),
        "params": _params_to_json(_attr(entry, "params", {})),
    }


def _render_embedding(emb: Any) -> dict[str, Any] | None:
    if emb is None:
        return None
    return {
        "provider": str(_attr(emb, "provider", "") or ""),
        "model": str(_attr(emb, "model", "") or ""),
        "dimension": int(_attr(emb, "dimension", 0) or 0),
        "enabled": bool(_attr(emb, "enabled", True)),
        "params": _params_to_json(_attr(emb, "params", {})),
    }


def _kind_for(name: str, entry: Any) -> str | None:
    explicit = _attr(entry, "kind", None)
    if explicit:
        kind = str(explicit)
        if kind in KNOWN_PROVIDER_KINDS:
            return kind
        # Unknown explicit kind — drop, matches the Rust ``None``-branch
        # behaviour.
        return None
    # Fallback: infer from the slot name. Mirrors
    # ``corlinman_core::config::Providers::kind_for`` for the first-party
    # names. Anything unrecognised drops out of the JSON.
    lowered = str(name).lower()
    if lowered in KNOWN_PROVIDER_KINDS:
        return lowered
    return None


def _resolve_secret(secret: Any) -> str | None:
    """Resolve a ``SecretRef``-shaped value.

    Accepts:

    * ``None`` → ``None``
    * a bare ``str`` → returned as-is (treated as a literal)
    * an object with ``env`` attr → ``os.environ.get(env)``
    * an object with ``value`` attr → ``str(value)``
    * a dict ``{"env": "..."}`` / ``{"value": "..."}`` — same shape as
      the Rust ``SecretRef`` tagged-enum serialization.
    """
    if secret is None:
        return None
    if isinstance(secret, str):
        return secret
    if isinstance(secret, Mapping):
        if "env" in secret:
            return os.environ.get(str(secret["env"]))
        if "value" in secret:
            value = secret["value"]
            return None if value is None else str(value)
        return None
    env = getattr(secret, "env", None)
    if env is not None:
        return os.environ.get(str(env))
    value = getattr(secret, "value", None)
    if value is not None:
        return str(value)
    return None


def _params_to_json(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, Mapping):
        return {str(k): _jsonable(v) for k, v in params.items()}
    if hasattr(params, "items") and callable(params.items):
        return {str(k): _jsonable(v) for k, v in params.items()}
    return {}


def _jsonable(value: Any) -> Any:
    """Pass-through coercion for ``serde_json::Value`` analogues.

    Pydantic models, dataclasses, and plain dict/list/str/int/float/bool
    are all already JSON-safe; anything else falls back to ``str()`` so
    the renderer can't 500 on an unexpected param value.
    """
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump()
    return str(value)


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Mapping-aware ``getattr`` — looks up ``name`` on attributes and
    ``Mapping`` keys, so dicts and Pydantic models both work."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_py_config_sync(cfg: Any, path: Path | str) -> None:
    """Synchronously render + atomically write the JSON drop.

    Atomicity: write to a sibling ``<path>.new``, then ``os.rename`` —
    matches the Rust ``write_py_config_sync`` byte-for-byte so the reader
    side (``corlinman_server.main._ReloadingProviderResolver``) sees a
    fully-formed file on every mtime bump.
    """
    target = Path(path)
    payload = render_py_config(cfg)
    body = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    # ``tempfile.NamedTemporaryFile`` keeps us safe from a half-written
    # file if the process dies mid-write; ``os.replace`` is atomic on
    # the same filesystem.
    fd, tmp = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".new",
        dir=str(target.parent) if target.parent.as_posix() else ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, target)
    except BaseException:
        # Clean up the temp file on any failure; the rename window is
        # microscopic but we don't want to leak ``.new`` shards.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def write_py_config(cfg: Any, path: Path | str) -> None:
    """Async wrapper around :func:`write_py_config_sync`.

    The Rust version uses ``tokio::fs`` for the rename hop. We stay sync
    here — the JSON payload is small (single-digit kB at most for a
    realistic provider set), and a serial sync write inside the async
    flow doesn't block the loop noticeably. Wrapping in
    :func:`asyncio.to_thread` would only buy us pretend-concurrency.
    """
    write_py_config_sync(cfg, path)


__all__ = [
    "DEFAULT_PY_CONFIG_FILENAME",
    "ENV_PY_CONFIG",
    "KNOWN_PROVIDER_KINDS",
    "default_py_config_path",
    "render_py_config",
    "write_py_config",
    "write_py_config_sync",
]
