//! Tool approval queue backed by SQLite `pending_approvals`.
//
// TODO: enqueue `AwaitingApproval { call_id, plugin, tool, args_preview, session_key, reason }`;
//       admin UI polls + decides; decision is written back to the running gRPC stream.
// TODO: first-use policy: non-Bundled plugins default to `mode=prompt` on first call per
//       session_key (plan §7.8 security rule).
