"""Port of ``corlinman-tenant::id`` unit tests to pytest."""

from __future__ import annotations

import json

import pytest
from corlinman_server.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_SLUG_REGEX_STR,
    TenantId,
    TenantIdEmpty,
    TenantIdError,
    TenantIdInvalidShape,
    default_tenant,
)


def test_legacy_default_is_valid() -> None:
    t = TenantId.legacy_default()
    assert t.as_str() == "default"
    assert t.is_legacy_default()
    # `default_tenant()` free function agrees with class method.
    assert default_tenant() == TenantId.legacy_default()


@pytest.mark.parametrize(
    "slug",
    [
        "default",
        "acme",
        "acme-corp",
        "tenant1",
        "a",
        "ymylive-prod",
        # 63 chars: max allowed.
        "a234567890123456789012345678901234567890123456789012345678901bc",
    ],
)
def test_accepts_valid_slugs(slug: str) -> None:
    t = TenantId.from_str(slug)
    assert t.as_str() == slug


@pytest.mark.parametrize(
    "slug",
    [
        "1leading-digit",
        "-leading-dash",
        "Acme",  # uppercase
        "acme_corp",  # underscore
        "acme corp",  # space
        "acme.corp",  # dot
        "acme/corp",  # slash (path traversal vector)
        "acme\\corp",  # backslash
        "acme\ncorp",  # newline (multi-line bypass guard)
        "acmeé",  # non-ASCII
        # 64 chars: one over the cap.
        "a234567890123456789012345678901234567890123456789012345678901bcd",
    ],
)
def test_rejects_invalid_slugs(slug: str) -> None:
    with pytest.raises(TenantIdInvalidShape) as excinfo:
        TenantId.from_str(slug)
    assert excinfo.value.value == slug


def test_empty_is_distinct_error_from_invalid() -> None:
    # Operator typing `?tenant=` with an empty value is the most
    # common mistake — we keep it as its own subclass so a gateway
    # middleware can return a more helpful 400 message.
    with pytest.raises(TenantIdEmpty):
        TenantId.from_str("")
    with pytest.raises(TenantIdInvalidShape):
        TenantId.from_str(" ")


def test_empty_and_invalid_share_base_class() -> None:
    """Both subclasses must derive from :class:`TenantIdError` so generic
    handlers can catch either with one except."""
    assert issubclass(TenantIdEmpty, TenantIdError)
    assert issubclass(TenantIdInvalidShape, TenantIdError)


def test_display_round_trips_through_from_str() -> None:
    t = TenantId.new("acme-corp")
    s = str(t)
    assert s == "acme-corp"
    assert TenantId.from_str(s) == t


def test_repr_includes_value() -> None:
    t = TenantId.new("acme")
    assert "acme" in repr(t)


def test_json_round_trip() -> None:
    """``to_json`` emits a bare string, mirroring Rust's ``Serialize``."""
    t = TenantId.new("acme-corp")
    payload = json.dumps(t.to_json())
    assert payload == '"acme-corp"'
    back = TenantId.from_json(json.loads(payload))
    assert back == t


def test_from_json_rejects_path_traversal() -> None:
    # Integration-level guard: a malicious config file must not
    # smuggle a path-traversal segment through the deserializer.
    with pytest.raises(TenantIdInvalidShape):
        TenantId.from_json("../etc")


def test_from_json_rejects_non_string() -> None:
    with pytest.raises(TenantIdInvalidShape):
        TenantId.from_json(123)


def test_ordering_is_lexicographic_on_underlying_str() -> None:
    # `sorted()` (BTreeMap users — config rendering, deterministic test
    # fixtures) depends on this. Flipping the impl would silently break
    # JSON / TOML diff stability.
    ids = [
        TenantId.new("charlie"),
        TenantId.new("acme"),
        TenantId.new("bravo"),
    ]
    ids.sort()
    assert [t.as_str() for t in ids] == ["acme", "bravo", "charlie"]


def test_tenant_id_is_hashable_and_dict_key() -> None:
    t1 = TenantId.new("acme")
    t2 = TenantId.new("acme")
    d: dict[TenantId, int] = {t1: 1}
    assert d[t2] == 1, "TenantId equality + hashing must coincide"


def test_into_inner_returns_str() -> None:
    t = TenantId.new("acme")
    s = t.into_inner()
    assert isinstance(s, str)
    assert s == "acme"


def test_default_constant_value() -> None:
    assert DEFAULT_TENANT_ID == "default"


def test_public_regex_string_matches_spec() -> None:
    """Pin the public regex string against the documented pattern."""
    # Python uses `\Z` where Rust uses `\z`; both mean "end of string
    # not matching before a trailing newline". The literal in the
    # crate docs is the Rust spelling; we store the Python-flavoured
    # spelling but the semantics are identical.
    assert TENANT_SLUG_REGEX_STR == r"\A[a-z][a-z0-9-]{0,62}\Z"
