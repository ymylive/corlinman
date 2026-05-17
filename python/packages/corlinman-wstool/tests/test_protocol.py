"""Wire-format unit tests.

Mirror the Rust ``#[cfg(test)] mod tests`` block in
``rust/crates/corlinman-wstool/src/message.rs`` so the two
implementations stay observable from the same spec.
"""

from __future__ import annotations

from corlinman_wstool.protocol import ToolAdvert, WsToolMessage


def test_invoke_round_trips() -> None:
    original = WsToolMessage.Invoke(
        request_id="req-1",
        tool="echo",
        args={"msg": "hi"},
        timeout_ms=5000,
    )
    text = original.to_json()
    assert '"kind":"invoke"' in text
    back = WsToolMessage.from_json(text)
    assert back == original


def test_accept_round_trips_with_tools() -> None:
    original = WsToolMessage.Accept(
        server_version="0.1.0",
        heartbeat_secs=15,
        supported_tools=[
            ToolAdvert(
                name="echo",
                description="returns args",
                parameters={"type": "object"},
            )
        ],
    )
    text = original.to_json()
    back = WsToolMessage.from_json(text)
    assert back == original


def test_ping_and_pong_are_symmetric() -> None:
    for m in (WsToolMessage.Ping(), WsToolMessage.Pong()):
        text = m.to_json()
        back = WsToolMessage.from_json(text)
        assert back == m


def test_result_and_error_are_distinct_kinds() -> None:
    r = WsToolMessage.Result(request_id="r", ok=True, payload=1)
    e = WsToolMessage.Error(request_id="r", code="boom", message="nope")
    assert '"kind":"result"' in r.to_json()
    assert '"kind":"error"' in e.to_json()


def test_unknown_kind_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        WsToolMessage.from_dict({"kind": "no_such_thing"})


def test_url_builder_appends_path_once() -> None:
    from corlinman_wstool.client import build_connect_url

    u = build_connect_url("ws://127.0.0.1:18790", "tok", "r1", "0.1.0")
    assert u == (
        "ws://127.0.0.1:18790/wstool/connect"
        "?auth_token=tok&runner_id=r1&version=0.1.0"
    )


def test_url_builder_trims_trailing_slash() -> None:
    from corlinman_wstool.client import build_connect_url

    u = build_connect_url("ws://127.0.0.1:18790/", "tok", "r1", "0.1.0")
    assert u.startswith("ws://127.0.0.1:18790/wstool/connect?")


def test_url_encoder_escapes_unreserved_only() -> None:
    from corlinman_wstool.client import url_encode

    # Reserved characters must percent-escape; safe ASCII passes through.
    assert url_encode("abc-_.~0") == "abc-_.~0"
    assert url_encode("a b") == "a%20b"
    assert url_encode("a&b=c?d#e") == "a%26b%3Dc%3Fd%23e"
