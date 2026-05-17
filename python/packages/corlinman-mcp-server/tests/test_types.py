"""JSON-RPC envelope + MCP capability payload serde round-trip tests.

Mirrors the Rust ``tests/schema_roundtrip.rs`` cases 1:1 — every variant
gets a serialize-then-parse check plus at least one explicit-shape
assertion against the spec.
"""

from __future__ import annotations

import json

import pytest

from corlinman_mcp_server import (
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
    ClientCapabilities,
    Implementation,
    InitializeParams,
    InitializeResult,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    Prompt,
    PromptMessage,
    PromptRole,
    PromptsCapability,
    PromptsGetResult,
    PromptsListResult,
    Resource,
    ResourcesCapability,
    ResourcesListResult,
    ResourcesReadResult,
    ServerCapabilities,
    TextContent,
    TextResourceContent,
    ToolDescriptor,
    ToolsCallResult,
    ToolsCapability,
    ToolsListResult,
    error_codes,
    prompt_text_content,
    text_content,
    text_resource,
)


def _roundtrip_request(req: JsonRpcRequest) -> dict:
    return req.model_dump()


def test_jsonrpc_request_with_id_round_trips():
    req = JsonRpcRequest(
        jsonrpc=JSONRPC_VERSION,
        id="req-1",
        method="tools/list",
        params={},
    )
    wire = _roundtrip_request(req)
    assert wire["jsonrpc"] == "2.0"
    assert wire["id"] == "req-1"
    assert wire["method"] == "tools/list"
    back = JsonRpcRequest.model_validate(wire)
    assert back == req
    assert not back.is_notification()


def test_jsonrpc_notification_omits_id_field():
    req = JsonRpcRequest(
        jsonrpc=JSONRPC_VERSION,
        id=None,
        method="notifications/initialized",
        params=None,
    )
    wire = _roundtrip_request(req)
    assert "id" not in wire, f"id must be omitted, got {wire!r}"
    back = JsonRpcRequest.model_validate(wire)
    assert back == req
    assert back.is_notification()


def test_jsonrpc_request_rejects_wrong_version():
    with pytest.raises(Exception):
        JsonRpcRequest.model_validate(
            {"jsonrpc": "1.0", "id": "x", "method": "tools/list"}
        )


def test_jsonrpc_response_result_round_trip():
    resp = JsonRpcResponse.ok("req-1", {"ok": True})
    wire = resp.model_dump()
    assert wire["jsonrpc"] == "2.0"
    assert wire["id"] == "req-1"
    assert wire["result"] == {"ok": True}
    assert "error" not in wire
    back = JsonRpcResponse.parse(wire)
    assert back.id == "req-1"
    assert back.result == {"ok": True}


def test_jsonrpc_response_error_round_trip_carries_data():
    err = JsonRpcError.new(error_codes.INVALID_PARAMS, "bad name").with_data(
        {"name": "bogus"}
    )
    resp = JsonRpcResponse.err(7, err)
    wire = resp.model_dump()
    assert wire["jsonrpc"] == "2.0"
    assert wire["id"] == 7
    assert wire["error"]["code"] == -32602
    assert wire["error"]["message"] == "bad name"
    assert wire["error"]["data"] == {"name": "bogus"}
    assert "result" not in wire
    back = JsonRpcResponse.parse(wire)
    assert back.error.code == -32602


def test_jsonrpc_error_omits_data_when_none():
    resp = JsonRpcResponse.err(
        1, JsonRpcError.new(error_codes.METHOD_NOT_FOUND, "no method")
    )
    wire = resp.model_dump()
    assert "data" not in wire["error"], f"data should be elided when None: {wire!r}"


def test_initialize_handshake_round_trips_both_directions():
    params = InitializeParams(
        protocolVersion=MCP_PROTOCOL_VERSION,
        capabilities=ClientCapabilities(),
        clientInfo=Implementation(name="claude-desktop", version="0.7.0"),
    )
    wire = params.model_dump()
    assert wire["protocolVersion"] == "2024-11-05"
    assert wire["clientInfo"]["name"] == "claude-desktop"
    back = InitializeParams.model_validate(wire)
    assert back.protocol_version == params.protocol_version
    assert back.client_info.name == "claude-desktop"

    result = InitializeResult(
        protocolVersion=MCP_PROTOCOL_VERSION,
        capabilities=ServerCapabilities(
            tools=ToolsCapability(),
            resources=ResourcesCapability(subscribe=False),
            prompts=PromptsCapability(),
        ),
        serverInfo=Implementation(name="corlinman", version="0.1.0"),
    )
    wire = result.model_dump()
    assert wire["serverInfo"]["name"] == "corlinman"
    assert wire["capabilities"]["resources"]["subscribe"] is False
    assert isinstance(wire["capabilities"]["tools"], dict)
    back = InitializeResult.model_validate(wire)
    assert back.server_info.name == "corlinman"


