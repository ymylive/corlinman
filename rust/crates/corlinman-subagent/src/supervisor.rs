//! Concurrency / depth caps for `subagent.spawn`.
//!
//! Iter 3 ships only the in-process slot accountant. Actual child execution
//! (PyO3 bridge → Python `run_child`) lands in iter 5; the timeout / tool-
//! allowlist / hook-event layers stack on top in iter 6+. By keeping the
//! accountant separate we can unit-test the cap policies without spinning a
//! Python interpreter or a real reasoning loop.
//!
//! Three caps, all enforced at `try_acquire` time:
//!
//! * **Per-parent concurrency** (default 3) — keyed by
//!   `parent_session_key`. One operator session can fan out at most N
//!   children at any instant; siblings must finish before the (N+1)th.
//! * **Per-tenant quota** (default 15) — keyed by `tenant_id`. Stops one
//!   noisy tenant from starving siblings under shared deployment.
//! * **Depth cap** (default 2) — `parent_ctx.depth >= max_depth` refuses
//!   the spawn outright. Prevents fork-bomb chains.
//!
//! The slot returned on success is a drop-guard: holding the `Slot`
//! reserves the counts; dropping it (success, error, panic, all the same)
//! decrements both the per-parent and per-tenant counters atomically.
//! Callers must NOT mem::forget the slot.
//!
//! Caps are checked under per-key entries; the design accepts this is not
//! a strict global linearisation — the rare race where two threads each
//! see "currently 2" and both increment to 3+1 is bounded at +N
//! concurrent caller threads (small) and self-corrects on the next
//! release. The design deliberately picked DashMap-keyed counters over a
//! single Mutex-wrapped map because spawn-paths are hot.
//!
//! Supervisor is `Send + Sync` and meant to be cloned into the gateway
//! tool dispatcher as `Arc<Supervisor>`.

use std::sync::Arc;

use corlinman_hooks::{HookBus, HookEvent};
use dashmap::DashMap;
use serde::{Deserialize, Serialize};

use crate::types::{FinishReason, ParentContext, TaskResult};

/// Policy knobs for the cap accountant. Mirrors the `[subagent]` config
/// block (design § "Resource governance"). Defaults match design §
/// "Caps" — `max_concurrent_per_parent=3`, `max_concurrent_per_tenant=15`,
/// `max_depth=2`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct SupervisorPolicy {
    pub max_concurrent_per_parent: u32,
    pub max_concurrent_per_tenant: u32,
    pub max_depth: u8,
}

impl Default for SupervisorPolicy {
    fn default() -> Self {
        Self {
            max_concurrent_per_parent: 3,
            max_concurrent_per_tenant: 15,
            max_depth: 2,
        }
    }
}

/// Reason `try_acquire` refused. Mapped to `FinishReason` at the call
/// site so we don't import this enum into the wire types module.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquireReject {
    /// `parent_ctx.depth >= policy.max_depth`.
    DepthCapped,
    /// Per-parent counter at or above `max_concurrent_per_parent`.
    ParentConcurrencyExceeded,
    /// Per-tenant counter at or above `max_concurrent_per_tenant`.
    TenantQuotaExceeded,
}

/// Cap accountant. Cloning is cheap (the per-key maps live behind `Arc`
/// inside `DashMap`, and we wrap them in `Arc` ourselves for the supervisor
/// itself). Construct once at gateway boot and share via `Arc<Supervisor>`.
///
/// Iter 9: an optional `Arc<HookBus>` lets the supervisor emit
/// `Subagent{Spawned,Completed,TimedOut,DepthCapped}` lifecycle events.
/// The bus is `Option<...>` rather than required so unit tests + the
/// pure-Rust cap-accountant tests don't need to stand up a bus, and
/// the gateway boot can defer the wiring until iter 10's E2E.
#[derive(Debug, Default)]
pub struct Supervisor {
    policy: SupervisorPolicy,
    /// Currently-in-flight count per `parent_session_key`.
    per_parent: DashMap<String, u32>,
    /// Currently-in-flight count per `tenant_id`.
    per_tenant: DashMap<String, u32>,
    /// Optional hook bus. When set, the supervisor emits one of the
    /// four `Subagent*` variants on each lifecycle transition. The
    /// emit is best-effort: bus errors are logged at `warn` and never
    /// propagated, matching the rest of the gateway's "hooks never
    /// crash the caller" stance.
    hook_bus: Option<Arc<HookBus>>,
}

