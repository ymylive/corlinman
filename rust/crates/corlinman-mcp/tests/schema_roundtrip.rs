//! Iter 1 — JSON-RPC envelope + MCP capability payload serde round-trip tests.
//!
//! The schema crate is the wire surface shared with the C2 outbound
//! client. Every variant here gets a serialize-then-deserialize check
//! plus at least one explicit-shape assertion against the spec.

use corlinman_mcp::schema::{
    error_codes, prompts, resources, tools, ClientCapabilities, Implementation, InitializeParams,
    InitializeResult, JsonRpcError, JsonRpcRequest, JsonRpcResponse, PromptsCapability,
    ResourcesCapability, ServerCapabilities, ToolsCapability, JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
};
use serde_json::{json, Value};

#[test]
fn jsonrpc_request_with_id_round_trips() {
    let req = JsonRpcRequest {
        jsonrpc: JSONRPC_VERSION.into(),
        id: Some(json!("req-1")),
        method: "tools/list".into(),
        params: json!({}),
    };
    let wire = serde_json::to_value(&req).unwrap();
    assert_eq!(wire["jsonrpc"], "2.0");
    assert_eq!(wire["id"], "req-1");
    assert_eq!(wire["method"], "tools/list");
    let back: JsonRpcRequest = serde_json::from_value(wire).unwrap();
    assert_eq!(back, req);
    assert!(!back.is_notification());
}

#[test]
fn jsonrpc_notification_omits_id_field() {
    // Per JSON-RPC 2.0 §4 a *missing* `id` makes the frame a
    // notification. Round-trip must preserve the missing-vs-null
    // distinction.
    let req = JsonRpcRequest {
        jsonrpc: JSONRPC_VERSION.into(),
        id: None,
        method: "notifications/initialized".into(),
        params: Value::Null,
    };
    let wire = serde_json::to_value(&req).unwrap();
    assert!(wire.get("id").is_none(), "id must be omitted, got {wire:?}");
    let back: JsonRpcRequest = serde_json::from_value(wire).unwrap();
    assert_eq!(back, req);
    assert!(back.is_notification());
}

#[test]
fn jsonrpc_request_rejects_wrong_version() {
    let bad = json!({
        "jsonrpc": "1.0",
        "id": "x",
        "method": "tools/list",
    });
    let res: Result<JsonRpcRequest, _> = serde_json::from_value(bad);
    assert!(
        res.is_err(),
        "expected jsonrpc=1.0 to be rejected, got {res:?}"
    );
}

#[test]
fn jsonrpc_response_result_round_trip_keeps_jsonrpc_literal() {
    let resp = JsonRpcResponse::ok(json!("req-1"), json!({"ok": true}));
    let wire = serde_json::to_value(&resp).unwrap();
    assert_eq!(wire["jsonrpc"], "2.0");
    assert_eq!(wire["id"], "req-1");
    assert_eq!(wire["result"], json!({"ok": true}));
    assert!(
        wire.get("error").is_none(),
        "result+error are mutually exclusive"
    );
    let back: JsonRpcResponse = serde_json::from_value(wire).unwrap();
    assert_eq!(back, resp);
}

#[test]
fn jsonrpc_response_error_round_trip_carries_data() {
    let err = JsonRpcError::new(error_codes::INVALID_PARAMS, "bad name")
        .with_data(json!({"name": "bogus"}));
    let resp = JsonRpcResponse::err(json!(7), err);
    let wire = serde_json::to_value(&resp).unwrap();
    assert_eq!(wire["jsonrpc"], "2.0");
    assert_eq!(wire["id"], 7);
    assert_eq!(wire["error"]["code"], -32602);
    assert_eq!(wire["error"]["message"], "bad name");
    assert_eq!(wire["error"]["data"], json!({"name": "bogus"}));
    assert!(wire.get("result").is_none());
    let back: JsonRpcResponse = serde_json::from_value(wire).unwrap();
    assert_eq!(back, resp);
}

#[test]
fn jsonrpc_error_omits_data_when_none() {
    let resp = JsonRpcResponse::err(
        json!(1),
        JsonRpcError::new(error_codes::METHOD_NOT_FOUND, "no method"),
    );
    let wire = serde_json::to_value(&resp).unwrap();
    assert!(
        wire["error"].get("data").is_none(),
        "data should be elided when None: {wire:?}"
    );
}

#[test]
fn initialize_handshake_round_trips_both_directions() {
    let params = InitializeParams {
        protocol_version: MCP_PROTOCOL_VERSION.into(),
        capabilities: ClientCapabilities::default(),
        client_info: Implementation {
            name: "claude-desktop".into(),
            version: "0.7.0".into(),
        },
    };
    let wire = serde_json::to_value(&params).unwrap();
    assert_eq!(wire["protocolVersion"], "2024-11-05");
    assert_eq!(wire["clientInfo"]["name"], "claude-desktop");
    let back: InitializeParams = serde_json::from_value(wire).unwrap();
    assert_eq!(back, params);

    let result = InitializeResult {
        protocol_version: MCP_PROTOCOL_VERSION.into(),
        capabilities: ServerCapabilities {
            tools: Some(ToolsCapability::default()),
            resources: Some(ResourcesCapability {
                subscribe: Some(false),
                list_changed: None,
            }),
            prompts: Some(PromptsCapability::default()),
        },
        server_info: Implementation {
            name: "corlinman".into(),
            version: "0.1.0".into(),
        },
    };
    let wire = serde_json::to_value(&result).unwrap();
    assert_eq!(wire["serverInfo"]["name"], "corlinman");
    assert_eq!(wire["capabilities"]["resources"]["subscribe"], false);
    assert!(
        wire["capabilities"]["tools"].is_object(),
        "tools cap must be present-but-empty object: {wire}"
    );
    let back: InitializeResult = serde_json::from_value(wire).unwrap();
    assert_eq!(back, result);
}

