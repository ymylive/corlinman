//! Public type surface for the subagent runtime.
//!
//! These types form the JSON envelope that crosses the boundary
//! between the parent's `subagent.spawn` tool call and the child's
//! reasoning loop. They deliberately mirror the Python dataclasses
//! defined in
//! `python/packages/corlinman-agent/src/corlinman_agent/subagent/api.py`
//! (lands in iter 4) so the PyO3 bridge can convert in either
//! direction without bespoke marshalling.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// Defaults pulled from the design doc's `[subagent]` config block.
/// They live here as `const` rather than in a config struct because
/// iter 1 is types-only; the iter-2 supervisor will accept a
/// `SubagentConfig` that overrides them and the const layer becomes
/// the documented fallback (matches how `corlinman-scheduler` treats
/// its job defaults).
pub mod defaults {
    /// Hard ceiling on a child's wall-clock budget. Per design,
    /// `task.max_wall_seconds` may *lower* this but never raise it;
    /// the supervisor in iter 2 enforces the upper bound.
    pub const DEFAULT_MAX_WALL_SECONDS: u32 = 60;

    /// Cap on the child's `_MAX_ROUNDS` (parent loop's own ceiling
    /// is 8 — `reasoning_loop.py:143`). Children get a slightly
    /// higher allowance because they often need to chain
    /// search → fetch → summarise.
    pub const DEFAULT_MAX_TOOL_CALLS: u16 = 12;

    /// Maximum nesting depth (parent → child → grandchild).
    /// Used by the iter-2 supervisor's `depth_capped` short-circuit.
    pub const DEFAULT_MAX_DEPTH: u8 = 2;
}

/// Parent-loop request to spawn one child.
///
/// Mirrors the Python dataclass:
///
/// ```python
/// @dataclass(slots=True, frozen=True)
/// class TaskSpec:
///     goal: str
///     tool_allowlist: list[str] | None = None
///     max_wall_seconds: int = 60
///     max_tool_calls: int = 12
///     extra_context: dict[str, str] = field(default_factory=dict)
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskSpec {
    /// User-turn prompt the child sees as its only message.
    pub goal: String,

    /// `None` → inherit parent's tool set.
    /// `Some(vec![])` → pure LLM call, no tools.
    /// `Some(non-empty)` → must be a subset of parent's tools or
    /// the iter-7 escalation check rejects it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_allowlist: Option<Vec<String>>,

    /// Hard timeout. Capped from above by
    /// `[subagent].max_wall_seconds_ceiling` in iter-2 supervisor.
    #[serde(default = "default_max_wall_seconds")]
    pub max_wall_seconds: u32,

    /// Per-child round cap.
    #[serde(default = "default_max_tool_calls")]
    pub max_tool_calls: u16,

    /// `{ctx.<key>}` blobs spliced into the child's system prompt.
    /// `BTreeMap` so the JSON serialisation is order-stable, which
    /// keeps trace fingerprints reproducible for the evolution loop.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub extra_context: BTreeMap<String, String>,
}

fn default_max_wall_seconds() -> u32 {
    defaults::DEFAULT_MAX_WALL_SECONDS
}

fn default_max_tool_calls() -> u16 {
    defaults::DEFAULT_MAX_TOOL_CALLS
}

impl TaskSpec {
    /// Minimum-information constructor; everything else takes the
    /// `defaults::*` values. Mirrors the Python dataclass call site
    /// `TaskSpec(goal=...)`.
    pub fn new(goal: impl Into<String>) -> Self {
        Self {
            goal: goal.into(),
            tool_allowlist: None,
            max_wall_seconds: defaults::DEFAULT_MAX_WALL_SECONDS,
            max_tool_calls: defaults::DEFAULT_MAX_TOOL_CALLS,
            extra_context: BTreeMap::new(),
        }
    }
}

/// Why the child stopped. Mirrors the design's `finish_reason`
/// enumeration; serialises as the lowercase string the parent's
/// LLM expects in the `ToolResult` JSON envelope.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FinishReason {
    /// Normal termination — provider returned a final response.
    Stop,
    /// Hit `max_tool_calls` without producing a final.
    Length,
    /// Wall-clock budget exhausted; partial output preserved.
    Timeout,
    /// Runner raised; see `TaskResult.error`.
    Error,
    /// Parent depth >= `max_depth`; child loop never invoked.
    DepthCapped,
    /// Concurrency / tenant quota / allowlist escalation rejected
    /// the spawn before any work happened.
    Rejected,
}

