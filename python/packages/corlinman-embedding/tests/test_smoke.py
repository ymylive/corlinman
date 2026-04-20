"""Smoke test — package imports cleanly without the ``local`` extra."""

from __future__ import annotations


def test_package_imports() -> None:
    import corlinman_embedding  # noqa: F401
    from corlinman_embedding import remote_client, router  # noqa: F401