impl Supervisor {
    pub fn new(policy: SupervisorPolicy) -> Arc<Self> {
        Arc::new(Self {
            policy,
            per_parent: DashMap::new(),
            per_tenant: DashMap::new(),
            hook_bus: None,
        })
    }

    /// Builder-style: install a hook bus. The supervisor lives behind
    /// an `Arc`, so we accept `Arc<Self>` as input and return a freshly
    /// `Arc`-wrapped clone with the bus filled in. This avoids the
    /// `Arc::get_mut` dance for a one-shot install at gateway boot.
    pub fn with_hook_bus(self: Arc<Self>, bus: Arc<HookBus>) -> Arc<Self> {
        // Clone the per-instance fields out; the per-key counter maps
        // are not yet populated at boot so the rebuild has zero data
        // cost. If callers ever install a bus mid-flight (not the
        // production pattern) the maps would reset — debug-asserted.
        debug_assert!(
            self.per_parent.is_empty() && self.per_tenant.is_empty(),
            "with_hook_bus must be called before any try_acquire"
        );
        Arc::new(Self {
            policy: self.policy,
            per_parent: DashMap::new(),
            per_tenant: DashMap::new(),
            hook_bus: Some(bus),
        })
    }

    pub fn policy(&self) -> SupervisorPolicy {
        self.policy
    }

    /// Borrow the installed hook bus, if any. Lets the python_bridge
    /// emit `SubagentCompleted` / `SubagentTimedOut` events from the
    /// post-runner branch where it owns the result envelope and the
    /// supervisor only knows the slot was acquired.
    pub fn hook_bus(&self) -> Option<&Arc<HookBus>> {
        self.hook_bus.as_ref()
    }

    /// Try to reserve a child slot for the given parent context.
    ///
    /// Returns `Ok(Slot)` on success — drop the slot to release. Returns
    /// `Err(AcquireReject)` if any cap is hit. The check order is:
    /// depth → per-parent concurrency → per-tenant quota. Order matters
    /// because depth is the cheapest check and the "wrong tenant"
    /// telemetry the operator wants is closer to the bottom.
    pub fn try_acquire(
        self: &Arc<Self>,
        parent_ctx: &ParentContext,
    ) -> Result<Slot, AcquireReject> {
        // Depth gate is purely on the caller's snapshot — no map writes.
        if parent_ctx.depth >= self.policy.max_depth {
            // Iter 9: emit DepthCapped so the operator UI / evolution
            // observer can see "the LLM tried but the cap held".
            self.emit_reject(parent_ctx, AcquireReject::DepthCapped);
            return Err(AcquireReject::DepthCapped);
        }

        let parent_key = parent_ctx.parent_session_key.clone();
        let tenant_key = parent_ctx.tenant_id.clone();

        // Per-parent admit-or-reject. We check + increment under the same
        // entry so two concurrent acquires for the same parent see
        // consistent counts; the only race that survives is between
        // different DashMap shards, which is bounded as documented at the
        // module level.
        {
            let mut entry = self.per_parent.entry(parent_key.clone()).or_insert(0);
            if *entry >= self.policy.max_concurrent_per_parent {
                drop(entry);
                self.emit_reject(parent_ctx, AcquireReject::ParentConcurrencyExceeded);
                return Err(AcquireReject::ParentConcurrencyExceeded);
            }
            *entry += 1;
        }

        // Per-tenant. If this fails, we must roll back the per-parent
        // increment so the slot accounting stays balanced.
        {
            let mut entry = self.per_tenant.entry(tenant_key.clone()).or_insert(0);
            if *entry >= self.policy.max_concurrent_per_tenant {
                drop(entry);
                self.dec_parent(&parent_key);
                self.emit_reject(parent_ctx, AcquireReject::TenantQuotaExceeded);
                return Err(AcquireReject::TenantQuotaExceeded);
            }
            *entry += 1;
        }

        Ok(Slot {
            supervisor: Arc::clone(self),
            parent_key,
            tenant_key,
            released: false,
        })
    }