impl FinishReason {
    /// Iter-2 supervisor uses this when short-circuiting a spawn:
    /// no child loop was driven, so no output / tool calls / session.
    pub fn is_pre_spawn_rejection(self) -> bool {
        matches!(self, FinishReason::DepthCapped | FinishReason::Rejected)
    }
}

/// One entry of `TaskResult.tool_calls_made`. Carries enough for
/// the parent to attribute behaviour without re-pulling the (often
/// huge) raw arguments. Matches the design's
/// `tool_calls_made: list[dict[str, Any]]` shape.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolCallSummary {
    pub name: String,
    /// Short freeform synopsis of args (e.g. `"query=transformers"`).
    /// Not the raw JSON — that lives in the child session for replay.
    pub args_summary: String,
    pub duration_ms: u64,
}

/// Result envelope the parent loop receives as its `ToolResult.content`
/// (JSON-serialised). One child run = one of these.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskResult {
    /// Concatenated assistant stream — always a string. Schema
    /// validation falls on the parent's prompt ("ask child for JSON;
    /// you parse"), per design § Result merging.
    pub output_text: String,

    /// Attribution trail. `Vec` rather than `Option` so an empty list
    /// still serialises as `[]` and the parent's prompt can rely on
    /// the field being present.
    pub tool_calls_made: Vec<ToolCallSummary>,

    /// Forensic replay handle. Format: `<parent_session>::child::<seq>`.
    pub child_session_key: String,

    /// Persona row identity. Format: `<parent_agent>::<card>::<seq>`.
    pub child_agent_id: String,

    pub elapsed_ms: u64,
    pub finish_reason: FinishReason,

    /// Populated iff `finish_reason == Error`. `None` is the
    /// happy-path default.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl TaskResult {
    /// Constructor for the iter-2 supervisor's pre-spawn rejection
    /// path: depth cap or concurrency / allowlist refusal. The child
    /// loop never ran, so output / tool calls are empty and the
    /// session / agent ids are placeholders the parent can ignore.
    pub fn rejected(
        reason: FinishReason,
        parent_session_key: &str,
        error: impl Into<String>,
    ) -> Self {
        debug_assert!(
            reason.is_pre_spawn_rejection(),
            "TaskResult::rejected() is for DepthCapped/Rejected only; got {reason:?}"
        );
        Self {
            output_text: String::new(),
            tool_calls_made: Vec::new(),
            // Convention: `::child::-` marks a slot that was refused
            // rather than allocated. Iter-2 uses this when emitting
            // the `Rejected` hook event so operators can tell a
            // never-spawned child from a spawned-then-failed one.
            child_session_key: format!("{parent_session_key}::child::-"),
            child_agent_id: String::new(),
            elapsed_ms: 0,
            finish_reason: reason,
            error: Some(error.into()),
        }
    }
}

/// Per-spawn snapshot of the parent's identity passed across the
/// PyO3 boundary. The iter-2 supervisor reads `depth` for the
/// recursion cap and `tenant_id` for the per-tenant quota; iter-9
/// observability reads `trace_id` for evolution-signal linking.
///
/// Memory-host handles are *not* in this struct yet — the read-only
/// host wrapper (design § iter 2 of the design's own plan, mapped
/// to a different iter in this branch's plan) lives in
/// `corlinman-memory-host` and the type only enters here once the
/// PyO3 surface needs to round-trip it. Iter 1 keeps this clean.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParentContext {
    pub tenant_id: String,
    pub parent_agent_id: String,
    pub parent_session_key: String,
    /// 0 for top-level user-driven turns. `+1` per spawn frame.
    /// `>= max_depth` triggers the iter-2 short-circuit.
    #[serde(default)]
    pub depth: u8,
    /// Stable id used to fold child evolution signals into the same
    /// trace tree as the parent (iter 9). `String` rather than `Uuid`
    /// because gateway-side trace ids are already string-typed and
    /// re-parsing would just be ceremony.
    pub trace_id: String,
}

