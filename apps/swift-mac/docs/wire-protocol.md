# Wire protocol — corlinman native client

**Status**: Phase 4 W3 C4 iter 10 — extracted from the Mac client's
working SSE parser (`Sources/CorlinmanCore/ChatStream.swift`,
`Sources/CorlinmanCore/Models.swift`). Language-neutral.

This page is the contract a native chat client implements. It mirrors
what `routes/chat.rs` (gateway) emits and `routes/chat_approve.rs`
(gateway) accepts. Authoritative source: the Rust gateway. This file is
a reference, not a source of truth — when the gateway evolves, this
file gets updated alongside.

## Overview

Three endpoints carry every interactive turn:

```
1. POST /v1/chat/completions               — open a streaming turn
2. SSE: data: {…} … data: [DONE]           — token + tool-call deltas, approval
3. POST /v1/chat/completions/<id>/approve  — operator decision on a tool call
```

Auth: every `/v1/*` call carries `Authorization: Bearer <chat-scoped api_key>`.
See [`auth-flow.md`](auth-flow.md) for the mint flow.

## 1. Chat-completion request

Standard OpenAI-style request body, plus one corlinman extension:
`session_key` for stable session correlation.

```http
POST /v1/chat/completions HTTP/1.1
Host: gateway.example.com
Authorization: Bearer ck_live_abc123…
Content-Type: application/json
Accept: text/event-stream

{
  "model": "corlinman/chat",
  "stream": true,
  "messages": [
    { "role": "user", "content": "What's the weather in SF?" }
  ],
  "session_key": "session-uuid-or-stable-id",
  "tools": [ … OpenAI tool spec … ]      // optional, gateway may inject
}
```

Notes for porters:

- `stream: true` — non-streaming is supported but the native UX expects
  token-level rendering. Stick with streaming.
- `session_key` is a string the client picks. Stable across resumes
  for the same conversation — the gateway maps it to its
  `sessions` table. New conversation → new uuid.
- The gateway inserts the system prompt; clients pass only user / prior
  assistant turns. Resumes re-send prior turns from local cache.

## 2. SSE response

### Framing rules (subset of RFC 8895 we use)

- Lines terminated by `\n` (gateway sends LF; clients tolerate `\r\n`).
- Blank line dispatches the buffered event.
- `data: <json-or-[DONE]>` accumulates the data buffer.
- `event: <name>` sets the next event's name (default empty).
- `id:`, `retry:`, comments (`:` prefix), unknown fields → ignored.
- Terminal `data: [DONE]` closes the stream.

The Mac client's parser is 60 lines in `ChatStream.swift`. Reimplement
in your language; don't pull a heavy SSE library for this.

### Chunk envelope (default `event: message`)

OpenAI's `chat.completion.chunk` shape, with one or more `choices[].delta`
slots per frame. Token deltas, tool-call deltas, and finish reasons
all share this envelope.

```json
{
  "id":     "turn-id-string",
  "object": "chat.completion.chunk",
  "model":  "corlinman/chat",
  "choices": [
    {
      "index": 0,
      "delta": {
        "role":       "assistant",      // present on the first chunk only
        "content":    "Hello, ",        // token delta
        "tool_calls": [                  // present when the model emits a tool call
          {
            "index":    0,               // monotonic per call within the turn
            "id":       "call_abc123",   // OpenAI-style call id
            "type":     "function",
            "function": {
              "name":      "shell.run",  // present on the first slot only
              "arguments": "{\"cmd\""    // chunked across multiple slots
            }
          }
        ]
      },
      "finish_reason": null              // "stop" | "length" | "tool_calls" | "error"
    }
  ]
}
```

**Tool-call accumulation** — OpenAI's spec splits a single tool call
across multiple deltas. Each delta has the same `index`, and only the
first slot carries `id` + `function.name`. Subsequent slots only carry
`function.arguments` fragments which the client concatenates into the
full JSON arguments string. The full call is only valid when
`finish_reason == "tool_calls"`.

### Custom event: `awaiting_approval`

When the agent stalls on `AwaitingApproval` (`agent.proto:137-143`),
the gateway emits a custom-named SSE event interleaved with the normal
chunk stream:

```text
event: awaiting_approval
data: {
  "turn_id":      "turn-id-string",
  "call_id":      "call_abc123",
  "plugin":       "shell",
  "tool":         "run",
  "args_preview": "ls /etc"
}
```

Native clients render an approval sheet from these fields and hold the
stream open — the gateway resumes emitting chunks once the operator
POSTs a decision (see §3 below). If the user cancels the stream
(URLSession cancel), the gateway propagates a `Cancel` to the agent
and the approval is implicitly denied.

### Terminator

```text
data: [DONE]

```

Closes the SSE stream. Anything after is ignored.

### Keep-alive

The gateway emits comment-only frames (`: keepalive\n\n`) every ~15s
when the agent is in a long synchronous tool call. Parsers ignore
these (per SSE spec) — they exist only to keep proxies from idle-
killing the connection.

## 3. Approval round-trip

Once the operator picks approve / deny, the client POSTs the decision
to a separate endpoint scoped to the same turn. The bearer stays the
same chat-scoped api_key — no admin creds needed.

