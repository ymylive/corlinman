"""Localhost admin HTTP sidecar (Feature C last-mile).

The Rust gateway's ``POST /admin/embedding/benchmark`` route passes through
to a tiny HTTP endpoint on this sidecar. Running it in-process with the
gRPC server avoids a second proto regen while keeping the admin surface
off the public bind.

Bind: ``127.0.0.1:$CORLINMAN_PY_ADMIN_PORT`` (default ``50052``).

Routes:
  * ``POST /embedding/benchmark`` body ``{"samples": [...], "dimension"?,
    "params"?}`` → 200 ``BenchmarkView`` | 400 invalid | 503 not-configured
    | 500 upstream error.

The sidecar runs on a daemon thread using :class:`http.server.ThreadingHTTPServer`
— stdlib only, no extra deps. The benchmark handler uses
:func:`asyncio.run_coroutine_threadsafe` to hop back onto the main
asyncio loop so the ``CorlinmanEmbeddingProvider.embed`` call runs on the
same loop as the gRPC servicers (which matters for httpx client reuse
later, and keeps the mental model simple now).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

import structlog
from corlinman_embedding import (
    CorlinmanEmbeddingProvider,
    GoogleEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    benchmark_embedding,
)
from corlinman_providers import EmbeddingSpec, ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)

_DEFAULT_PORT: Final[int] = 50052
_DEFAULT_BIND: Final[str] = "127.0.0.1"
_MAX_BODY_BYTES: Final[int] = 256 * 1024  # plenty for ≤20 samples
_MAX_SAMPLES: Final[int] = 20


def admin_sidecar_bind() -> tuple[str, int]:
    """Resolve ``(host, port)`` from env. Host is always localhost for safety."""
    port_str = os.environ.get("CORLINMAN_PY_ADMIN_PORT")
    try:
        port = int(port_str) if port_str else _DEFAULT_PORT
    except ValueError:
        port = _DEFAULT_PORT
    return _DEFAULT_BIND, port


def _load_py_config() -> dict[str, Any] | None:
    """Read ``CORLINMAN_PY_CONFIG`` and return the parsed JSON dict.

    Returns ``None`` when the env var is unset or the file doesn't exist /
    doesn't parse — callers must treat this as "embedding not configured".
    """
    path = os.environ.get("CORLINMAN_PY_CONFIG")
    if not path:
        return None
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("py_config.sidecar_load_failed", path=path, error=str(exc))
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _build_embedding_provider(
    data: dict[str, Any],
) -> tuple[CorlinmanEmbeddingProvider, EmbeddingSpec] | None:
    """Resolve the embedding provider from the py-config JSON.

    Returns ``None`` if the config has no enabled ``embedding`` section or
    the referenced provider isn't available / isn't embedding-capable.
    """
    emb_raw = data.get("embedding")
    if not emb_raw:
        return None
    try:
        spec = EmbeddingSpec.model_validate(emb_raw)
    except Exception as exc:
        logger.warning("py_config.embedding_invalid", error=str(exc))
        return None
    if not spec.enabled:
        return None

    # Find the referenced provider block for its api_key + base_url.
    provider_block: dict[str, Any] | None = None
    for entry in data.get("providers", []) or []:
        if entry.get("name") == spec.provider:
            provider_block = entry
            break
    if provider_block is None:
        logger.warning("py_config.embedding_provider_missing", provider=spec.provider)
        return None

    try:
        provider_spec = ProviderSpec.model_validate(provider_block)
    except Exception as exc:
        logger.warning("py_config.provider_invalid", error=str(exc))
        return None

    # Pick the embedding adapter by provider kind. Only the OpenAI-compatible
    # shape + Google embeddings are implemented — Anthropic has no embedding
    # API, so spec.provider pointing at an Anthropic slot is rejected.
    if provider_spec.kind in (
        ProviderKind.OPENAI,
        ProviderKind.OPENAI_COMPATIBLE,
        ProviderKind.DEEPSEEK,
        ProviderKind.QWEN,
        ProviderKind.GLM,
    ):
        return (
            OpenAICompatibleEmbeddingProvider.build(
                spec, api_key=provider_spec.api_key, base_url=provider_spec.base_url
            ),
            spec,
        )
    if provider_spec.kind is ProviderKind.GOOGLE:
        return (
            GoogleEmbeddingProvider.build(
                spec, api_key=provider_spec.api_key, base_url=provider_spec.base_url
            ),
            spec,
        )
    logger.warning(
        "py_config.embedding_kind_unsupported",
        provider=spec.provider,
        kind=str(provider_spec.kind),
    )
    return None


async def _run_benchmark(body: dict[str, Any]) -> dict[str, Any]:
    """Shared async path used by both the HTTP handler and tests.

    Raises :class:`ValueError` for user-facing 400s and :class:`RuntimeError`
    for 503 "not configured" / upstream failures — the HTTP handler maps
    them onto status codes.
    """
    samples = body.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("samples must be a non-empty list")
    if len(samples) > _MAX_SAMPLES:
        raise ValueError(f"samples is capped at {_MAX_SAMPLES} entries")
    if not all(isinstance(s, str) for s in samples):
        raise ValueError("samples must be strings")

    data = _load_py_config()
    if data is None:
        raise RuntimeError("py-config not available")

    built = _build_embedding_provider(data)
    if built is None:
        raise RuntimeError("no enabled [embedding] section in py-config")
    provider, spec = built

    # Caller may override `dimension` (e.g. dry-run a dimension change
    # before persisting it); falling back to the configured value keeps
    # the common case a one-liner: `{"samples": [...]}`.
    dimension = int(body.get("dimension") or spec.dimension)
    params = body.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")

    report = await benchmark_embedding(
        provider, samples, dimension=dimension, params=params
    )
    return asdict(report)


def _make_handler(loop: asyncio.AbstractEventLoop) -> type[BaseHTTPRequestHandler]:
    """Close over the main asyncio loop so the thread-pool HTTP handler
    can schedule the benchmark coroutine on it."""

    class _Handler(BaseHTTPRequestHandler):
        # Silence the default stderr access log — structlog already covers
        # what we care about and the default line format is noisy in tests.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:
            if self.path != "/embedding/benchmark":
                self._send_json(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _MAX_BODY_BYTES:
                self._send_json(400, {"error": "invalid_length"})
                return
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_json", "message": str(exc)})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "invalid_body"})
                return

            fut = asyncio.run_coroutine_threadsafe(_run_benchmark(body), loop)
            try:
                result = fut.result(timeout=60.0)
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_request", "message": str(exc)})
                return
            except RuntimeError as exc:
                self._send_json(
                    503, {"error": "not_configured", "message": str(exc)}
                )
                return
            except Exception as exc:
                logger.warning("admin_sidecar.benchmark_failed", error=str(exc))
                self._send_json(500, {"error": "upstream_failed", "message": str(exc)})
                return
            self._send_json(200, result)

        def do_GET(self) -> None:
            # Tiny health probe so a curl from the host can sanity-check the
            # sidecar without spinning up a full benchmark request.
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": "not_found"})

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


class AdminSidecar:
    """Lifecycle handle for the HTTP sidecar thread."""

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address  # type: ignore[return-value]

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def start_admin_sidecar(loop: asyncio.AbstractEventLoop) -> AdminSidecar | None:
    """Start the sidecar on a daemon thread. Returns ``None`` on bind
    failure (port already in use), after logging — the gRPC server still
    boots; the benchmark endpoint is the only feature that loses the
    sidecar and the gateway handles its 503 gracefully."""

    host, port = admin_sidecar_bind()
    handler_cls = _make_handler(loop)
    try:
        server = ThreadingHTTPServer((host, port), handler_cls)
    except OSError as exc:
        logger.warning(
            "admin_sidecar.bind_failed", host=host, port=port, error=str(exc)
        )
        return None

    thread = threading.Thread(
        target=server.serve_forever,
        name="corlinman-admin-sidecar",
        daemon=True,
    )
    thread.start()
    logger.info("admin_sidecar.listening", host=host, port=port)
    return AdminSidecar(server, thread)


__all__ = [
    "AdminSidecar",
    "_run_benchmark",  # exposed for tests
    "admin_sidecar_bind",
    "start_admin_sidecar",
]
