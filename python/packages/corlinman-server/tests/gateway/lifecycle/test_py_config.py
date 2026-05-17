"""Tests for :mod:`corlinman_server.gateway.lifecycle.py_config`.

Mirrors the Rust ``corlinman_gateway::py_config::tests`` suite. Duck-
typed config inputs (``dict`` / ``SimpleNamespace``) stand in for the
in-tree ``Config`` so the renderer works against whatever shape the
sibling agent lands.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from corlinman_server.gateway.lifecycle.py_config import (
    DEFAULT_PY_CONFIG_FILENAME,
    KNOWN_PROVIDER_KINDS,
    default_py_config_path,
    render_py_config,
    write_py_config_sync,
)


def _cfg_with_everything(env_key_value: str = "sk-test-xyz") -> SimpleNamespace:
    """Build a duck-typed config with the same shape the renderer
    expects of the Rust ``Config`` struct."""
    os.environ["PY_CONFIG_TEST_KEY"] = env_key_value
    return SimpleNamespace(
        providers={
            "anthropic": SimpleNamespace(
                kind=None,  # inferred from slot name
                api_key=SimpleNamespace(env="PY_CONFIG_TEST_KEY"),
                base_url=None,
                enabled=True,
                params={"temperature": 0.7},
            ),
            "openai": SimpleNamespace(
                kind="openai",
                api_key=SimpleNamespace(value="sk-literal"),
                base_url="https://api.openai.com/v1",
                enabled=True,
                params={},
            ),
        },
        models=SimpleNamespace(
            aliases={
                "smart": SimpleNamespace(
                    provider="anthropic",
                    model="claude-opus-4-7",
                    params={"temperature": 0.5},
                ),
                # Shorthand alias — should be dropped from JSON.
                "bare": "gpt-4o",
            },
        ),
        embedding=SimpleNamespace(
            provider="openai",
            model="text-embedding-3-small",
            dimension=1536,
            enabled=True,
            params={},
        ),
    )


def test_render_matches_python_schema() -> None:
    try:
        cfg = _cfg_with_everything()
        v = render_py_config(cfg)

        providers = v["providers"]
        assert len(providers) == 2

        anthropic = next(p for p in providers if p["name"] == "anthropic")
        assert anthropic["kind"] == "anthropic"
        assert anthropic["api_key"] == "sk-test-xyz"
        assert anthropic["enabled"] is True
        assert anthropic["params"]["temperature"] == 0.7

        openai = next(p for p in providers if p["name"] == "openai")
        assert openai["kind"] == "openai"
        assert openai["api_key"] == "sk-literal"
        assert openai["base_url"] == "https://api.openai.com/v1"

        aliases = v["aliases"]
        assert "smart" in aliases
        assert "bare" not in aliases
        assert aliases["smart"]["provider"] == "anthropic"
        assert aliases["smart"]["model"] == "claude-opus-4-7"
        assert aliases["smart"]["params"]["temperature"] == 0.5

        embedding = v["embedding"]
        assert embedding is not None
        assert embedding["provider"] == "openai"
        assert embedding["model"] == "text-embedding-3-small"
        assert embedding["dimension"] == 1536
        assert embedding["enabled"] is True
    finally:
        os.environ.pop("PY_CONFIG_TEST_KEY", None)


def test_write_py_config_sync_produces_parseable_file(tmp_path: Path) -> None:
    try:
        cfg = _cfg_with_everything()
        target = tmp_path / "py-config.json"

        write_py_config_sync(cfg, target)
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert isinstance(parsed["providers"], list)
        assert isinstance(parsed["aliases"], dict)
        assert isinstance(parsed["embedding"], dict)

        # No stale ``.new`` sidecar after atomic rename.
        for sibling in tmp_path.iterdir():
            assert not sibling.name.endswith(".new"), sibling
    finally:
        os.environ.pop("PY_CONFIG_TEST_KEY", None)


def test_missing_env_var_leaves_api_key_null() -> None:
    os.environ.pop("PY_CONFIG_TEST_MISSING", None)
    cfg = SimpleNamespace(
        providers={
            "anthropic": SimpleNamespace(
                kind=None,
                api_key=SimpleNamespace(env="PY_CONFIG_TEST_MISSING"),
                base_url=None,
                enabled=True,
                params={},
            ),
        },
        models=SimpleNamespace(aliases={}),
        embedding=None,
    )
    v = render_py_config(cfg)
    anthropic = next(p for p in v["providers"] if p["name"] == "anthropic")
    assert anthropic["api_key"] is None


def test_empty_config_renders_empty_sections() -> None:
    cfg = SimpleNamespace(
        providers={},
        models=SimpleNamespace(aliases={}),
        embedding=None,
    )
    v = render_py_config(cfg)
    assert v["providers"] == []
    assert v["aliases"] == {}
    assert v["embedding"] is None


def test_dict_shaped_config_also_works() -> None:
    """The renderer is duck-typed so plain dicts work too — useful for
    admin handlers that build their own payload."""
    cfg = {
        "providers": {
            "openai": {
                "kind": "openai",
                "api_key": {"value": "sk-from-dict"},
                "base_url": None,
                "enabled": True,
                "params": {},
            }
        },
        "models": {
            "aliases": {
                "fast": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "params": {},
                }
            }
        },
        "embedding": None,
    }
    v = render_py_config(cfg)
    assert v["providers"][0]["api_key"] == "sk-from-dict"
    assert v["aliases"]["fast"]["model"] == "gpt-4o-mini"


def test_default_py_config_path_uses_data_dir_env(tmp_path: Path) -> None:
    """``$CORLINMAN_DATA_DIR`` takes precedence over $HOME."""
    old = os.environ.get("CORLINMAN_DATA_DIR")
    try:
        os.environ["CORLINMAN_DATA_DIR"] = str(tmp_path)
        path = default_py_config_path()
        assert path == tmp_path / DEFAULT_PY_CONFIG_FILENAME
    finally:
        if old is None:
            os.environ.pop("CORLINMAN_DATA_DIR", None)
        else:
            os.environ["CORLINMAN_DATA_DIR"] = old


def test_known_provider_kinds_covers_first_party() -> None:
    # Spot-check that the inference list isn't accidentally emptied.
    for name in ("anthropic", "openai", "gemini", "ollama"):
        assert name in KNOWN_PROVIDER_KINDS
