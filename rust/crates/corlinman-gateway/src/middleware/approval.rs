//! Tool-approval gate (Sprint 2 T3).
//!
//! The gate runs **between** a `ServerFrame::ToolCall` arriving from Python
//! and the plugin runtime executing it. For every call it consults the
//! configured [`ApprovalRule`] list:
//!
//! - `Auto` — immediately approved.
//! - `Deny` — immediately denied with the matching rule's reason.
//! - `Prompt` — a row is written to `pending_approvals`, a broadcast
//!   `ApprovalEvent::Pending` is emitted (so the admin UI's SSE
//!   subscribers see it), and the call blocks on a `oneshot` until an
//!   operator calls [`ApprovalGate::resolve`] (via
//!   `POST /admin/approvals/:id/decide`) or the configured timeout
//!   elapses.
//!
//! Session-key bypass: a `Prompt` rule whose `allow_session_keys` contains
//! the call's `session_key` auto-approves without prompting — handy for
//! trusted internal sessions (e.g. scheduler jobs) while still gating
//! human-facing channels.
//!
//! The gate owns the pending oneshot map (a `DashMap`), so it is safe to
//! clone (`Arc<Self>`) and hand to both the chat hot path and the admin
//! REST routes without any further synchronisation. Dropping a clone does
//! not leak pending decisions: the `check` future removes its entry from
//! the map on every exit path.

use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use corlinman_core::config::{ApprovalMode, ApprovalRule};
use corlinman_core::CorlinmanError;
use corlinman_hooks::{HookBus, HookEvent};
use corlinman_vector::{PendingApproval, SqliteStore};
use dashmap::DashMap;
use tokio::sync::{broadcast, oneshot};
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};
use uuid::Uuid;

/// Default `Prompt` wait deadline when the caller doesn't override it.
/// Matches the 5-minute figure documented in the Sprint 2 roadmap.
pub const DEFAULT_PROMPT_TIMEOUT: Duration = Duration::from_secs(300);

/// Capacity of the broadcast channel that surfaces approval events to
/// SSE subscribers and metrics collectors. A slow consumer that lags by
/// more than this many events will miss older messages — they'll still
/// see the correct live queue via the `GET /admin/approvals` polling
/// companion of the stream.
const EVENT_BROADCAST_CAPACITY: usize = 256;

/// Outcome of [`ApprovalGate::check`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ApprovalDecision {
    /// Rule matched `Auto`, or a `Prompt` rule was satisfied by session
    /// key whitelist, or an operator answered approve.
    Approved,
    /// Rule matched `Deny`, or an operator answered reject. The string
    /// carries a human-readable reason (empty = generic).
    Denied(String),
    /// Wait exceeded the configured timeout before any decision arrived.
    Timeout,
}

impl ApprovalDecision {
    /// Stable DB column value. Mirrored by
    /// `corlinman_vector::PendingApproval::decision`.
    pub fn db_label(&self) -> &'static str {
        match self {
            Self::Approved => "approved",
            Self::Denied(_) => "denied",
            Self::Timeout => "timeout",
        }
    }
}

/// Event broadcast to SSE subscribers when the approval queue changes.
/// Cheap to clone because `PendingApproval` is just a bag of `String`s.
#[derive(Clone, Debug)]
pub enum ApprovalEvent {
    /// A new `Prompt` row was inserted and is awaiting an operator.
    Pending(PendingApproval),
    /// An existing row was resolved (approved / denied / timed out).
    Decided {
        id: String,
        decision: ApprovalDecision,
    },
}

