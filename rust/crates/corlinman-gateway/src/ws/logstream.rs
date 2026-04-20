//! Log stream WebSocket: `GET /logstream?token=<token>`.
//
// TODO: authenticate via query string `token` or `Authorization` header;
//       attach to the `state.events` broadcast and forward as structured JSON frames.
// TODO: support tail/resume semantics so reconnects don't miss the last N messages.
