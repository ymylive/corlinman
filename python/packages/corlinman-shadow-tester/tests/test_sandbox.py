"""Sandbox tests — ports of ``rust/.../src/sandbox/`` unit tests."""

from __future__ import annotations

from corlinman_shadow_tester.sandbox import (
    DockerBackend,
    InProcessBackend,
    sha256_hex,
)


# ---------------------------------------------------------------------------
# sha256_hex
# ---------------------------------------------------------------------------


def test_sha256_hex_matches_known_vector() -> None:
    # Standard NIST test vector for SHA-256 of "abc".
    assert (
        sha256_hex(b"abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_hex_handles_empty_input() -> None:
    assert (
        sha256_hex(b"")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


# ---------------------------------------------------------------------------
# InProcessBackend
# ---------------------------------------------------------------------------


async def test_run_self_test_returns_payload_sha256() -> None:
    backend = InProcessBackend()
    result = await backend.run_self_test("abc")
    assert (
        result.hash
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


async def test_run_self_test_is_deterministic_across_calls() -> None:
    backend = InProcessBackend()
    a = await backend.run_self_test("hello world")
    b = await backend.run_self_test("hello world")
    assert a == b


# ---------------------------------------------------------------------------
# DockerBackend.run_argv pins the isolation knobs without a real daemon.
# ---------------------------------------------------------------------------


def test_run_argv_pins_isolation_knobs() -> None:
    backend = DockerBackend("test/image:vN", 256, 30)
    argv = backend.run_argv("hello")
    for required in [
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=256m",
        "--memory-swap=256m",
        "--cpus=1.0",
        "--pids-limit=64",
        "--user=65532:65532",
        "test/image:vN",
        "sandbox-self-test",
        "--payload",
        "hello",
    ]:
        assert required in argv, f"argv missing {required}: {argv}"