/// The gate itself.
///
/// Constructed from the current `ApprovalsConfig.rules` snapshot (wrapped
/// in `ArcSwap` so hot-reload in a later milestone can swap it in place
/// without recreating the gate) and a `SqliteStore` already migrated to
/// schema v3.
#[derive(Clone)]
pub struct ApprovalGate {
    rules: Arc<ArcSwap<Vec<ApprovalRule>>>,
    store: Arc<SqliteStore>,
    broadcaster: broadcast::Sender<ApprovalEvent>,
    pending: Arc<DashMap<String, oneshot::Sender<ApprovalDecision>>>,
    default_timeout: Duration,
    /// Optional unified hook bus (B4-BE6). When `Some`, every `Pending` /
    /// `Decided` broadcast is mirrored to `HookEvent::ApprovalRequested` /
    /// `ApprovalDecided` so cross-component subscribers (python bridge,
    /// admin UI aggregation layer) observe approvals without reaching into
    /// this crate's internal `broadcast::Sender`. `None` preserves
    /// pre-B4-BE6 behaviour exactly.
    bus: Option<Arc<HookBus>>,
}

impl ApprovalGate {
    /// Build a new gate. `rules` is cloned into an `ArcSwap` the caller can
    /// later swap out with [`Self::swap_rules`] when config reloads.
    pub fn new(
        rules: Vec<ApprovalRule>,
        store: Arc<SqliteStore>,
        default_timeout: Duration,
    ) -> Self {
        let (broadcaster, _rx) = broadcast::channel(EVENT_BROADCAST_CAPACITY);
        Self {
            rules: Arc::new(ArcSwap::from_pointee(rules)),
            store,
            broadcaster,
            pending: Arc::new(DashMap::new()),
            default_timeout,
            bus: None,
        }
    }

    /// Builder: attach a shared `HookBus`. When set, every approval
    /// lifecycle transition is additionally mirrored to the bus. Additive
    /// only — the legacy `broadcast::Sender<ApprovalEvent>` still fires
    /// for pre-existing SSE subscribers (`/admin/approvals/stream`).
    pub fn with_bus(mut self, bus: Arc<HookBus>) -> Self {
        self.bus = Some(bus);
        self
    }

    /// Replace the rules snapshot (used by the live-config reload task in
    /// S2 T4). Existing in-flight waits are not disturbed — they already
    /// captured the outcome of the rule that matched their call.
    pub fn swap_rules(&self, rules: Vec<ApprovalRule>) {
        self.rules.store(Arc::new(rules));
    }

    /// Subscribe to the event stream. Returns a fresh receiver; only
    /// events published **after** subscription are delivered.
    pub fn subscribe(&self) -> broadcast::Receiver<ApprovalEvent> {
        self.broadcaster.subscribe()
    }

    /// Snapshot of the current rule list, mostly for diagnostics / tests.
    pub fn rules_snapshot(&self) -> Arc<Vec<ApprovalRule>> {
        self.rules.load_full()
    }

    /// Borrow the backing `SqliteStore`. Crate-private; the admin routes
    /// use this to serve `GET /admin/approvals` without re-opening the DB.
    pub(crate) fn store_arc(&self) -> Arc<SqliteStore> {
        self.store.clone()
    }

    /// Public accessor for the backing store, intended for integration
    /// tests that assert on the persisted queue directly. Keeping this
    /// alongside the crate-private `store_arc` documents the split: prod
    /// handlers get the narrow crate-level one; tests reach in via the
    /// `_public` name so the "careful, test-only" intent is obvious.
    pub fn store_arc_public(&self) -> Arc<SqliteStore> {
        self.store.clone()
    }

    /// Match a `(plugin, tool, session_key)` triple against the rule list.
    /// Pure function — exposed for unit tests.
    pub fn match_rule(&self, plugin: &str, tool: &str, session_key: &str) -> RuleMatch {
        match_rule_impl(&self.rules.load(), plugin, tool, session_key)
    }