#[test]
fn tools_list_result_serializes_input_schema_camel_case() {
    let result = tools::ListResult {
        tools: vec![tools::ToolDescriptor {
            name: "kb:search".into(),
            description: Some("search the kb".into()),
            input_schema: json!({"type": "object", "properties": {}}),
        }],
        next_cursor: None,
    };
    let wire = serde_json::to_value(&result).unwrap();
    assert_eq!(wire["tools"][0]["name"], "kb:search");
    // MCP wire format: camelCase field name `inputSchema`.
    assert!(
        wire["tools"][0]["inputSchema"].is_object(),
        "inputSchema must be camelCase, got {wire}"
    );
    assert!(
        wire["tools"][0].get("input_schema").is_none(),
        "snake_case must not leak: {wire}"
    );
    let back: tools::ListResult = serde_json::from_value(wire).unwrap();
    assert_eq!(back, result);
}

#[test]
fn tools_call_result_text_content_and_is_error_flag() {
    let result = tools::CallResult {
        content: vec![tools::Content::text("hello world")],
        is_error: false,
    };
    let wire = serde_json::to_value(&result).unwrap();
    assert_eq!(wire["content"][0]["type"], "text");
    assert_eq!(wire["content"][0]["text"], "hello world");
    assert_eq!(wire["isError"], false);
    let back: tools::CallResult = serde_json::from_value(wire).unwrap();
    assert_eq!(back, result);

    // is_error true variant — runtime errors surface here, not in
    // JSON-RPC error frame.
    let errored = tools::CallResult {
        content: vec![tools::Content::text("kaboom")],
        is_error: true,
    };
    let wire = serde_json::to_value(&errored).unwrap();
    assert_eq!(wire["isError"], true);
}

#[test]
fn resources_list_and_read_round_trip() {
    let listing = resources::ListResult {
        resources: vec![resources::Resource {
            uri: "corlinman://skill/foo".into(),
            name: "foo".into(),
            description: Some("a skill".into()),
            mime_type: Some("text/markdown".into()),
        }],
        next_cursor: Some("page-2".into()),
    };
    let wire = serde_json::to_value(&listing).unwrap();
    assert_eq!(wire["resources"][0]["uri"], "corlinman://skill/foo");
    assert_eq!(wire["resources"][0]["mimeType"], "text/markdown");
    assert_eq!(wire["nextCursor"], "page-2");
    let back: resources::ListResult = serde_json::from_value(wire).unwrap();
    assert_eq!(back, listing);

    let read = resources::ReadResult {
        contents: vec![resources::ResourceContent::text(
            "corlinman://skill/foo",
            "# body",
        )],
    };
    let wire = serde_json::to_value(&read).unwrap();
    assert_eq!(wire["contents"][0]["uri"], "corlinman://skill/foo");
    assert_eq!(wire["contents"][0]["text"], "# body");
    let back: resources::ReadResult = serde_json::from_value(wire).unwrap();
    assert_eq!(back, read);
}

#[test]
fn prompts_list_and_get_round_trip() {
    let listing = prompts::ListResult {
        prompts: vec![prompts::Prompt {
            name: "review-pr".into(),
            description: Some("review a PR".into()),
            arguments: vec![],
        }],
        next_cursor: None,
    };
    let wire = serde_json::to_value(&listing).unwrap();
    assert_eq!(wire["prompts"][0]["name"], "review-pr");
    // Empty arguments array elided per Vec::is_empty skip rule.
    assert!(
        wire["prompts"][0].get("arguments").is_none(),
        "empty arguments should be elided: {wire}"
    );
    let back: prompts::ListResult = serde_json::from_value(wire).unwrap();
    assert_eq!(back, listing);

    let get_result = prompts::GetResult {
        description: Some("review a PR".into()),
        messages: vec![prompts::PromptMessage {
            role: prompts::PromptRole::User,
            content: prompts::PromptContent::text("# body"),
        }],
    };
    let wire = serde_json::to_value(&get_result).unwrap();
    assert_eq!(wire["messages"][0]["role"], "user");
    assert_eq!(wire["messages"][0]["content"]["type"], "text");
    assert_eq!(wire["messages"][0]["content"]["text"], "# body");
    let back: prompts::GetResult = serde_json::from_value(wire).unwrap();
    assert_eq!(back, get_result);
}

#[test]
fn standard_error_codes_match_jsonrpc_2_0_spec() {
    // Pin the on-the-wire numeric codes — these are referenced from
    // the test matrix and from any client implementing the spec.
    assert_eq!(error_codes::PARSE_ERROR, -32700);
    assert_eq!(error_codes::INVALID_REQUEST, -32600);
    assert_eq!(error_codes::METHOD_NOT_FOUND, -32601);
    assert_eq!(error_codes::INVALID_PARAMS, -32602);
    assert_eq!(error_codes::INTERNAL_ERROR, -32603);
    // corlinman extensions live in the implementation-defined range.
    assert!((-32099..=-32000).contains(&error_codes::TOOL_NOT_ALLOWED));
    assert!((-32099..=-32000).contains(&error_codes::SESSION_NOT_INITIALIZED));
}