```http
POST /v1/chat/completions/turn-id-string/approve HTTP/1.1
Host: gateway.example.com
Authorization: Bearer ck_live_abc123…
Content-Type: application/json

{
  "call_id":      "call_abc123",
  "approved":     true,
  "scope":        "once",                  // "once" | "session" | "always"
  "deny_message": null                      // required when approved=false
}
```

Response:

```json
{
  "turn_id":  "turn-id-string",
  "call_id":  "call_abc123",
  "decision": "approved"               // "approved" | "denied"
}
```

Notes:

- `scope` is forward-compatible. Today the gateway treats `session`
  and `always` as `once` (`chat_approve.rs:50-54`); when scope-tracking
  lands, the wire shape stays the same.
- `deny_message` is optional even on deny. Surface a textbox in your
  UI; let the user leave it blank.
- The SSE stream continues emitting after the POST returns; do **not**
  close the stream when you submit. Cancellation is the only thing
  that closes it.

## 4. Cancellation

The client cancels by tearing down the HTTP transport (URLSession's
data task cancel, OkHttp's `Call.cancel()`, etc.). The gateway sees
the disconnect and propagates `Cancel` to the agent (`agent.proto:94-97`).
There is **no** explicit cancel RPC — HTTP-level disconnect is the
contract.

## 5. Error responses

Pre-stream errors come back as plain HTTP, not SSE:

| Status | Meaning | Client action |
|---|---|---|
| `400` | Malformed body, unknown model, invalid `session_key`. | Surface error inline; don't retry. |
| `401` | Bearer rejected. | Re-mint; if mint also fails, bounce to onboarding. |
| `402` | Payment / quota required (future). | Surface upsell; freeze the composer. |
| `403` | Tenant doesn't own this api_key. | Wipe local cache for this tenant, re-onboard. |
| `429` | Rate-limit. `Retry-After` header. | Disable composer until retry. |
| `5xx` | Gateway transient. | Show "transient — retry"; keep the bearer. |

Mid-stream errors are surfaced as a final chunk with
`choices[0].finish_reason = "error"` followed by `[DONE]`. The
client treats this as a stream-end with an error indicator.

## 6. Push notifications (out-of-band)

When the agent finishes long-running work while the user is away, a
push notification arrives via APNs (production) or a Unix socket (dev).
Schema is shared:

```json
{
  "id":            "notif-uuid",
  "tenant_id":     "tenant-uuid",
  "user_id":       "canonical-user-id",
  "kind":          "PUSH_KIND_TASK_COMPLETED",
  "title":         "Skill evolution applied",
  "body":          "Pinned: shell.run-grep",
  "deep_link":     "corlinman://session/<key>",
  "created_at_ms": 1746812345678
}
```

The Mac client decodes both directly-embedded payloads (gateway sends
our schema inside the `aps`-extension namespace) and APNs canonical
form (`aps.alert.title` + custom keys). Porters should handle both —
some APNs gateway providers flatten custom keys.

Device-token registration (sending the APNs token to the gateway via
`POST /v1/devices`) is **not yet wired** — flagged as a Phase 5
follow-up. Until then, dev pushes via Unix socket are the only
exercised path.

## 7. Reference SSE transcript

A full streaming turn that calls a tool and gets approved. Lines split
for readability — actual on-the-wire format has each `data:` chunk on
one line:

```text
data: {"id":"turn-7","object":"chat.completion.chunk","model":"corlinman/chat",
  "choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"turn-7","object":"chat.completion.chunk","model":"corlinman/chat",
  "choices":[{"index":0,"delta":{"content":"Let me check… "},"finish_reason":null}]}

data: {"id":"turn-7","object":"chat.completion.chunk","model":"corlinman/chat",
  "choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_99",
    "type":"function","function":{"name":"shell.run"}}]},"finish_reason":null}]}

data: {"id":"turn-7","object":"chat.completion.chunk","model":"corlinman/chat",
  "choices":[{"index":0,"delta":{"tool_calls":[{"index":0,
    "function":{"arguments":"{\"cmd\":\""}}]},"finish_reason":null}]}

data: {"id":"turn-7","object":"chat.completion.chunk","model":"corlinman/chat",
  "choices":[{"index":0,"delta":{"tool_calls":[{"index":0,
    "function":{"arguments":"ls /etc\"}"}}]},"finish_reason":null}]}

event: awaiting_approval
data: {"turn_id":"turn-7","call_id":"call_99","plugin":"shell","tool":"run","args_preview":"ls /etc"}

# ← client renders sheet, operator approves, client POSTs:
#   POST /v1/chat/completions/turn-7/approve
#   { "call_id":"call_99", "approved":true, "scope":"once" }

data: {"id":"turn-7","object":"chat.completion.chunk","model":"corlinman/chat",
  "choices":[{"index":0,"delta":{"content":"\nResult: foo bar"},"finish_reason":null}]}

data: {"id":"turn-7","object":"chat.completion.chunk","model":"corlinman/chat",
  "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]

```

A correctly-implemented client renders, in order:

1. The user message (echo, locally).
2. Streaming assistant text: `"Let me check… "`.
3. A "tool call: shell.run" indicator.
4. An approval sheet — modal — with `args_preview = "ls /etc"`.
5. After approve: continued streaming `"\nResult: foo bar"`.
6. End-of-turn signal; persist user + assistant messages locally.
