//! Log stream outbound broadcast helper.
//
// TODO: wrap `tokio::sync::broadcast::Sender<LogEvent>` so plugin runtime,
//       scheduler, and chat route can all publish without knowing about subscribers.
// TODO: emit structured JSON frames (plan §12 websocket-compat).