def test_tools_list_result_serializes_input_schema_camel_case():
    result = ToolsListResult(
        tools=[
            ToolDescriptor(
                name="kb:search",
                description="search the kb",
                inputSchema={"type": "object", "properties": {}},
            )
        ],
        nextCursor=None,
    )
    wire = result.model_dump()
    assert wire["tools"][0]["name"] == "kb:search"
    assert isinstance(wire["tools"][0]["inputSchema"], dict)
    assert "input_schema" not in wire["tools"][0]
    back = ToolsListResult.model_validate(wire)
    assert back.tools[0].name == "kb:search"


def test_tools_call_result_text_content_and_is_error_flag():
    result = ToolsCallResult(
        content=[text_content("hello world")],
        isError=False,
    )
    wire = result.model_dump()
    assert wire["content"][0]["type"] == "text"
    assert wire["content"][0]["text"] == "hello world"
    assert wire["isError"] is False
    back = ToolsCallResult.model_validate(wire)
    assert isinstance(back.content[0], TextContent)
    assert back.content[0].text == "hello world"

    errored = ToolsCallResult(
        content=[text_content("kaboom")],
        isError=True,
    )
    wire = errored.model_dump()
    assert wire["isError"] is True


def test_resources_list_and_read_round_trip():
    listing = ResourcesListResult(
        resources=[
            Resource(
                uri="corlinman://skill/foo",
                name="foo",
                description="a skill",
                mimeType="text/markdown",
            )
        ],
        nextCursor="page-2",
    )
    wire = listing.model_dump()
    assert wire["resources"][0]["uri"] == "corlinman://skill/foo"
    assert wire["resources"][0]["mimeType"] == "text/markdown"
    assert wire["nextCursor"] == "page-2"
    back = ResourcesListResult.model_validate(wire)
    assert back.resources[0].uri == "corlinman://skill/foo"

    read = ResourcesReadResult(
        contents=[text_resource("corlinman://skill/foo", "# body")],
    )
    wire = read.model_dump()
    assert wire["contents"][0]["uri"] == "corlinman://skill/foo"
    assert wire["contents"][0]["text"] == "# body"
    back = ResourcesReadResult.model_validate(wire)
    assert isinstance(back.contents[0], TextResourceContent)


def test_prompts_list_and_get_round_trip():
    listing = PromptsListResult(
        prompts=[Prompt(name="review-pr", description="review a PR", arguments=[])],
        nextCursor=None,
    )
    wire = listing.model_dump()
    assert wire["prompts"][0]["name"] == "review-pr"
    # Empty arguments list elided.
    assert "arguments" not in wire["prompts"][0]
    back = PromptsListResult.model_validate(wire)
    assert back.prompts[0].name == "review-pr"

    get_result = PromptsGetResult(
        description="review a PR",
        messages=[
            PromptMessage(
                role=PromptRole.USER,
                content=prompt_text_content("# body"),
            )
        ],
    )
    wire = get_result.model_dump()
    assert wire["messages"][0]["role"] == "user"
    assert wire["messages"][0]["content"]["type"] == "text"
    assert wire["messages"][0]["content"]["text"] == "# body"
    back = PromptsGetResult.model_validate(wire)
    assert back.messages[0].role is PromptRole.USER


def test_standard_error_codes_match_jsonrpc_spec():
    assert error_codes.PARSE_ERROR == -32700
    assert error_codes.INVALID_REQUEST == -32600
    assert error_codes.METHOD_NOT_FOUND == -32601
    assert error_codes.INVALID_PARAMS == -32602
    assert error_codes.INTERNAL_ERROR == -32603
    # corlinman extensions in -32099..-32000.
    assert -32099 <= error_codes.TOOL_NOT_ALLOWED <= -32000
    assert -32099 <= error_codes.SESSION_NOT_INITIALIZED <= -32000


def test_jsonrpc_response_payload_is_str_serialisable():
    """Sanity: every shape this package emits round-trips through
    ``json.dumps`` cleanly (the transport relies on this)."""
    resp = JsonRpcResponse.ok("x", {"k": [1, 2, 3]})
    s = json.dumps(resp.model_dump())
    assert "result" in s