    /// Iter 9 emit helper: best-effort `SubagentDepthCapped` event for
    /// every cap-rejected spawn. The variant carries a `reason` field
    /// discriminating depth-cap from the concurrency caps so dashboards
    /// can split the funnel.
    fn emit_reject(&self, parent_ctx: &ParentContext, reject: AcquireReject) {
        let Some(bus) = self.hook_bus.as_ref() else {
            return;
        };
        let event = HookEvent::SubagentDepthCapped {
            parent_session_key: parent_ctx.parent_session_key.clone(),
            attempted_depth: parent_ctx.depth,
            reason: match reject {
                AcquireReject::DepthCapped => "depth_capped",
                AcquireReject::ParentConcurrencyExceeded => "parent_concurrency_exceeded",
                AcquireReject::TenantQuotaExceeded => "tenant_quota_exceeded",
            }
            .into(),
            parent_trace_id: parent_ctx.trace_id.clone(),
            tenant_id: parent_ctx.tenant_id.clone(),
        };
        // Best-effort: failures land on tracing rather than propagating
        // (matches the scheduler's `emit_outcome` stance).
        bus.emit_nonblocking(event);
    }

    /// Iter 9: emit `SubagentSpawned` once the slot is acquired and the
    /// child's runtime context is known. Called from the python_bridge
    /// after `try_acquire` succeeds — at that point we know the child's
    /// session_key + agent_id (the bridge derives them from the parent
    /// context via `child_context()`).
    pub fn emit_spawned(
        &self,
        parent_ctx: &ParentContext,
        child_ctx: &ParentContext,
        agent_card: &str,
    ) {
        let Some(bus) = self.hook_bus.as_ref() else {
            return;
        };
        let event = HookEvent::SubagentSpawned {
            parent_session_key: parent_ctx.parent_session_key.clone(),
            child_session_key: child_ctx.parent_session_key.clone(),
            child_agent_id: child_ctx.parent_agent_id.clone(),
            agent_card: agent_card.into(),
            depth: child_ctx.depth,
            parent_trace_id: parent_ctx.trace_id.clone(),
            tenant_id: parent_ctx.tenant_id.clone(),
        };
        bus.emit_nonblocking(event);
    }

    /// Iter 9: emit `SubagentCompleted` / `SubagentTimedOut` based on
    /// the child's terminal `TaskResult`. Splits Timeout into its own
    /// variant so dashboards can red-flag timeouts directly without
    /// parsing the inner `finish_reason`. Pre-spawn rejections are
    /// emitted by `emit_reject`, not here.
    pub fn emit_finished(&self, parent_ctx: &ParentContext, result: &TaskResult) {
        let Some(bus) = self.hook_bus.as_ref() else {
            return;
        };
        let event = match result.finish_reason {
            FinishReason::Timeout => HookEvent::SubagentTimedOut {
                parent_session_key: parent_ctx.parent_session_key.clone(),
                child_session_key: result.child_session_key.clone(),
                child_agent_id: result.child_agent_id.clone(),
                elapsed_ms: result.elapsed_ms,
                parent_trace_id: parent_ctx.trace_id.clone(),
                tenant_id: parent_ctx.tenant_id.clone(),
            },
            FinishReason::DepthCapped | FinishReason::Rejected => {
                // Pre-spawn rejections are owned by emit_reject —
                // calling emit_finished on one of these would double-
                // emit. Drop the second emit silently.
                return;
            }
            _ => HookEvent::SubagentCompleted {
                parent_session_key: parent_ctx.parent_session_key.clone(),
                child_session_key: result.child_session_key.clone(),
                child_agent_id: result.child_agent_id.clone(),
                finish_reason: result.finish_reason.as_str().to_string(),
                elapsed_ms: result.elapsed_ms,
                tool_calls_made: result.tool_calls_made.len() as u32,
                parent_trace_id: parent_ctx.trace_id.clone(),
                tenant_id: parent_ctx.tenant_id.clone(),
            },
        };
        bus.emit_nonblocking(event);
    }

    /// Test-friendly inspector: current per-parent count.
    #[doc(hidden)]
    pub fn parent_count(&self, parent_session_key: &str) -> u32 {
        self.per_parent
            .get(parent_session_key)
            .map(|v| *v)
            .unwrap_or(0)
    }

    /// Test-friendly inspector: current per-tenant count.
    #[doc(hidden)]
    pub fn tenant_count(&self, tenant_id: &str) -> u32 {
        self.per_tenant.get(tenant_id).map(|v| *v).unwrap_or(0)
    }