    /// The heart of the gate: ask whether this tool call should execute.
    ///
    /// Control flow:
    /// 1. Run [`match_rule_impl`] over the current snapshot.
    /// 2. `MatchedAuto` / `MatchedWhitelist` → return `Approved` now.
    /// 3. `MatchedDeny` → persist a decided row (so the admin UI still
    ///    sees a log entry) and return `Denied`.
    /// 4. `MatchedPrompt` → write a pending row, emit
    ///    `ApprovalEvent::Pending`, park on a oneshot, race against both
    ///    the default timeout and the caller's cancellation token.
    ///    Whichever fires first determines the decision; the pending row
    ///    is updated in the DB accordingly.
    ///
    /// The cancellation token lets the enclosing request abort a prompt
    /// wait on client disconnect, in which case we record a `timeout`
    /// decision in the DB (the operator missed their window because the
    /// caller gave up) and surface [`CorlinmanError::Cancelled`] to the
    /// hot path.
    pub async fn check(
        &self,
        session_key: &str,
        plugin: &str,
        tool: &str,
        args_json: &[u8],
        cancel: CancellationToken,
    ) -> Result<ApprovalDecision, CorlinmanError> {
        match self.match_rule(plugin, tool, session_key) {
            RuleMatch::NoMatch | RuleMatch::MatchedAuto | RuleMatch::MatchedWhitelist => {
                Ok(ApprovalDecision::Approved)
            }
            RuleMatch::MatchedDeny { reason } => {
                // Record the denial so the history tab shows it too.
                let row = self.build_row(session_key, plugin, tool, args_json);
                if let Err(err) = self.store.insert_pending_approval(&row).await {
                    warn!(error = %err, id = %row.id, "approval.deny: insert row failed");
                }
                if let Err(err) = self
                    .store
                    .decide_approval(
                        &row.id,
                        ApprovalDecision::Denied(reason.clone()).db_label(),
                        time::OffsetDateTime::now_utc(),
                    )
                    .await
                {
                    warn!(error = %err, id = %row.id, "approval.deny: decide update failed");
                }
                // Bus mirror: fire Requested before Decided so subscribers
                // always see the full lifecycle even on instant-deny paths.
                self.emit_requested_on_bus(&row, args_json);
                let denied = ApprovalDecision::Denied(reason.clone());
                self.emit_decided_on_bus(&row.id, &denied, None);
                let _ = self.broadcaster.send(ApprovalEvent::Decided {
                    id: row.id,
                    decision: denied.clone(),
                });
                Ok(denied)
            }
            RuleMatch::MatchedPrompt => {
                self.prompt_wait(session_key, plugin, tool, args_json, cancel)
                    .await
            }
        }
    }

