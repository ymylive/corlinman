//! `AppState` — cloneable bundle of shared handles (config, agent client, plugin registry).
//
// TODO: hold `config: Arc<ArcSwap<CorlinmanConfig>>` for lock-free hot reload
//       (plan §14 R10); every handler calls `state.config.load()` at entry.
// TODO: include `agent: corlinman_agent_client::AgentClient`,
//       `plugins: corlinman_plugins::Registry`, `vector: corlinman_vector::Store`,
//       `approvals: ApprovalQueue`, and a broadcast `events: tokio::sync::broadcast::Sender<Event>`.