    fn dec_parent(&self, parent_session_key: &str) {
        if let Some(mut e) = self.per_parent.get_mut(parent_session_key) {
            if *e > 0 {
                *e -= 1;
            }
        }
    }

    fn dec_tenant(&self, tenant_id: &str) {
        if let Some(mut e) = self.per_tenant.get_mut(tenant_id) {
            if *e > 0 {
                *e -= 1;
            }
        }
    }
}

/// Drop-guard for an acquired slot. Decrements the per-parent and
/// per-tenant counters when dropped. `released = true` after a successful
/// `release()` short-circuits the Drop impl so manual + scope-end paths
/// don't double-decrement.
#[must_use = "drop the Slot to release; mem::forget would leak the cap counters"]
#[derive(Debug)]
pub struct Slot {
    supervisor: Arc<Supervisor>,
    parent_key: String,
    tenant_key: String,
    released: bool,
}

impl Slot {
    /// Explicit release — intended for callers that want to free the
    /// slot before the natural end of their scope (e.g. on a `select!`
    /// branch that abandons the in-flight task). Idempotent.
    pub fn release(mut self) {
        self.do_release();
    }

    fn do_release(&mut self) {
        if self.released {
            return;
        }
        self.released = true;
        self.supervisor.dec_parent(&self.parent_key);
        self.supervisor.dec_tenant(&self.tenant_key);
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        self.do_release();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parent_ctx(tenant: &str, session: &str, depth: u8) -> ParentContext {
        ParentContext {
            tenant_id: tenant.into(),
            parent_agent_id: format!("agent-of-{session}"),
            parent_session_key: session.into(),
            depth,
            trace_id: format!("trace-of-{session}"),
        }
    }

    /// Maps to design test row `concurrency_cap_rejects_fourth_when_three_in_flight`.
    #[test]
    fn concurrency_cap_rejects_fourth_when_three_in_flight() {
        let sup = Supervisor::new(SupervisorPolicy::default()); // 3 per parent
        let ctx = parent_ctx("t1", "session-A", 0);

        let s1 = sup.try_acquire(&ctx).expect("first");
        let s2 = sup.try_acquire(&ctx).expect("second");
        let s3 = sup.try_acquire(&ctx).expect("third");
        assert_eq!(sup.parent_count("session-A"), 3);

        // Fourth refused with the per-parent reason.
        let err = sup.try_acquire(&ctx).expect_err("fourth");
        assert_eq!(err, AcquireReject::ParentConcurrencyExceeded);
        assert_eq!(
            sup.parent_count("session-A"),
            3,
            "rejected acquire must not increment"
        );

        // Releasing one frees the slot for another.
        drop(s1);
        assert_eq!(sup.parent_count("session-A"), 2);
        let s4 = sup.try_acquire(&ctx).expect("fourth-after-release");
        assert_eq!(sup.parent_count("session-A"), 3);

        drop(s2);
        drop(s3);
        drop(s4);
        assert_eq!(sup.parent_count("session-A"), 0);
        assert_eq!(sup.tenant_count("t1"), 0, "tenant counter must follow");
    }

    /// Maps to design test row `tenant_quota_caps_across_parents`.
    /// Same tenant, multiple parent sessions; cap is the tenant quota.
    #[test]
    fn tenant_quota_caps_across_parents() {
        let policy = SupervisorPolicy {
            max_concurrent_per_parent: 100, // disable per-parent for this test
            max_concurrent_per_tenant: 4,
            max_depth: 2,
        };
        let sup = Supervisor::new(policy);

        // Spread across 4 parent sessions, all under tenant `shared`.
        let mut held = vec![];
        for i in 0..4 {
            let ctx = parent_ctx("shared", &format!("sess-{i}"), 0);
            held.push(sup.try_acquire(&ctx).expect("under quota"));
        }
        assert_eq!(sup.tenant_count("shared"), 4);

        // 5th refused with TenantQuotaExceeded — even though it's a
        // brand-new parent session.
        let ctx_new = parent_ctx("shared", "sess-new", 0);
        let err = sup.try_acquire(&ctx_new).expect_err("over quota");
        assert_eq!(err, AcquireReject::TenantQuotaExceeded);
        assert_eq!(
            sup.tenant_count("shared"),
            4,
            "tenant counter unchanged on rejection"
        );
        assert_eq!(
            sup.parent_count("sess-new"),
            0,
            "per-parent must roll back when tenant rejects"
        );

        // A different tenant is unaffected.
        let ctx_other = parent_ctx("isolated", "sess-x", 0);
        let _other = sup.try_acquire(&ctx_other).expect("different tenant ok");
        assert_eq!(sup.tenant_count("isolated"), 1);
    }

    /// Maps to design test row `depth_cap_blocks_grandchild_at_depth_2`.
    #[test]
    fn depth_cap_blocks_grandchild_at_depth_2() {
        let sup = Supervisor::new(SupervisorPolicy::default()); // max_depth=2

        // depth 0 (top-level user turn): allowed.
        let ctx0 = parent_ctx("t", "s", 0);
        let _s0 = sup.try_acquire(&ctx0).expect("depth 0 spawns child");

        // depth 1 (child wants to spawn grandchild): allowed.
        let ctx1 = parent_ctx("t", "s::child::0", 1);
        let _s1 = sup.try_acquire(&ctx1).expect("depth 1 spawns grandchild");

        // depth 2 (grandchild wants to spawn): refused — that would be
        // a great-grandchild beyond the cap.
        let ctx2 = parent_ctx("t", "s::child::0::child::0", 2);
        let err = sup.try_acquire(&ctx2).expect_err("depth 2 refuses");
        assert_eq!(err, AcquireReject::DepthCapped);

        // No counters should have been incremented for the rejected spawn.
        assert_eq!(sup.parent_count("s::child::0::child::0"), 0);
    }

    #[test]
    fn explicit_release_is_idempotent_and_decrements_once() {
        let sup = Supervisor::new(SupervisorPolicy::default());
        let ctx = parent_ctx("t", "s", 0);

        let slot = sup.try_acquire(&ctx).unwrap();
        assert_eq!(sup.parent_count("s"), 1);
        slot.release();
        assert_eq!(sup.parent_count("s"), 0);
        // Second slot in the same key works (no double-decrement leaked).
        let _slot2 = sup.try_acquire(&ctx).unwrap();
        assert_eq!(sup.parent_count("s"), 1);
    }

    #[test]
    fn policy_defaults_match_design() {
        let p = SupervisorPolicy::default();
        assert_eq!(p.max_concurrent_per_parent, 3);
        assert_eq!(p.max_concurrent_per_tenant, 15);
        assert_eq!(p.max_depth, 2);
    }

    // -----------------------------------------------------------------
    // Iter 9: hook-bus emit tests
    //
    // Pattern mirrors the scheduler's runtime tests
    // (`corlinman-scheduler/src/runtime.rs`): subscribe a Normal-tier
    // listener, drive the action, drain the receiver, assert on event
    // shape. Critical tier isn't necessary — the supervisor uses the
    // fire-and-forget `emit_nonblocking` path which fans out to all
    // tiers.
    // -----------------------------------------------------------------

    use corlinman_hooks::{HookBus, HookEvent};
    use std::time::Duration;

    fn drain_events(sub: &mut corlinman_hooks::HookSubscription) -> Vec<HookEvent> {
        let mut out = Vec::new();
        // try_recv loop with a tight bound — the supervisor emits
        // synchronously from `try_acquire`, so by the time the test
        // resumes the events are already on the channel. We give a
        // tiny budget in case the broadcast yields the runtime.
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime.block_on(async {
            let deadline = tokio::time::Instant::now() + Duration::from_millis(100);
            loop {
                tokio::select! {
                    res = sub.recv() => {
                        match res {
                            Ok(ev) => out.push(ev),
                            Err(_) => return,
                        }
                    }
                    _ = tokio::time::sleep_until(deadline) => return,
                }
            }
        });
        out
    }

    /// Spawning success on a hook-bus-equipped supervisor emits the
    /// `SubagentSpawned` event with the full id triple (parent, child,
    /// agent_card) and the `parent_trace_id` for evolution-signal
    /// linking.
    #[test]
    fn emit_spawned_carries_parent_trace_id() {
        let bus = Arc::new(HookBus::new(64));
        let mut sub = bus.subscribe(corlinman_hooks::HookPriority::Normal);

        let sup = Supervisor::new(SupervisorPolicy::default()).with_hook_bus(bus);
        let parent = parent_ctx("tenant-a", "sess-root", 0);
        let child = parent.child_context("researcher", 0);
        sup.emit_spawned(&parent, &child, "researcher");

        let events = drain_events(&mut sub);
        match events.as_slice() {
            [HookEvent::SubagentSpawned {
                parent_session_key,
                child_session_key,
                child_agent_id,
                agent_card,
                depth,
                parent_trace_id,
                tenant_id,
            }] => {
                assert_eq!(parent_session_key, "sess-root");
                assert_eq!(child_session_key, "sess-root::child::0");
                assert_eq!(child_agent_id, "agent-of-sess-root::researcher::0");
                assert_eq!(agent_card, "researcher");
                assert_eq!(*depth, 1);
                // Children inherit parent's trace_id verbatim — locks
                // the iter 9 join-query contract.
                assert_eq!(parent_trace_id, "trace-of-sess-root");
                assert_eq!(tenant_id, "tenant-a");
            }
            other => panic!("expected one SubagentSpawned, got {other:?}"),
        }
    }

    /// `emit_finished` fires `SubagentCompleted` for every non-pre-spawn
    /// reason; `Stop` is the canonical happy path.
    #[test]
    fn emit_finished_completed_on_stop() {
        let bus = Arc::new(HookBus::new(64));
        let mut sub = bus.subscribe(corlinman_hooks::HookPriority::Normal);
        let sup = Supervisor::new(SupervisorPolicy::default()).with_hook_bus(bus);
        let parent = parent_ctx("t", "s", 0);
        let result = TaskResult {
            output_text: "ok".into(),
            tool_calls_made: vec![],
            child_session_key: "s::child::0".into(),
            child_agent_id: "agent::card::0".into(),
            elapsed_ms: 42,
            finish_reason: FinishReason::Stop,
            error: None,
        };

        sup.emit_finished(&parent, &result);
        let events = drain_events(&mut sub);
        match events.as_slice() {
            [HookEvent::SubagentCompleted {
                finish_reason,
                elapsed_ms,
                tool_calls_made,
                parent_trace_id,
                ..
            }] => {
                assert_eq!(finish_reason, "stop");
                assert_eq!(*elapsed_ms, 42);
                assert_eq!(*tool_calls_made, 0);
                assert_eq!(parent_trace_id, "trace-of-s");
            }
            other => panic!("expected SubagentCompleted, got {other:?}"),
        }
    }

    /// `Timeout` finish reason maps to `SubagentTimedOut` (its own
    /// variant), not `SubagentCompleted{finish_reason="timeout"}` —
    /// design pins this so dashboards red-flag without parsing inner
    /// fields.
    #[test]
    fn emit_finished_timed_out_on_timeout() {
        let bus = Arc::new(HookBus::new(64));
        let mut sub = bus.subscribe(corlinman_hooks::HookPriority::Normal);
        let sup = Supervisor::new(SupervisorPolicy::default()).with_hook_bus(bus);
        let parent = parent_ctx("t", "s", 0);
        let result = TaskResult {
            output_text: String::new(),
            tool_calls_made: vec![],
            child_session_key: "s::child::0".into(),
            child_agent_id: "a::c::0".into(),
            elapsed_ms: 1234,
            finish_reason: FinishReason::Timeout,
            error: None,
        };

        sup.emit_finished(&parent, &result);
        let events = drain_events(&mut sub);
        match events.as_slice() {
            [HookEvent::SubagentTimedOut {
                child_session_key,
                elapsed_ms,
                parent_trace_id,
                ..
            }] => {
                assert_eq!(child_session_key, "s::child::0");
                assert_eq!(*elapsed_ms, 1234);
                assert_eq!(parent_trace_id, "trace-of-s");
            }
            other => panic!("expected SubagentTimedOut, got {other:?}"),
        }
    }

    /// Pre-spawn rejections (DepthCapped / Rejected) are owned by
    /// `emit_reject`; calling `emit_finished` on one of those reasons
    /// must NOT double-emit. The supervisor short-circuits silently.
    #[test]
    fn emit_finished_skips_pre_spawn_reasons() {
        let bus = Arc::new(HookBus::new(64));
        let mut sub = bus.subscribe(corlinman_hooks::HookPriority::Normal);
        let sup = Supervisor::new(SupervisorPolicy::default()).with_hook_bus(bus);
        let parent = parent_ctx("t", "s", 0);

        for reason in [FinishReason::DepthCapped, FinishReason::Rejected] {
            let result = TaskResult {
                output_text: String::new(),
                tool_calls_made: vec![],
                child_session_key: "s::child::-".into(),
                child_agent_id: String::new(),
                elapsed_ms: 0,
                finish_reason: reason,
                error: Some("noop".into()),
            };
            sup.emit_finished(&parent, &result);
        }

        let events = drain_events(&mut sub);
        assert!(
            events.is_empty(),
            "pre-spawn reasons must not double-emit, got {events:?}"
        );
    }

    /// Depth-cap rejection fires `SubagentDepthCapped` with
    /// `reason="depth_capped"`; concurrency rejections fire the same
    /// variant with `reason="parent_concurrency_exceeded"` /
    /// `reason="tenant_quota_exceeded"`. The reason discriminator lets
    /// the operator UI funnel the four cap kinds.
    #[test]
    fn try_acquire_emits_depth_capped_on_cap_hit() {
        let bus = Arc::new(HookBus::new(64));
        let mut sub = bus.subscribe(corlinman_hooks::HookPriority::Normal);
        let sup = Supervisor::new(SupervisorPolicy::default()).with_hook_bus(bus);

        // depth >= max_depth (2) refused immediately.
        let ctx = parent_ctx("t", "s", 2);
        let _err = sup.try_acquire(&ctx).expect_err("depth cap");

        let events = drain_events(&mut sub);
        match events.as_slice() {
            [HookEvent::SubagentDepthCapped {
                parent_session_key,
                attempted_depth,
                reason,
                parent_trace_id,
                tenant_id,
            }] => {
                assert_eq!(parent_session_key, "s");
                assert_eq!(*attempted_depth, 2);
                assert_eq!(reason, "depth_capped");
                assert_eq!(parent_trace_id, "trace-of-s");
                assert_eq!(tenant_id, "t");
            }
            other => panic!("expected SubagentDepthCapped, got {other:?}"),
        }
    }

    /// Per-parent concurrency cap also emits `SubagentDepthCapped`
    /// with the discriminating `reason` field — same variant, different
    /// reason string. Locks the design's "all four caps emit hook
    /// events" wording.
    #[test]
    fn try_acquire_emits_depth_capped_on_concurrency_cap() {
        let bus = Arc::new(HookBus::new(64));
        let mut sub = bus.subscribe(corlinman_hooks::HookPriority::Normal);
        let sup = Supervisor::new(SupervisorPolicy::default()).with_hook_bus(bus);

        let ctx = parent_ctx("t", "s", 0);
        let _s1 = sup.try_acquire(&ctx).unwrap();
        let _s2 = sup.try_acquire(&ctx).unwrap();
        let _s3 = sup.try_acquire(&ctx).unwrap();
        // Fourth refused — concurrency cap.
        let _err = sup.try_acquire(&ctx).expect_err("concurrency");

        let events = drain_events(&mut sub);
        // Successful acquires don't emit (the runner emits
        // `SubagentSpawned` after deriving the child context); only
        // the rejection does.
        match events.last() {
            Some(HookEvent::SubagentDepthCapped { reason, .. }) => {
                assert_eq!(reason, "parent_concurrency_exceeded");
            }
            other => panic!("expected trailing SubagentDepthCapped, got {other:?}"),
        }
    }

    /// Supervisor without a hook bus is a no-op on emits — the
    /// `Option<Arc<HookBus>>` is `None` by default and every emit
    /// helper returns early. Locks the "tests don't need to stand up
    /// a bus" property.
    #[test]
    fn no_hook_bus_emits_are_silent() {
        let sup = Supervisor::new(SupervisorPolicy::default());
        let ctx = parent_ctx("t", "s", 0);
        sup.emit_spawned(&ctx, &ctx.child_context("c", 0), "c");
        sup.emit_finished(
            &ctx,
            &TaskResult {
                output_text: String::new(),
                tool_calls_made: vec![],
                child_session_key: "s::child::0".into(),
                child_agent_id: "a".into(),
                elapsed_ms: 0,
                finish_reason: FinishReason::Stop,
                error: None,
            },
        );
        // No assertion needed beyond "didn't panic"; the absence of a
        // bus is the contract.
    }
}
