"""Execution sandbox for high-risk EvolutionKinds.

Ported 1:1 from ``rust/crates/corlinman-shadow-tester/src/sandbox/``.

The Phase 3 in-process simulators (``MemoryOpSimulator``,
``TagRebalanceSimulator``, ``SkillUpdateSimulator``) live in
:mod:`corlinman_shadow_tester.simulator` and stay there: each is already
TOCTOU-hardened and the kind contracts they evaluate never reach across
process boundaries.

Phase 4 introduces three new kinds — ``prompt_template``,
``tool_policy``, ``new_skill`` — whose evals call out to a live LLM or
run unverified scripts. Those need stronger isolation than an in-process
simulator gives. This module is the abstraction that lets the runner
route work to either:

- :class:`InProcessBackend` — runs the workload directly in the
  gateway's process. Suitable for deterministic eval workloads that
  don't touch outside resources.
- :class:`DockerBackend` — spawns a frozen ``corlinman-sandbox``
  container with ``--network=none``, ``--read-only``, ``--cap-drop=ALL``,
  ``--security-opt=no-new-privileges``, ``--memory=<config>m``,
  ``--pids-limit=64``, a wall-clock timeout, and ``--user=65532:65532``.

v1 surface is deliberately small: a single :meth:`run_self_test` that
accepts a payload and returns its SHA-256.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass
from typing import Protocol


def sha256_hex(payload: bytes) -> str:
    """Compute the SHA-256 of ``payload`` and format it as lowercase hex.

    Shared between the in-process backend and the ``sandbox-self-test``
    subcommand the docker backend invokes inside the container —
    cross-process consistency is the whole point of the integration test.
    """
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class SelfTestResult:
    """JSON shape returned by the ``sandbox-self-test`` subcommand."""

    hash: str

    def to_dict(self) -> dict[str, str]:
        return {"hash": self.hash}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SelfTestResult:
        raw_hash = data.get("hash")
        if not isinstance(raw_hash, str):
            raise ValueError("SelfTestResult.hash must be a string")
        return cls(hash=raw_hash)


class SandboxError(RuntimeError):
    """Base error for sandbox failures."""


class SpawnError(SandboxError):
    def __init__(self, source: Exception) -> None:
        super().__init__(f"docker spawn: {source}")
        self.source = source


class TimeoutError_(SandboxError):
    """Wall-clock timeout. Named with a trailing underscore so it doesn't
    shadow the builtin ``TimeoutError``."""

    def __init__(self, timeout_secs: float) -> None:
        super().__init__(f"docker timeout after {timeout_secs}s")
        self.timeout_secs = timeout_secs


class NonZeroExitError(SandboxError):
    def __init__(self, status: int | None, stderr: str) -> None:
        super().__init__(
            f"docker exited non-zero (status={status!r}, stderr={stderr})"
        )
        self.status = status
        self.stderr = stderr


class OutputParseError(SandboxError):
    def __init__(self, source: Exception, raw: str) -> None:
        super().__init__(f"docker stdout was not valid JSON ({source}); raw: {raw}")
        self.source = source
        self.raw = raw


class DaemonUnavailableError(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(f"docker daemon unreachable: {message}")
        self.message = message


class SandboxBackend(Protocol):
    """Execution backend for sandboxed workloads.

    Every implementation is either zero-state (in-process) or holds plain
    config (docker). The runner stamps a concrete backend onto its state
    at boot and reuses it across per-eval futures.
    """

    async def run_self_test(self, payload: str) -> SelfTestResult: ...


# ---------------------------------------------------------------------------
# InProcessBackend
# ---------------------------------------------------------------------------


class InProcessBackend:
    """Zero-state :class:`SandboxBackend` that runs work directly in the
    caller's process.

    See module docs for when to use it. The deterministic self-test
    workload (SHA-256 of a payload) has no isolation requirements —
    running it in-process is legitimately equivalent to running it in a
    container.
    """

    async def run_self_test(self, payload: str) -> SelfTestResult:
        return SelfTestResult(hash=sha256_hex(payload.encode("utf-8")))


# ---------------------------------------------------------------------------
# DockerBackend
# ---------------------------------------------------------------------------


class DockerBackend:
    """Docker :class:`SandboxBackend`.

    Holds the image tag and resource caps the per-call ``docker run``
    command needs.
    """

    def __init__(self, image: str, mem_mb: int, timeout_secs: int) -> None:
        self.image = image
        self.mem_mb = int(mem_mb)
        self.timeout_secs = int(timeout_secs)

    def run_argv(self, payload: str) -> list[str]:
        """Compose the per-call ``docker run`` argv. Exposed for testing
        so callers can pin the exact isolation knobs."""
        mem_arg = f"{self.mem_mb}m"
        return [
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:size=64m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={mem_arg}",
            f"--memory-swap={mem_arg}",
            "--cpus=1.0",
            "--pids-limit=64",
            "--user=65532:65532",
            self.image,
            "sandbox-self-test",
            "--payload",
            payload,
        ]

    async def run_self_test(self, payload: str) -> SelfTestResult:
        argv = self.run_argv(payload)

        if shutil.which("docker") is None:
            raise DaemonUnavailableError(
                "docker binary not found on PATH; install Docker or set $PATH"
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # Race between ``shutil.which`` and exec where the binary
            # disappears (or never existed on a path mid-replace) ends
            # up here.
            raise DaemonUnavailableError(
                f"docker binary not found on PATH: {exc}"
            ) from exc
        except OSError as exc:
            raise SpawnError(exc) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_secs
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise TimeoutError_(float(self.timeout_secs)) from exc

        if proc.returncode != 0:
            raise NonZeroExitError(
                status=proc.returncode,
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise OutputParseError(exc, stdout_text) from exc
        if not isinstance(data, dict):
            raise OutputParseError(
                ValueError("top-level JSON must be an object"), stdout_text
            )
        try:
            return SelfTestResult.from_dict(data)
        except ValueError as exc:
            raise OutputParseError(exc, stdout_text) from exc


__all__ = [
    "DaemonUnavailableError",
    "DockerBackend",
    "InProcessBackend",
    "NonZeroExitError",
    "OutputParseError",
    "SandboxBackend",
    "SandboxError",
    "SelfTestResult",
    "SpawnError",
    "TimeoutError_",
    "sha256_hex",
]