    async fn prompt_wait(
        &self,
        session_key: &str,
        plugin: &str,
        tool: &str,
        args_json: &[u8],
        cancel: CancellationToken,
    ) -> Result<ApprovalDecision, CorlinmanError> {
        let row = self.build_row(session_key, plugin, tool, args_json);
        let id = row.id.clone();

        // Persist first. If the DB write fails we can't guarantee the admin
        // UI will ever see this call, so we surface a storage error rather
        // than silently admitting it.
        self.store
            .insert_pending_approval(&row)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("insert pending approval: {e}")))?;

        // Register the oneshot BEFORE broadcasting, so a very fast
        // /decide call cannot race and find the entry missing.
        let (tx, rx) = oneshot::channel();
        self.pending.insert(id.clone(), tx);

        let _ = self.broadcaster.send(ApprovalEvent::Pending(row.clone()));
        // Unified bus: subscribers see the raise alongside the legacy SSE.
        self.emit_requested_on_bus(&row, args_json);
        debug!(id = %id, plugin = %plugin, tool = %tool, "approval.prompt.enqueued");

        let timeout = tokio::time::sleep(self.default_timeout);
        tokio::pin!(timeout);

        let outcome = tokio::select! {
            decision = rx => {
                match decision {
                    Ok(d) => d,
                    // Sender dropped without sending (e.g. gate torn down
                    // mid-flight). Treat like a timeout so the reasoning
                    // loop can move on.
                    Err(_) => ApprovalDecision::Timeout,
                }
            }
            _ = &mut timeout => ApprovalDecision::Timeout,
            _ = cancel.cancelled() => {
                // Caller disconnected: clear our map entry and record a
                // timeout so the UI still sees a terminal state.
                self.pending.remove(&id);
                self.persist_decision(&id, &ApprovalDecision::Timeout).await;
                // Mirror the terminal state on the bus too; legacy
                // broadcaster intentionally stays silent here (existing
                // behaviour) but the unified bus audience wants to see the
                // lifecycle close out.
                self.emit_decided_on_bus(&id, &ApprovalDecision::Timeout, None);
                return Err(CorlinmanError::Cancelled("approval wait cancelled"));
            }
        };

        // Clean up the map entry (resolve() already removed it on success,
        // but a timeout path leaves it behind).
        self.pending.remove(&id);
        self.persist_decision(&id, &outcome).await;
        let _ = self.broadcaster.send(ApprovalEvent::Decided {
            id: id.clone(),
            decision: outcome.clone(),
        });
        // Timeouts fire here too; `resolve()` handles operator-driven
        // allow/deny. Either way the bus sees exactly one Decided per id.
        if matches!(outcome, ApprovalDecision::Timeout) {
            self.emit_decided_on_bus(&id, &outcome, None);
        }
        Ok(outcome)
    }

    /// Operator-driven decision path, invoked by
    /// `POST /admin/approvals/:id/decide`.
    ///
    /// Updates the DB, fires the oneshot to wake the parked `check`
    /// future (if it's still around), and broadcasts a `Decided` event.
    /// Returns `NotFound` when the id is unknown (already decided rows
    /// still return Ok so the UI's optimistic state stays consistent if
    /// two operators click at once).
    pub async fn resolve(
        &self,
        id: &str,
        decision: ApprovalDecision,
    ) -> Result<(), CorlinmanError> {
        let row = self
            .store
            .get_pending_approval(id)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("lookup pending approval: {e}")))?
            .ok_or_else(|| CorlinmanError::NotFound {
                kind: "approval",
                id: id.to_string(),
            })?;

        // Already-decided is a no-op (idempotent).
        if row.decided_at.is_some() {
            return Ok(());
        }

        self.persist_decision(id, &decision).await;

        // Wake the waiting oneshot if this process is still the one that
        // enqueued it. Dropping the sender (not present) is expected for
        // anything that timed out first.
        if let Some((_, tx)) = self.pending.remove(id) {
            let _ = tx.send(decision.clone());
        }
        let _ = self.broadcaster.send(ApprovalEvent::Decided {
            id: id.to_string(),
            decision: decision.clone(),
        });
        // Unified bus mirror. `resolve()` owns the operator-driven
        // decision paths; the timeout path is emitted from `prompt_wait`.
        // This split guarantees exactly one `ApprovalDecided` per id.
        self.emit_decided_on_bus(id, &decision, None);
        Ok(())
    }

    /// Truncate an `args_json` payload to a safe preview size for the hook
    /// bus. Keeps the first 512 bytes and replaces the rest with `…` — the
    /// bus is fanned out to many subscribers, so avoid large clones.
    fn preview_args(args_json: &[u8]) -> String {
        const MAX: usize = 512;
        let bytes = if args_json.len() <= MAX {
            args_json
        } else {
            &args_json[..MAX]
        };
        let head = String::from_utf8_lossy(bytes).into_owned();
        if args_json.len() > MAX {
            format!("{head}…")
        } else {
            head
        }
    }

    /// Wall-clock ms since the Unix epoch. Used for `ApprovalRequested`
    /// and `ApprovalDecided` timestamps on the bus.
    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    /// Mirror an approval-raise onto the bus (if wired). The legacy local
    /// `broadcast::Sender<ApprovalEvent>` is still driven by the caller —
    /// this is an additive side channel.
    fn emit_requested_on_bus(&self, row: &PendingApproval, args_json: &[u8]) {
        let Some(bus) = &self.bus else { return };
        let timeout_at_ms = Self::now_ms().saturating_add(self.default_timeout.as_millis() as u64);
        let ev = HookEvent::ApprovalRequested {
            id: row.id.clone(),
            session_key: row.session_key.clone(),
            plugin: row.plugin.clone(),
            tool: row.tool.clone(),
            args_preview: Self::preview_args(args_json),
            timeout_at_ms,
        };
        // `emit_nonblocking` is sync and safe from any context; we don't
        // need the strict yield-between-tiers ordering here because the
        // approval path already has its own broadcast for legacy SSE.
        bus.emit_nonblocking(ev);
    }

    /// Mirror an approval-decision onto the bus (if wired).
    fn emit_decided_on_bus(&self, id: &str, decision: &ApprovalDecision, decider: Option<String>) {
        let decision_label: &'static str = match decision {
            ApprovalDecision::Approved => "allow",
            ApprovalDecision::Denied(_) => "deny",
            ApprovalDecision::Timeout => "timeout",
        };

        // Counter: always observed, even without a hook bus attached.
        corlinman_core::metrics::APPROVALS_TOTAL
            .with_label_values(&[decision_label])
            .inc();

        let Some(bus) = &self.bus else { return };
        let ev = HookEvent::ApprovalDecided {
            id: id.to_string(),
            decision: decision_label.to_string(),
            decider,
            decided_at_ms: Self::now_ms(),
            // Phase 4 W1.5 (next-tasks A1): the approval gate doesn't
            // currently carry tenant context — that's a follow-up
            // when /v1/chat/* gains a tenant middleware. For now the
            // observer falls back to "default".
            tenant_id: None,
        };
        bus.emit_nonblocking(ev);
    }

    async fn persist_decision(&self, id: &str, decision: &ApprovalDecision) {
        if let Err(err) = self
            .store
            .decide_approval(id, decision.db_label(), time::OffsetDateTime::now_utc())
            .await
        {
            warn!(error = %err, id = %id, "approval: persist decision failed");
        }
    }

    fn build_row(
        &self,
        session_key: &str,
        plugin: &str,
        tool: &str,
        args_json: &[u8],
    ) -> PendingApproval {
        // Pretty best-effort: if args are valid UTF-8 we store them verbatim
        // so the admin UI can render them; otherwise we base64 the bytes
        // (very rare — agent-side JSON encoding gives us strings already).
        let args = match std::str::from_utf8(args_json) {
            Ok(s) => s.to_string(),
            Err(_) => {
                use base64::Engine;
                base64::engine::general_purpose::STANDARD.encode(args_json)
            }
        };
        let requested_at = time::OffsetDateTime::now_utc()
            .format(&time::format_description::well_known::Rfc3339)
            .unwrap_or_else(|_| String::from("unknown"));
        PendingApproval {
            id: Uuid::new_v4().to_string(),
            session_key: session_key.to_string(),
            plugin: plugin.to_string(),
            tool: tool.to_string(),
            args_json: args,
            requested_at,
            decided_at: None,
            decision: None,
        }
    }
}

