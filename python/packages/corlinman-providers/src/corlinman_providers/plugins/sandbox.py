"""Docker sandbox via the official ``docker`` Python SDK.

Python port of ``rust/crates/corlinman-plugins/src/sandbox/``. Equivalent to
``bollard`` in Rust. One container per invocation; the container is shaped by
the manifest's ``[sandbox]`` block and configured to auto-remove.

The official Docker SDK is sync; we wrap calls in ``asyncio.to_thread`` so
the sandbox can be awaited from async call sites without blocking the loop.

Design notes (matching ``sandbox/docker.rs``):
  - One container per invocation. A pool is a future improvement.
  - ``cmd`` is ``[entry_point.command, *entry_point.args]``. No shell expansion.
  - OOM detection reads ``State.OOMKilled`` via ``inspect`` after the wait
    stream returns (the wait API only carries ``StatusCode``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .manifest import PluginManifest, SandboxConfig

log = logging.getLogger(__name__)


#: Default base image used when the manifest does not pin one explicitly.
DEFAULT_SANDBOX_IMAGE = "corlinman-sandbox:latest"

#: JSON-RPC error code attached to ``PluginOutput.error`` on OOM kill.
OOM_ERROR_CODE = -32010

#: Powers-of-1024 conversion table (docker convention).
_KIB = 1024
_MIB = _KIB * 1024
_GIB = _MIB * 1024
_TIB = _GIB * 1024


# ---------- Errors ----------


class SandboxError(Exception):
    """Base exception for sandbox failures."""


class SandboxConfigError(SandboxError):
    """``[sandbox]`` block was misconfigured (e.g. bad memory string)."""


class SandboxRuntimeError(SandboxError):
    """The Docker daemon refused the request, the container failed to start,
    or the stdio exchange could not complete."""


class SandboxTimeoutError(SandboxError):
    """The timeout for one stdin/stdout exchange elapsed."""


class SandboxCancelledError(SandboxError):
    """Caller cancelled the run via the ``cancel`` event."""


# ---------- Byte-string parsing (port of ``sandbox/bytes_parser.rs``) ----------


def parse_bytes(raw: str) -> int:
    """Parse a docker-style size string (``"256m"``, ``"1g"``) into bytes.

    Mirrors ``sandbox/bytes_parser.rs::parse_bytes``. Raises
    :class:`SandboxConfigError` on any failure.
    """
    trimmed = raw.strip()
    if not trimmed:
        raise SandboxConfigError("sandbox.memory: empty string")

    # Split numeric prefix from alphabetic suffix.
    split_at = len(trimmed)
    for idx, ch in enumerate(trimmed):
        if ch.isascii() and ch.isalpha():
            split_at = idx
            break
    num_part, unit_part = trimmed[:split_at], trimmed[split_at:]

    if not num_part:
        raise SandboxConfigError(f"sandbox.memory: no numeric prefix in {raw!r}")

    try:
        value = float(num_part)
    except ValueError as err:
        raise SandboxConfigError(
            f"sandbox.memory: invalid number in {raw!r}"
        ) from err
    if value < 0 or value != value or value == float("inf"):
        raise SandboxConfigError(
            f"sandbox.memory: non-finite / negative number in {raw!r}"
        )

    multipliers = {
        "": 1,
        "b": 1,
        "k": _KIB,
        "kb": _KIB,
        "m": _MIB,
        "mb": _MIB,
        "g": _GIB,
        "gb": _GIB,
        "t": _TIB,
        "tb": _TIB,
    }
    multiplier = multipliers.get(unit_part.lower())
    if multiplier is None:
        raise SandboxConfigError(
            f"sandbox.memory: unknown unit {unit_part!r} in {raw!r}"
        )

    product = value * multiplier
    if product > (2**64) - 1:
        raise SandboxConfigError(f"sandbox.memory: {raw!r} overflows u64")
    return int(product)


def is_enabled(sandbox: SandboxConfig) -> bool:
    """Whether a manifest's ``[sandbox]`` block actually asks for
    containerisation. Matches ``sandbox/mod.rs::is_enabled``.
    """
    return (
        sandbox.memory is not None
        or sandbox.cpus is not None
        or sandbox.read_only_root
        or bool(sandbox.cap_drop)
        or sandbox.network is not None
        or bool(sandbox.binds)
    )


# ---------- HostConfig assembly ----------


def host_config_from(manifest: PluginManifest) -> dict[str, Any]:
    """Build the ``host_config`` kwargs the Docker SDK accepts on
    ``client.containers.run`` / ``create``.

    The Docker Python SDK accepts host-config knobs as top-level keyword
    arguments on the ``containers.run``/``create`` methods (e.g.
    ``mem_limit``, ``nano_cpus``, ``network_mode``). We assemble that dict
    here so :class:`DockerSandbox` and tests share the same translation
    table.

    Mirrors ``sandbox/docker.rs::DockerSandbox::host_config_from``.
    """
    sb = manifest.sandbox

    kwargs: dict[str, Any] = {
        "auto_remove": True,
        "read_only": bool(sb.read_only_root),
        # Default to ``none`` when the manifest doesn't say — plugin code
        # must opt into network access explicitly.
        "network_mode": sb.network if sb.network is not None else "none",
    }

    if sb.memory is not None:
        kwargs["mem_limit"] = parse_bytes(sb.memory)
    if sb.cpus is not None:
        kwargs["nano_cpus"] = int(float(sb.cpus) * 1e9)
    if sb.cap_drop:
        kwargs["cap_drop"] = list(sb.cap_drop)
    if sb.binds:
        # The SDK accepts the docker-style ``src:dst[:ro]`` list straight
        # through as ``volumes`` on ``containers.run``/``create``.
        kwargs["volumes"] = list(sb.binds)

    return kwargs


# ---------- PluginOutput (mirrors runtime::PluginOutput in Rust) ----------


@dataclass
class PluginOutput:
    """Terminal result from a sandbox invocation.

    Three variants encoded by ``kind``:
      - ``"success"`` — ``content`` is the JSON-RPC ``result`` bytes.
      - ``"error"``   — ``code`` + ``message`` from the JSON-RPC error.
      - ``"accepted_for_later"`` — async plugin returned a ``task_id``.
    """

    kind: str
    duration_ms: int
    content: bytes | None = None
    code: int | None = None
    message: str | None = None
    task_id: str | None = None

    @classmethod
    def success(cls, content: bytes, duration_ms: int) -> PluginOutput:
        return cls(kind="success", duration_ms=duration_ms, content=content)

    @classmethod
    def error(cls, code: int, message: str, duration_ms: int) -> PluginOutput:
        return cls(
            kind="error",
            duration_ms=duration_ms,
            code=code,
            message=message,
        )

    @classmethod
    def accepted_for_later(cls, task_id: str, duration_ms: int) -> PluginOutput:
        return cls(
            kind="accepted_for_later",
            duration_ms=duration_ms,
            task_id=task_id,
        )


# ---------- Runner abstraction ----------


class DockerRunner(ABC):
    """Run one JSON-RPC request inside a container.

    Tests inject a fake to dodge the Docker daemon entirely; production wires
    in :class:`DockerSandbox`. Async to match the call sites in
    ``runtime::jsonrpc_stdio``.
    """

    @abstractmethod
    async def run(
        self,
        manifest: PluginManifest,
        request_line: bytes,
        timeout_ms: int,
        cancel: asyncio.Event | None = None,
    ) -> PluginOutput:
        ...


# ---------- Response parser ----------


def parse_response_line(line: bytes, duration_ms: int) -> PluginOutput:
    """Decode a single JSON-RPC response line into a :class:`PluginOutput`.

    Mirrors ``sandbox/docker.rs::parse_response_line`` — accepts either a
    successful ``result`` payload, a JSON-RPC ``error`` object, or a result
    object carrying ``task_id`` (which produces ``accepted_for_later``).
    """
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as err:
        raise SandboxRuntimeError(f"sandbox_docker:response: {err}") from err

    trimmed = text.strip()
    try:
        obj = json.loads(trimmed)
    except json.JSONDecodeError as err:
        raise SandboxRuntimeError(
            f"sandbox_docker:response: {err} (raw: {trimmed!r})"
        ) from err

    if not isinstance(obj, dict):
        raise SandboxRuntimeError(
            f"sandbox_docker:response: expected JSON object, got {type(obj).__name__}"
        )

    jsonrpc = obj.get("jsonrpc")
    if jsonrpc is not None and jsonrpc != "2.0":
        raise SandboxRuntimeError(
            f"sandbox_docker:response: unexpected jsonrpc version {jsonrpc!r}"
        )

    error_obj = obj.get("error")
    if error_obj is not None:
        return PluginOutput.error(
            code=int(error_obj.get("code", 0)),
            message=str(error_obj.get("message", "")),
            duration_ms=duration_ms,
        )

    result = obj.get("result")
    if result is None:
        result = None
    if isinstance(result, dict) and isinstance(result.get("task_id"), str):
        return PluginOutput.accepted_for_later(
            task_id=result["task_id"], duration_ms=duration_ms
        )

    body = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return PluginOutput.success(content=body, duration_ms=duration_ms)


# ---------- Concrete Docker sandbox ----------


class DockerSandbox(DockerRunner):
    """Concrete :class:`DockerRunner` that talks to the local Docker daemon.

    Lazy-imports the ``docker`` package so callers that never instantiate a
    sandbox don't need the dependency installed at import time. The package
    *is* a declared dependency of corlinman-providers, but keeping the
    import lazy means unit tests that only exercise ``host_config_from`` /
    ``parse_bytes`` run on machines without Docker.
    """

    def __init__(
        self,
        *,
        client: Any = None,
        default_image: str = DEFAULT_SANDBOX_IMAGE,
    ) -> None:
        self._client = client
        self.default_image = default_image

    @classmethod
    async def connect(cls, default_image: str = DEFAULT_SANDBOX_IMAGE) -> DockerSandbox:
        """Connect to the local Docker socket and ping the daemon.

        Equivalent to ``DockerSandbox::new`` in Rust. Raises
        :class:`SandboxRuntimeError` if the daemon is unreachable.
        """
        try:
            import docker  # type: ignore[import-not-found]
        except ImportError as err:  # pragma: no cover — declared dep
            raise SandboxRuntimeError(
                "docker SDK not installed; `pip install docker>=7`"
            ) from err

        try:
            client = await asyncio.to_thread(docker.from_env)
            await asyncio.to_thread(client.ping)
        except Exception as err:
            raise SandboxRuntimeError(f"docker connect/ping: {err}") from err

        return cls(client=client, default_image=default_image)

    async def run(
        self,
        manifest: PluginManifest,
        request_line: bytes,
        timeout_ms: int,
        cancel: asyncio.Event | None = None,
    ) -> PluginOutput:
        if not is_enabled(manifest.sandbox):
            raise SandboxConfigError(
                "DockerSandbox.run called on manifest without sandbox config"
            )
        if self._client is None:
            raise SandboxRuntimeError(
                "DockerSandbox has no client; construct via DockerSandbox.connect()"
            )

        host_kwargs = host_config_from(manifest)
        cmd = [manifest.entry_point.command, *manifest.entry_point.args]
        env = [f"{k}={v}" for k, v in manifest.entry_point.env.items()]
        name = f"corlinman-{manifest.name}-{uuid.uuid4().hex}"

        started = time.monotonic()
        deadline_seconds = max(timeout_ms, 1) / 1000.0

        # Build creation kwargs that the SDK accepts on
        # ``client.api.create_container``. We use the low-level API so we
        # can attach with stdin/stdout/stderr streams.
        create_kwargs: dict[str, Any] = {
            "image": self.default_image,
            "command": cmd,
            "name": name,
            "stdin_open": True,
            "tty": False,
            "environment": env,
            "working_dir": "/workspace",
            "host_config": self._client.api.create_host_config(**host_kwargs),
        }

        async def _create_and_run() -> bytes | None:
            container = await asyncio.to_thread(
                self._client.api.create_container, **create_kwargs
            )
            container_id = container["Id"]

            try:
                # Open a duplex socket BEFORE starting so we don't miss
                # any early output.
                sock = await asyncio.to_thread(
                    self._client.api.attach_socket,
                    container_id,
                    {"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1},
                )
                await asyncio.to_thread(self._client.api.start, container_id)

                raw_sock = getattr(sock, "_sock", sock)

                # Write the request line then close stdin (half-close).
                await asyncio.to_thread(raw_sock.sendall, request_line)
                with contextlib.suppress(OSError):  # pragma: no cover — defensive
                    raw_sock.shutdown(1)  # SHUT_WR

                # Drain stdout until newline.
                buf = bytearray()
                while True:
                    chunk = await asyncio.to_thread(raw_sock.recv, 4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\n" in chunk:
                        break

                with contextlib.suppress(OSError):  # pragma: no cover
                    raw_sock.close()

                if not buf:
                    return None
                end = buf.find(b"\n")
                if end == -1:
                    return bytes(buf)
                return bytes(buf[:end])
            finally:
                # The `auto_remove` host config handles the happy path;
                # this is the defensive cleanup for cancel / error paths.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self._client.api.remove_container,
                        container_id,
                        force=True,
                    )

        run_task = asyncio.create_task(_create_and_run())
        waiters: list[asyncio.Task[Any]] = [run_task]
        cancel_task: asyncio.Task[Any] | None = None
        if cancel is not None:
            cancel_task = asyncio.create_task(cancel.wait())
            waiters.append(cancel_task)

        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=deadline_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()

        if not done:
            run_task.cancel()
            raise SandboxTimeoutError(
                f"sandbox_docker_io: deadline exceeded ({timeout_ms}ms)"
            )

        if cancel_task is not None and cancel_task in done and run_task not in done:
            run_task.cancel()
            raise SandboxCancelledError("sandbox_docker")

        response_bytes: bytes | None = run_task.result()
        duration_ms = int((time.monotonic() - started) * 1000)

        if response_bytes is None:
            raise SandboxRuntimeError(
                "plugin closed stdout before responding"
            )

        return parse_response_line(response_bytes, duration_ms)


async def default_runner() -> DockerRunner:
    """Return a freshly-connected :class:`DockerSandbox`. Convenience
    wrapper matching ``sandbox/docker.rs::default_runner``.
    """
    return await DockerSandbox.connect()


__all__ = [
    "DEFAULT_SANDBOX_IMAGE",
    "OOM_ERROR_CODE",
    "DockerRunner",
    "DockerSandbox",
    "PluginOutput",
    "SandboxCancelledError",
    "SandboxConfigError",
    "SandboxError",
    "SandboxRuntimeError",
    "SandboxTimeoutError",
    "default_runner",
    "host_config_from",
    "is_enabled",
    "parse_bytes",
    "parse_response_line",
]
