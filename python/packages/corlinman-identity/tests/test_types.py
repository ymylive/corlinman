"""Unit tests for :mod:`corlinman_identity.types`.

Ports the Rust ``types::tests`` module.
"""

from __future__ import annotations

import json

from corlinman_identity import BindingKind, UserId


def test_user_id_generate_is_unique_per_call() -> None:
    a = UserId.generate()
    b = UserId.generate()
    assert a != b, "ULIDs must be unique within the same ms tick"
    assert len(a.as_str()) == 26, "ULID is 26 chars in Crockford base32"


def test_user_id_round_trip_through_string() -> None:
    original = UserId.generate()
    s = original.as_str()
    restored = UserId(s)
    assert original == restored


def test_binding_kind_string_round_trip() -> None:
    for kind in (BindingKind.AUTO, BindingKind.VERIFIED, BindingKind.OPERATOR):
        assert BindingKind.from_db_str(kind.as_str()) is kind


def test_binding_kind_unknown_collapses_to_auto() -> None:
    # Forward-compat: a hypothetical future "federated" variant read
    # off an upgraded DB shouldn't raise.
    assert BindingKind.from_db_str("federated") is BindingKind.AUTO
    assert BindingKind.from_db_str("") is BindingKind.AUTO


def test_user_id_serialises_as_bare_string() -> None:
    # The Rust crate uses ``#[serde(transparent)]`` so a UserId becomes
    # a bare JSON string. Python's UserId subclasses str — same effect.
    uid = UserId("01HV3K9PQRSTUVWXYZABCDEFGH")
    encoded = json.dumps(uid)
    assert encoded == '"01HV3K9PQRSTUVWXYZABCDEFGH"'
    decoded = UserId(json.loads(encoded))
    assert decoded == uid
