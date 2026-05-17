"""Port of ``rust/crates/corlinman-nodebridge/src/message.rs`` tests.

Covers JSON round-trip behaviour for every variant + the
``signature = None`` skip + ``Telemetry.tags`` key ordering.
"""

from __future__ import annotations

import json

from corlinman_nodebridge import (
    Capability,
    DispatchJob,
    JobResult,
    Ping,
    Pong,
    Register,
    Registered,
    RegisterRejected,
    Shutdown,
    Telemetry,
    decode_message,
    encode_message,
)


def test_register_round_trips_without_signature() -> None:
    original = Register(
        node_id="ios-dev-1",
        node_type="ios",
        capabilities=[Capability.new("system.notify", "1.0", {"type": "object"})],
        auth_token="tok",
        version="0.1.0",
        signature=None,
    )
    text = encode_message(original)
    assert '"kind":"register"' in text
    # Omitted signature must not appear on the wire.
    assert "signature" not in text, f"signature=None must be skipped, got {text}"
    back = decode_message(text)
    assert back == original


def test_register_round_trips_with_signature() -> None:
    original = Register(
        node_id="ios-dev-1",
        node_type="ios",
        capabilities=[],
        auth_token="tok",
        version="0.1.0",
        signature="base64sig",
    )
    text = encode_message(original)
    assert '"signature":"base64sig"' in text
    back = decode_message(text)
    assert back == original


def test_registered_and_rejected_are_distinct_kinds() -> None:
    ok = Registered(node_id="n", server_version="1.0.0-alpha", heartbeat_secs=15)
    nope = RegisterRejected(code="unsigned_registration", message="signature required")
    ok_s = encode_message(ok)
    nope_s = encode_message(nope)
    assert '"kind":"registered"' in ok_s
    assert '"kind":"register_rejected"' in nope_s


def test_dispatch_and_job_result_are_symmetric() -> None:
    d = DispatchJob(job_id="j1", job_kind="system.notify", params={"title": "hi"}, timeout_ms=5000)
    r = JobResult(job_id="j1", ok=True, payload={"delivered": True})
    for m in (d, r):
        text = encode_message(m)
        back = decode_message(text)
        assert back == m


def test_telemetry_tags_serialize_in_key_order() -> None:
    m = Telemetry(
        node_id="n",
        metric="battery.level",
        value=0.9,
        tags={"region": "cn", "build": "dev"},
    )
    text = encode_message(m)
    build_at = text.find("build")
    region_at = text.find("region")
    assert build_at != -1 and region_at != -1
    assert build_at < region_at, f"tags must be sorted: {text}"


def test_ping_pong_shutdown_round_trip() -> None:
    for m in (Ping(), Pong(), Shutdown(reason="server_stopping")):
        text = encode_message(m)
        back = decode_message(text)
        assert back == m


def test_dispatch_job_wire_uses_job_kind_field() -> None:
    """The Rust source renames the in-Rust ``kind`` field to ``job_kind``
    on the wire to avoid colliding with the tagged-union discriminant.
    The Python port uses ``job_kind`` end-to-end; assert the wire output
    matches what mobile clients will see."""
    msg = DispatchJob(job_id="j", job_kind="camera", params={}, timeout_ms=1)
    data = json.loads(encode_message(msg))
    assert data["kind"] == "dispatch_job"
    assert data["job_kind"] == "camera"
