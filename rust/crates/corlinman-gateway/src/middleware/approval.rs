//! Tool-approval middleware (runs on ToolCall frames, not HTTP requests).
//
// TODO: on each outgoing ToolCall from the agent, consult `ToolApprovalConfig`
//       (plan §7.6); if mode=prompt, persist to SQLite `pending_approvals` and
//       emit `AwaitingApproval` frame instead of forwarding.
// TODO: expose `decide(decision: ApprovalDecision)` for the admin UI route handler.
