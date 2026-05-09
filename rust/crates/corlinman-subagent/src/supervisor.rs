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

use dashmap::DashMap;
use serde::{Deserialize, Serialize};

use crate::types::ParentContext;

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
#[derive(Debug, Default)]
pub struct Supervisor {
    policy: SupervisorPolicy,
    /// Currently-in-flight count per `parent_session_key`.
    per_parent: DashMap<String, u32>,
    /// Currently-in-flight count per `tenant_id`.
    per_tenant: DashMap<String, u32>,
}

impl Supervisor {
    pub fn new(policy: SupervisorPolicy) -> Arc<Self> {
        Arc::new(Self {
            policy,
            per_parent: DashMap::new(),
            per_tenant: DashMap::new(),
        })
    }

    pub fn policy(&self) -> SupervisorPolicy {
        self.policy
    }

    /// Try to reserve a child slot for the given parent context.
    ///
    /// Returns `Ok(Slot)` on success — drop the slot to release. Returns
    /// `Err(AcquireReject)` if any cap is hit. The check order is:
    /// depth → per-parent concurrency → per-tenant quota. Order matters
    /// because depth is the cheapest check and the "wrong tenant"
    /// telemetry the operator wants is closer to the bottom.
    pub fn try_acquire(self: &Arc<Self>, parent_ctx: &ParentContext) -> Result<Slot, AcquireReject> {
        // Depth gate is purely on the caller's snapshot — no map writes.
        if parent_ctx.depth >= self.policy.max_depth {
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
        assert_eq!(sup.parent_count("session-A"), 3, "rejected acquire must not increment");

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
}