impl ParentContext {
    /// Derive the child's `ParentContext` for one nested spawn. Used
    /// by the iter-2 supervisor *after* the depth check passes.
    /// `child_seq` increments per child within one parent frame so
    /// agent_id / session_key collisions cannot happen.
    pub fn child_context(&self, child_card: &str, child_seq: u32) -> Self {
        Self {
            tenant_id: self.tenant_id.clone(),
            parent_agent_id: format!("{}::{}::{}", self.parent_agent_id, child_card, child_seq),
            parent_session_key: format!("{}::child::{}", self.parent_session_key, child_seq),
            depth: self.depth.saturating_add(1),
            // Children inherit the parent's trace_id verbatim so the
            // evolution observer's join query (iter 9) finds them
            // by `parent_trace_id == self.trace_id`.
            trace_id: self.trace_id.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Maps to design test row `task_spec_serialises_round_trip`.
    /// JSON is the wire format the PyO3 bridge will use; if the
    /// defaults don't survive a round trip we'd silently drop fields
    /// in the parent's tool envelope.
    #[test]
    fn task_spec_round_trip_with_defaults() {
        let spec = TaskSpec::new("research transformers");
        let json = serde_json::to_string(&spec).expect("serialise");
        // `tool_allowlist` is None and `extra_context` is empty;
        // both are `skip_serializing_if`-elided. `max_*` defaults
        // *are* serialised (no skip predicate) so the Python side
        // sees explicit values.
        assert!(!json.contains("tool_allowlist"));
        assert!(!json.contains("extra_context"));
        assert!(json.contains("\"max_wall_seconds\":60"));
        assert!(json.contains("\"max_tool_calls\":12"));

        let back: TaskSpec = serde_json::from_str(&json).expect("deserialise");
        assert_eq!(back, spec);
    }

    /// Defaults populate when JSON omits optional fields entirely
    /// (the parent's LLM may emit a minimal `{"goal": "..."}`).
    #[test]
    fn task_spec_defaults_populate_from_minimal_json() {
        let json = r#"{"goal": "summarise this"}"#;
        let spec: TaskSpec = serde_json::from_str(json).expect("parse");
        assert_eq!(spec.goal, "summarise this");
        assert_eq!(spec.tool_allowlist, None);
        assert_eq!(spec.max_wall_seconds, defaults::DEFAULT_MAX_WALL_SECONDS);
        assert_eq!(spec.max_tool_calls, defaults::DEFAULT_MAX_TOOL_CALLS);
        assert!(spec.extra_context.is_empty());
    }

    /// `tool_allowlist: Some(vec![])` (the "no tools" mode) must
    /// survive a round trip *distinctly* from `None` ("inherit").
    /// Conflating the two would silently widen child permissions.
    #[test]
    fn task_spec_empty_allowlist_distinct_from_none() {
        let inherit = TaskSpec::new("a");
        let mut empty = TaskSpec::new("b");
        empty.tool_allowlist = Some(Vec::new());

        let inherit_json = serde_json::to_string(&inherit).unwrap();
        let empty_json = serde_json::to_string(&empty).unwrap();
        assert_ne!(inherit_json, empty_json);

        let empty_back: TaskSpec = serde_json::from_str(&empty_json).unwrap();
        assert_eq!(empty_back.tool_allowlist, Some(Vec::<String>::new()));
    }

    /// Maps to design test row asserting result envelopes survive
    /// the JSON tool-result round-trip. The parent's LLM consumes
    /// this exact string.
    #[test]
    fn task_result_round_trip_happy_path() {
        let result = TaskResult {
            output_text: "transformers are…".into(),
            tool_calls_made: vec![ToolCallSummary {
                name: "web_search".into(),
                args_summary: "query=transformers".into(),
                duration_ms: 1240,
            }],
            child_session_key: "sess_abc::child::0".into(),
            child_agent_id: "main::researcher::0".into(),
            elapsed_ms: 4180,
            finish_reason: FinishReason::Stop,
            error: None,
        };
        let json = serde_json::to_string(&result).expect("serialise");
        let back: TaskResult = serde_json::from_str(&json).expect("deserialise");
        assert_eq!(back, result);
        // `error` must be elided on the happy path — keeps the
        // parent's prompt token-spend low when nothing went wrong.
        assert!(!json.contains("\"error\""));
        assert!(json.contains("\"finish_reason\":\"stop\""));
    }

    /// `FinishReason` is the discriminator the parent's LLM
    /// branches on. Lowercase snake_case is what `serde(rename_all)`
    /// promises — pin it with a test so a casual rename can't
    /// silently change the wire contract.
    #[test]
    fn finish_reason_serialises_as_snake_case() {
        let cases = [
            (FinishReason::Stop, "\"stop\""),
            (FinishReason::Length, "\"length\""),
            (FinishReason::Timeout, "\"timeout\""),
            (FinishReason::Error, "\"error\""),
            (FinishReason::DepthCapped, "\"depth_capped\""),
            (FinishReason::Rejected, "\"rejected\""),
        ];
        for (variant, expected) in cases {
            assert_eq!(
                serde_json::to_string(&variant).unwrap(),
                expected,
                "{variant:?} should serialise as {expected}"
            );
        }
    }

    /// The supervisor (iter 2) checks `is_pre_spawn_rejection`
    /// before deciding whether to allocate a session_key /
    /// agent_id. Lock the classification so a future variant can't
    /// accidentally land in the wrong bucket.
    #[test]
    fn pre_spawn_rejections_are_only_depth_and_rejected() {
        assert!(FinishReason::DepthCapped.is_pre_spawn_rejection());
        assert!(FinishReason::Rejected.is_pre_spawn_rejection());
        assert!(!FinishReason::Stop.is_pre_spawn_rejection());
        assert!(!FinishReason::Length.is_pre_spawn_rejection());
        assert!(!FinishReason::Timeout.is_pre_spawn_rejection());
        assert!(!FinishReason::Error.is_pre_spawn_rejection());
    }

    /// `TaskResult::rejected` is the helper iter-2 reaches for when
    /// it short-circuits. Verify it produces a well-formed envelope
    /// the parent's prompt can consume without special-casing.
    #[test]
    fn rejected_task_result_is_well_formed() {
        let result = TaskResult::rejected(
            FinishReason::DepthCapped,
            "sess_xyz",
            "depth>=2 cap reached",
        );
        assert_eq!(result.finish_reason, FinishReason::DepthCapped);
        assert_eq!(result.child_session_key, "sess_xyz::child::-");
        assert_eq!(result.elapsed_ms, 0);
        assert!(result.tool_calls_made.is_empty());
        assert!(result.output_text.is_empty());
        assert_eq!(result.error.as_deref(), Some("depth>=2 cap reached"));
    }

    #[test]
    #[should_panic(expected = "DepthCapped/Rejected only")]
    fn rejected_panics_on_non_pre_spawn_reason() {
        // Building a `Rejected` envelope from a `Stop` reason would
        // claim "no work happened" while the child actually ran.
        // Catch that misuse in debug builds.
        let _ = TaskResult::rejected(FinishReason::Stop, "sess", "wrong kind");
    }

    /// Child context derivation is the one place where parent ids
    /// are mangled into child ids. Both must follow the documented
    /// formats — these strings end up in operator UI and forensic
    /// replay queries, so the shape is part of the public contract.
    #[test]
    fn child_context_derives_ids_and_increments_depth() {
        let parent = ParentContext {
            tenant_id: "tenant-a".into(),
            parent_agent_id: "main".into(),
            parent_session_key: "sess_abc".into(),
            depth: 0,
            trace_id: "trace-xyz".into(),
        };
        let child = parent.child_context("researcher", 0);

        assert_eq!(child.tenant_id, "tenant-a");
        assert_eq!(child.parent_agent_id, "main::researcher::0");
        assert_eq!(child.parent_session_key, "sess_abc::child::0");
        assert_eq!(child.depth, 1);
        // trace_id inherits — required for the iter-9 join query.
        assert_eq!(child.trace_id, parent.trace_id);
    }

    /// Two siblings under the same parent must produce distinct ids.
    /// The `parallel_siblings_complete_independently` test in iter 4
    /// depends on this; lock the sequence-encoding here.
    #[test]
    fn child_context_seqs_disambiguate_siblings() {
        let parent = ParentContext {
            tenant_id: "t".into(),
            parent_agent_id: "p".into(),
            parent_session_key: "s".into(),
            depth: 0,
            trace_id: "trace".into(),
        };
        let a = parent.child_context("card", 0);
        let b = parent.child_context("card", 1);
        assert_ne!(a.parent_agent_id, b.parent_agent_id);
        assert_ne!(a.parent_session_key, b.parent_session_key);
    }

    /// Depth must saturate rather than wrap, so even a pathological
    /// caller passing `depth = u8::MAX` can't underflow back into
    /// the allowed range.
    #[test]
    fn child_context_depth_saturates_at_u8_max() {
        let parent = ParentContext {
            tenant_id: "t".into(),
            parent_agent_id: "p".into(),
            parent_session_key: "s".into(),
            depth: u8::MAX,
            trace_id: "trace".into(),
        };
        let child = parent.child_context("c", 0);
        assert_eq!(child.depth, u8::MAX);
    }
}