/// Outcome of evaluating the rule list. `NoMatch` means the call is
/// allowed implicitly (default-Auto) — surfaced as a distinct variant so
/// tests and metrics can distinguish "no rule covered this" from
/// "explicitly Auto".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuleMatch {
    NoMatch,
    MatchedAuto,
    MatchedPrompt,
    MatchedDeny {
        reason: String,
    },
    /// A `Prompt` rule matched but the call's session_key is whitelisted,
    /// so we short-circuit to approved without prompting an operator.
    MatchedWhitelist,
}

/// Pick the most specific rule that applies. Specificity order:
/// 1. Rule with `plugin == plugin && tool == Some(tool)`.
/// 2. Rule with `plugin == plugin && tool is None` (plugin-wide).
/// 3. `NoMatch`.
///
/// Within each tier the **first** rule in declaration order wins — mirrors
/// the TOML authoring expectation that `[[approvals.rules]]` is a list.
fn match_rule_impl(
    rules: &[ApprovalRule],
    plugin: &str,
    tool: &str,
    session_key: &str,
) -> RuleMatch {
    // Tier 1: exact plugin+tool.
    let exact = rules
        .iter()
        .find(|r| r.plugin == plugin && r.tool.as_deref() == Some(tool));
    // Tier 2: plugin-only (tool is None).
    let plugin_wide = rules
        .iter()
        .find(|r| r.plugin == plugin && r.tool.is_none());

    let rule = match (exact, plugin_wide) {
        (Some(r), _) => r,
        (None, Some(r)) => r,
        (None, None) => return RuleMatch::NoMatch,
    };

    match rule.mode {
        ApprovalMode::Auto => RuleMatch::MatchedAuto,
        ApprovalMode::Deny => RuleMatch::MatchedDeny {
            reason: format!("deny rule matched plugin='{}'", rule.plugin),
        },
        ApprovalMode::Prompt => {
            if !session_key.is_empty() && rule.allow_session_keys.iter().any(|k| k == session_key) {
                RuleMatch::MatchedWhitelist
            } else {
                RuleMatch::MatchedPrompt
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::config::{ApprovalMode, ApprovalRule};
    use tempfile::TempDir;

    fn rule(plugin: &str, tool: Option<&str>, mode: ApprovalMode, allow: &[&str]) -> ApprovalRule {
        ApprovalRule {
            plugin: plugin.to_string(),
            tool: tool.map(str::to_string),
            mode,
            allow_session_keys: allow.iter().map(|s| s.to_string()).collect(),
        }
    }

    async fn fresh_gate(rules: Vec<ApprovalRule>, timeout: Duration) -> (ApprovalGate, TempDir) {
        let tmp = TempDir::new().unwrap();
        let store = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        corlinman_vector::migration::ensure_schema(&store)
            .await
            .unwrap();
        let gate = ApprovalGate::new(rules, Arc::new(store), timeout);
        (gate, tmp)
    }

    // ---- rule matching (pure) ----

    #[test]
    fn match_rule_exact_beats_plugin_wide() {
        let rules = vec![
            rule("file-ops", None, ApprovalMode::Auto, &[]),
            rule("file-ops", Some("write"), ApprovalMode::Deny, &[]),
        ];
        let got = match_rule_impl(&rules, "file-ops", "write", "s1");
        assert!(matches!(got, RuleMatch::MatchedDeny { .. }));

        let got = match_rule_impl(&rules, "file-ops", "read", "s1");
        assert_eq!(got, RuleMatch::MatchedAuto);
    }

    #[test]
    fn match_rule_plugin_only_applies_to_all_tools() {
        let rules = vec![rule("shell", None, ApprovalMode::Prompt, &[])];
        for t in ["exec", "spawn", "whatever"] {
            assert_eq!(
                match_rule_impl(&rules, "shell", t, "s1"),
                RuleMatch::MatchedPrompt
            );
        }
    }

    #[test]
    fn match_rule_allow_session_keys_short_circuits_prompt() {
        let rules = vec![rule(
            "shell",
            None,
            ApprovalMode::Prompt,
            &["trusted-session"],
        )];
        assert_eq!(
            match_rule_impl(&rules, "shell", "exec", "trusted-session"),
            RuleMatch::MatchedWhitelist
        );
        assert_eq!(
            match_rule_impl(&rules, "shell", "exec", "random-session"),
            RuleMatch::MatchedPrompt
        );
    }

    #[test]
    fn match_rule_no_match_when_plugin_absent() {
        let rules = vec![rule("file-ops", None, ApprovalMode::Deny, &[])];
        assert_eq!(
            match_rule_impl(&rules, "calendar", "add", "s1"),
            RuleMatch::NoMatch
        );
    }

    #[test]
    fn match_rule_allow_session_keys_ignored_for_non_prompt_modes() {
        // A misconfigured Deny+allow_session_keys should still Deny —
        // whitelist is only meaningful for Prompt rules.
        let rules = vec![rule(
            "shell",
            None,
            ApprovalMode::Deny,
            &["trusted-session"],
        )];
        let got = match_rule_impl(&rules, "shell", "exec", "trusted-session");
        assert!(matches!(got, RuleMatch::MatchedDeny { .. }));
    }

    // ---- check() paths ----

    #[tokio::test]
    async fn check_auto_returns_approved_without_persisting() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("file-ops", None, ApprovalMode::Auto, &[])],
            Duration::from_millis(200),
        )
        .await;
        let d = gate
            .check("s1", "file-ops", "read", b"{}", CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(d, ApprovalDecision::Approved);
        assert!(gate
            .store
            .list_pending_approvals(true)
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn check_deny_records_decided_row() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Deny, &[])],
            Duration::from_millis(200),
        )
        .await;
        let d = gate
            .check("s1", "shell", "exec", b"{}", CancellationToken::new())
            .await
            .unwrap();
        assert!(matches!(d, ApprovalDecision::Denied(_)));
        let rows = gate.store.list_pending_approvals(true).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].decision.as_deref(), Some("denied"));
        assert!(rows[0].decided_at.is_some());
    }

    #[tokio::test]
    async fn check_prompt_times_out_and_persists_timeout() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Prompt, &[])],
            Duration::from_millis(80),
        )
        .await;
        let d = gate
            .check("s1", "shell", "exec", b"{}", CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(d, ApprovalDecision::Timeout);
        let rows = gate.store.list_pending_approvals(true).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].decision.as_deref(), Some("timeout"));
    }

    #[tokio::test]
    async fn check_prompt_with_whitelist_approves_without_row() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Prompt, &["s1"])],
            Duration::from_millis(80),
        )
        .await;
        let d = gate
            .check("s1", "shell", "exec", b"{}", CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(d, ApprovalDecision::Approved);
        assert!(gate
            .store
            .list_pending_approvals(true)
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn resolve_wakes_pending_check() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Prompt, &[])],
            Duration::from_secs(5),
        )
        .await;
        let gate_clone = gate.clone();
        let handle = tokio::spawn(async move {
            gate_clone
                .check("s1", "shell", "exec", b"{}", CancellationToken::new())
                .await
        });

        // Wait for the row to land.
        let id = loop {
            let rows = gate.store.list_pending_approvals(false).await.unwrap();
            if let Some(r) = rows.first() {
                break r.id.clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        };

        gate.resolve(&id, ApprovalDecision::Approved).await.unwrap();
        let d = handle.await.unwrap().unwrap();
        assert_eq!(d, ApprovalDecision::Approved);

        let rows = gate.store.list_pending_approvals(true).await.unwrap();
        assert_eq!(rows[0].decision.as_deref(), Some("approved"));
    }

    #[tokio::test]
    async fn resolve_denies_with_reason_payload() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Prompt, &[])],
            Duration::from_secs(5),
        )
        .await;
        let gate_clone = gate.clone();
        let handle = tokio::spawn(async move {
            gate_clone
                .check("s1", "shell", "exec", b"{}", CancellationToken::new())
                .await
        });

        let id = loop {
            let rows = gate.store.list_pending_approvals(false).await.unwrap();
            if let Some(r) = rows.first() {
                break r.id.clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        };

        gate.resolve(&id, ApprovalDecision::Denied("not safe".into()))
            .await
            .unwrap();
        let d = handle.await.unwrap().unwrap();
        assert_eq!(d, ApprovalDecision::Denied("not safe".into()));
    }

    #[tokio::test]
    async fn resolve_is_idempotent_for_decided_rows() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Deny, &[])],
            Duration::from_millis(50),
        )
        .await;
        let _ = gate
            .check("s1", "shell", "exec", b"{}", CancellationToken::new())
            .await
            .unwrap();
        let rows = gate.store.list_pending_approvals(true).await.unwrap();
        let id = rows[0].id.clone();
        // Second resolve on already-decided row must not error.
        gate.resolve(&id, ApprovalDecision::Approved).await.unwrap();
    }

    #[tokio::test]
    async fn subscribe_receives_pending_and_decided_events() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Prompt, &[])],
            Duration::from_secs(5),
        )
        .await;
        let mut rx = gate.subscribe();

        let gate_clone = gate.clone();
        let handle = tokio::spawn(async move {
            gate_clone
                .check("s1", "shell", "exec", b"{}", CancellationToken::new())
                .await
        });

        let first = rx.recv().await.expect("first event");
        let id = match first {
            ApprovalEvent::Pending(row) => row.id,
            other => panic!("expected Pending, got {other:?}"),
        };
        gate.resolve(&id, ApprovalDecision::Approved).await.unwrap();
        let second = rx.recv().await.expect("second event");
        matches!(second, ApprovalEvent::Decided { .. });
        let _ = handle.await.unwrap();
    }

    // ---- hook bus mirror (B4-BE6) ----

    /// With a `HookBus` attached, raising + resolving a prompt must fan
    /// out `ApprovalRequested` then `ApprovalDecided` to bus subscribers
    /// while the legacy local `ApprovalEvent` broadcaster still fires.
    #[tokio::test]
    async fn bus_receives_requested_then_decided_on_prompt_resolve() {
        use corlinman_hooks::{HookBus, HookEvent, HookPriority};

        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Prompt, &[])],
            Duration::from_secs(5),
        )
        .await;
        let bus = Arc::new(HookBus::new(16));
        let gate = gate.with_bus(bus.clone());
        let mut sub = bus.subscribe(HookPriority::Normal);

        let gate_clone = gate.clone();
        let handle = tokio::spawn(async move {
            gate_clone
                .check(
                    "s1",
                    "shell",
                    "exec",
                    b"{\"cmd\":\"ls\"}",
                    CancellationToken::new(),
                )
                .await
        });

        // First bus event must be `ApprovalRequested` — invariant for
        // downstream consumers (they assume Requested precedes Decided).
        let first = sub.recv().await.expect("first bus event");
        let id = match first {
            HookEvent::ApprovalRequested {
                id,
                session_key,
                plugin,
                tool,
                args_preview,
                ..
            } => {
                assert_eq!(session_key, "s1");
                assert_eq!(plugin, "shell");
                assert_eq!(tool, "exec");
                assert!(args_preview.contains("ls"));
                id
            }
            other => panic!("expected ApprovalRequested, got {other:?}"),
        };

        gate.resolve(&id, ApprovalDecision::Approved).await.unwrap();
        let second = sub.recv().await.expect("second bus event");
        match second {
            HookEvent::ApprovalDecided {
                id: ev_id,
                decision,
                ..
            } => {
                assert_eq!(ev_id, id);
                assert_eq!(decision, "allow");
            }
            other => panic!("expected ApprovalDecided, got {other:?}"),
        }

        let d = handle.await.unwrap().unwrap();
        assert_eq!(d, ApprovalDecision::Approved);
    }

    /// Instant-deny path must still fire Requested before Decided on the
    /// bus, so subscribers never see an orphaned decision.
    #[tokio::test]
    async fn bus_requested_before_decided_on_instant_deny() {
        use corlinman_hooks::{HookBus, HookEvent, HookPriority};

        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Deny, &[])],
            Duration::from_millis(50),
        )
        .await;
        let bus = Arc::new(HookBus::new(16));
        let gate = gate.with_bus(bus.clone());
        let mut sub = bus.subscribe(HookPriority::Normal);

        let d = gate
            .check("s1", "shell", "exec", b"{}", CancellationToken::new())
            .await
            .unwrap();
        assert!(matches!(d, ApprovalDecision::Denied(_)));

        let first = sub.recv().await.expect("requested event");
        assert!(matches!(first, HookEvent::ApprovalRequested { .. }));
        let second = sub.recv().await.expect("decided event");
        match second {
            HookEvent::ApprovalDecided { decision, .. } => assert_eq!(decision, "deny"),
            other => panic!("expected ApprovalDecided, got {other:?}"),
        }
    }

    /// Without a bus, behaviour is unchanged — no panics, no extra
    /// subscriber-visible events.
    #[tokio::test]
    async fn no_bus_preserves_legacy_behaviour() {
        let (gate, _tmp) = fresh_gate(
            vec![rule("shell", None, ApprovalMode::Deny, &[])],
            Duration::from_millis(50),
        )
        .await;
        // No with_bus() call.
        let d = gate
            .check("s1", "shell", "exec", b"{}", CancellationToken::new())
            .await
            .unwrap();
        assert!(matches!(d, ApprovalDecision::Denied(_)));
    }
}
