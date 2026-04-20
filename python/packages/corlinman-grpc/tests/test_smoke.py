"""Smoke test — package imports and placeholder constant is present."""

from __future__ import annotations


def test_package_imports() -> None:
    import corlinman_grpc

    assert corlinman_grpc.PROTO_VERSION == "v1"
