//! `mcp-cli-smoke` — ad-hoc smoke against a running MCP `/mcp` endpoint.
//!
//! Spec'd by the C1 design § "Implementation order" iter 10:
//!
//! > Plus `cargo run --example mcp-cli-smoke` for ad-hoc debugging
//! > against the spec's reference client.
//!
//! Walks the canonical handshake → list-each-capability cycle and
//! pretty-prints every server frame. Useful for poking at a real
//! gateway during development; not part of the test surface.
//!
//! Usage:
//!
//! ```sh
//! cargo run -p corlinman-mcp --example mcp-cli-smoke -- \
//!   --url ws://127.0.0.1:18791/mcp --token <opaque>
//! ```
//!
//! Exit codes:
//!   0 — every step succeeded
//!   1 — connect / handshake failed
//!   2 — server returned an unexpected error frame at any step

use std::process::ExitCode;

use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::Message as TgMessage;

#[derive(Parser, Debug)]
#[command(version, about = "Smoke-walk an MCP /mcp WebSocket endpoint.", long_about = None)]
struct Cli {
    /// Full WebSocket URL (typically `ws://127.0.0.1:18791/mcp`).
    #[arg(long)]
    url: String,
    /// Bearer token. Appended as `?token=<token>` to the URL.
    #[arg(long)]
    token: String,
    /// Optional `tools/call` target — `<plugin>:<tool>` form. Skipped
    /// when omitted.
    #[arg(long)]
    call: Option<String>,
    /// JSON arguments object for `--call`. Default `{}`.
    #[arg(long, default_value = "{}")]
    call_args: String,
}

fn pretty(v: &Value) -> String {
    serde_json::to_string_pretty(v).unwrap_or_else(|_| v.to_string())
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    let url = if cli.url.contains('?') {
        format!("{}&token={}", cli.url, cli.token)
    } else {
        format!("{}?token={}", cli.url, cli.token)
    };
    eprintln!("connecting to {url}...");
    let (mut ws, resp) = match tokio_tungstenite::connect_async(&url).await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("connect failed: {e}");
            return ExitCode::from(1);
        }
    };
    eprintln!("upgraded ({} {})", resp.status().as_u16(), resp.status().canonical_reason().unwrap_or(""));

    // initialize
    let init = json!({
        "jsonrpc": "2.0",
        "id": "smoke-init",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-cli-smoke", "version": env!("CARGO_PKG_VERSION")}
        }
    });
    if let Err(e) = ws.send(TgMessage::Text(init.to_string())).await {
        eprintln!("send initialize failed: {e}");
        return ExitCode::from(1);
    }
    let init_reply = match next_text(&mut ws).await {
        Some(t) => t,
        None => return ExitCode::from(1),
    };
    println!("== initialize ==\n{}", pretty(&init_reply));
    if init_reply.get("error").is_some() {
        return ExitCode::from(2);
    }

    let initd = json!({"jsonrpc": "2.0", "method": "notifications/initialized"});
    let _ = ws.send(TgMessage::Text(initd.to_string())).await;

    // tools/list
    if let Some(reply) = round_trip(&mut ws, "tools/list", json!({}), "smoke-tools-1").await {
        println!("== tools/list ==\n{}", pretty(&reply));
    }
    // resources/list
    if let Some(reply) = round_trip(&mut ws, "resources/list", json!({}), "smoke-res-1").await {
        println!("== resources/list ==\n{}", pretty(&reply));
    }
    // prompts/list
    if let Some(reply) = round_trip(&mut ws, "prompts/list", json!({}), "smoke-pr-1").await {
        println!("== prompts/list ==\n{}", pretty(&reply));
    }

    // optional tools/call
    if let Some(name) = cli.call {
        let args: Value = serde_json::from_str(&cli.call_args).unwrap_or(json!({}));
        let params = json!({"name": name, "arguments": args});
        if let Some(reply) = round_trip(&mut ws, "tools/call", params, "smoke-tools-call").await {
            println!("== tools/call ==\n{}", pretty(&reply));
        }
    }

    let _ = ws.close(None).await;
    ExitCode::SUCCESS
}

async fn round_trip<S>(
    ws: &mut tokio_tungstenite::WebSocketStream<S>,
    method: &str,
    params: Value,
    id: &str,
) -> Option<Value>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let req = json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": params
    });
    if let Err(e) = ws.send(TgMessage::Text(req.to_string())).await {
        eprintln!("send {method} failed: {e}");
        return None;
    }
    next_text(ws).await
}

async fn next_text<S>(ws: &mut tokio_tungstenite::WebSocketStream<S>) -> Option<Value>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    while let Some(frame) = ws.next().await {
        match frame.ok()? {
            TgMessage::Text(t) => return serde_json::from_str(&t).ok(),
            TgMessage::Binary(_) | TgMessage::Ping(_) | TgMessage::Pong(_) => continue,
            TgMessage::Close(_) | TgMessage::Frame(_) => return None,
        }
    }
    None
}
