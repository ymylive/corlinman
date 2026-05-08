# Phase 4 W4 D3 — Subagent delegation runtime

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 7-10d · **Roadmap row**: `phase4-roadmap.md:302` (4-4C)

> The agent loop gains a tool the *agent itself* can call:
> `spawn_child(agent_card, task) -> Future<TaskResult>`. Children
> inherit the parent's `memory_host` federation read-only and tenant
> scope; they get a fresh persona row, fresh session, and a
> time-bounded budget. Results merge back as one tool-result message
> in the parent's loop. Highest-LOC Wave 4 task because lifecycle,
> isolation, and cap surfaces compose for the first time.

Design seed for the iterations that follow. Pins the public API,
the inherit-vs-fresh contract, the Rust-supervisor / Python-runner
split, the result-merge envelope, the cap stack, and the open
questions around evolution-signal roll-up and operator UI tree
visualisation. Mirrors `phase4-w2-b1-design.md` in shape.

## Why this exists

Today the agent loop is a single-turn run-to-completion engine
(`python/packages/corlinman-agent/src/corlinman_agent/reasoning_loop.py:146-273`):
one `ChatStart` in, one streamed sequence of `TokenEvent` /
`ToolCallEvent` / `DoneEvent` out, capped at 8 rounds
(`reasoning_loop.py:143`). Tool execution happens **outside** —
loop emits a `ToolCallEvent`, the gateway round-trips a `ToolResult`
via `feed_tool_result` (`reasoning_loop.py:166-173`), loop appends a
`role="tool"` message and re-prompts the provider.

That model breaks on three shapes the Wave 4 acceptance calls out
(`phase4-roadmap.md:307-309`):

1. **Research-and-summarize fan-out** — "research X, summarize,
   draft 3 angles" is 3 independent searches + 3 summaries + 1
   synthesis. Serial inside one loop: 7 rounds against the 8-cap,
   one shared context window, re-loaded retrieval per turn. Parallel
   via subagents: 3 children execute concurrently with private
   contexts, parent receives 3 `TaskResult` envelopes, synthesises.
   Wall-clock cut roughly in half (`phase4-roadmap.md:429`).
2. **Parallel scrape / multi-source query** — same shape; each
   child holds its own scratch context.
3. **Fan-out evaluation / multi-angle critique** — "score draft on
   {accuracy, tone, brevity}". Three different agent cards, three
   clean persona starts (no cross-bleed of mood/fatigue).

The bigger argument is **context isolation**: by round 5 the
parent's prompt is dominated by intermediate scrape dumps the final
answer doesn't need. A subagent runs in its own message list and
returns *only* the distilled output — parent context stays clean.
Same argument as shell pipelines vs one mega-script.

## API shape

The new tool the parent loop can call:

```python
# python/packages/corlinman-agent/src/corlinman_agent/subagent/api.py (new)

@dataclass(slots=True, frozen=True)
class TaskSpec:
    goal: str                                       # user-turn prompt the child sees
    tool_allowlist: list[str] | None = None         # None → inherit parent's
    max_wall_seconds: int = 60                      # hard timeout
    max_tool_calls: int = 12                        # cap on child's _MAX_ROUNDS
    extra_context: dict[str, str] = field(default_factory=dict)
                                                    # {{ctx.<key>}} blobs

@dataclass(slots=True, frozen=True)
class TaskResult:
    output_text: str                                # concatenated assistant stream
    tool_calls_made: list[dict[str, Any]]           # name + args summaries
    child_session_key: str                          # forensic replay
    child_agent_id: str                             # spawned persona's agent_id
    elapsed_ms: int
    finish_reason: str                              # stop|length|timeout|error|depth_capped|rejected
    error: str | None = None

async def spawn_child(
    parent: ParentContext,         # carries tenant, memory_host, depth, trace_id
    agent_card: AgentCard,
    task: TaskSpec,
) -> TaskResult: ...
```

Public surface: **one async function**. The reasoning loop never
calls `spawn_child` directly — the function is wrapped as a regular
tool named `subagent.spawn`, registered alongside the agent's other
tools so the LLM emits a `ToolCallEvent` with `args_json = {"agent":
"researcher", "goal": "...", ...}` and the gateway-side executor
dispatches into `spawn_child`. Treating it as a tool means
**zero changes to the reasoning loop's event model** — the existing
`ToolResult.content` is the JSON-serialised `TaskResult`. (See
"Tool exposure" below for how the parent's tool list is mutated.)

## What children inherit / get fresh / are bounded by

| Property | Treatment | Site |
|---|---|---|
| `tenant_id` | **inherit** | child runs against same per-tenant DBs (`corlinman-tenant/src/path.rs:41`); cross-tenant spawn rejected at API |
| `model_alias` | **inherit**, override via `task.extra_context["model"]` | resolved via servicer's resolver (`agent_servicer.py:127-129`) |
| `memory_host` | **inherit read-only** | parent's `FederatedMemoryHost` (`corlinman-memory-host/src/federation.rs:50-78`) wrapped in a `ReadOnlyHost` decorator; `upsert`/`delete` return `Err` |
| `persona` row | **fresh** | new `agent_id = "<parent>::<child_card>::<seq>"`; `seed_from_card` (`corlinman-persona/src/corlinman_persona/seeder.py:116-146`) creates a row under same `tenant_id`. Composite-PK migration `f2cc7a9` makes per-tenant `agent_id` collision-free; spawn_seq makes parent-scoped `agent_id` unique |
| `session` | **fresh** | `session_key = f"{parent_session}::child::{seq}"` in `sessions.sqlite` (`corlinman-core/src/session_sqlite.rs:45-55`); busy-timeout fix `f4aae2a` (`session_sqlite.rs:112`) makes parallel siblings safe |
| chat history | **NOT inherited** | child sees system prompt + one user turn (`task.goal`); parent's history stays in parent's session (Open question 1) |
| tool-approval state | **fresh** | child's `ApprovalGate` is new; previously-approved tools re-prompt |
| evolution-signal trail | **fresh `trace_id`, link to parent** | signals carry `parent_trace_id`; query-time roll-up only (Open question 4) |
| `max_wall_seconds` | **bounded** | from `[subagent].default_timeout_seconds`; `task.max_wall_seconds` may lower but not raise |
| `max_tool_calls` | **bounded** | child's `_MAX_ROUNDS = task.max_tool_calls` (capped at the global default `reasoning_loop.py:143`); exhaustion → `finish_reason="length"` |
| `max_subagent_depth` | **bounded recursively** | `ParentContext.depth` increments per spawn; `depth >= [subagent].max_depth` (default 2) returns `depth_capped` *without* invoking the child loop |

The "fresh persona" choice matches the roadmap's leaning at
`phase4-roadmap.md:415` ("Subagent persona inheritance — Lean: fresh
from agent-card; child gets a clean mood/fatigue start; parent's
persona unaffected by children").

## Implementation surface — Rust supervisor + Python runner

Lifecycle / governance / isolation primitives live in **a new Rust
crate**; the agent-loop integration lives in a **new Python module**.

**Rust** (`rust/crates/corlinman-subagent/` — new):
- `SubagentSupervisor` — depth cap, per-parent concurrency, per-tenant
  quota, time-budget via cooperative `tokio::time::timeout`.
- `ParentContext` — record of parent's tenant, memory-host handle,
  current depth, `trace_id`. Cloned per child invocation.
- `ChildHandle` — wraps `JoinHandle<TaskResult>` + timeout +
  cancellation; returned to the executor as `Future<TaskResult>`.
- Hook-bus emits `SubagentSpawned/Completed/TimedOut/DepthCapped` so
  the observer (`corlinman-gateway/src/evolution_observer.rs`) folds
  outcomes into `evolution_signals`. Matches the scheduler-job
  pattern (`corlinman-scheduler/src/lib.rs:14-19`).

**Python** (`python/packages/corlinman-agent/src/corlinman_agent/subagent/` — new):
- `api.py` — `TaskSpec` / `TaskResult` / `ParentContext` dataclasses.
- `runner.py` — `run_child(parent_ctx, agent_card, task) -> TaskResult`:
  builds the child's `ChatStart` (fresh messages, fresh `session_key`,
  inherited model + filtered tools), drives a fresh `ReasoningLoop`,
  fills `TaskResult`. Most of the Python complexity lives here.
- `tool_wrapper.py` — registers `subagent.spawn` as a tool the
  parent's loop sees; gateway dispatches to the Rust supervisor,
  which re-enters Python over PyO3.

Split rationale: the **isolation contract** (depth cap, slot
counters) is enforceable from Rust where the LLM cannot reach it;
the actual loop driver must call `ReasoningLoop`, which lives in
Python. Pure-Python puts caps in prompt-injectable process memory;
pure-Rust duplicates the servicer's provider/persona wiring. PyO3
shape: `Supervisor.spawn(parent_ctx, card_name, task) ->
awaitable[TaskResult]`; supervisor calls back into Python via
`Python::with_gil` once the budget checks pass.

## Lifecycle

```
parent emits ToolCallEvent("subagent.spawn", {...})
        │
        ▼  schedule: supervisor budget check
        │    └─ reject → TaskResult{finish_reason="depth_capped" | "rejected"}
        ▼  spawn: seed persona row, fresh session_key, build ChatStart
        ▼  execute: ReasoningLoop.run wrapped in tokio::time::timeout
        │    └─ on expiry → cancel + 2s grace + drop → finish_reason="timeout"
        ▼  return: JSON-serialise TaskResult; emit hook; release slot
        ▼  parent receives ToolResult(content = TaskResult JSON);
           parent loop appends role="tool" message; next provider round
```

**Crash handling**: exceptions in `ReasoningLoop.run` are caught
inside `run_child`; child session left intact for forensics;
`finish_reason="error"`, `error` field carries the message; parent's
`ToolResult.is_error=True`. Supervisor's slot decrements in a
`finally` so a panicking child can't leak budget.

**Timeout handling**:
`tokio::time::timeout(Duration::from_secs(max_wall_seconds), ...)`
wraps the runner. On expiry, supervisor calls
`ReasoningLoop.cancel("subagent_timeout")` (existing cancel path at
`reasoning_loop.py:174-184`), waits 2s, drops the future.
`finish_reason="timeout"`; partial `output_text` preserved if any
tokens streamed.

**Depth-cap rejection**: returned synchronously without spawning —
parent immediately gets `TaskResult{finish_reason="depth_capped"}`.
We deliberately don't silently fail; the LLM must observe the
failure so the evolution loop can learn from repeated occurrences.

## Result merging — tool-call envelope wins

| Option | Pros | Cons |
|---|---|---|
| **Tool-call envelope** (chosen) | reuses the existing `ToolResult` round-trip path verbatim; child's output appears as one tool message; LLM is already trained to consume tool results | LLM has to extract from JSON — minor token cost |
| Context append (parent's message list grows by N child outputs) | semantically clean | bypasses the loop's tool-call discipline; provider-specific (some reject role=`assistant` with no preceding `user`) |

**Decision**: tool-call envelope, JSON-serialised:

```json
{
  "output_text": "<child's final assistant output>",
  "tool_calls_made": [
    {"name": "web_search", "args_summary": "query=X", "duration_ms": 1240}
  ],
  "child_session_key": "sess_abc::child::0",
  "child_agent_id": "main::researcher::0",
  "elapsed_ms": 4180,
  "finish_reason": "stop"
}
```

`tool_calls_made` carries name + summary (not raw args — those can
be huge) so the parent has attribution without re-pulling payloads.
`output_text` is **always a string** — schema-validation falls on
the parent's prompt ("ask child for JSON; you parse").

## Resource governance

Four caps stack:

1. **Per-parent concurrency** — `max_concurrent_per_parent` (default
   **3**). `DashMap<TraceId, AtomicUsize>` of in-flight child counts;
   exceed → `finish_reason="rejected"` immediately. Why 3: matches
   the canonical research-fan-out demo. Per-session ceiling 5
   (`phase4-roadmap.md:402`).
2. **Per-tenant quota** — `max_concurrent_per_tenant` (default
   **15**). Same map keyed by `TenantId`; stops one tenant from
   starving siblings.
3. **Nested-depth cap** — `max_depth` (default **2**).
   `ParentContext.depth: u8`; spawn at `depth >= max_depth` refuses.
   Why 2: parent → child → grandchild covers research fan-out;
   deeper is usually a fork bomb. Worst case at defaults:
   `1 * 3 * 3 = 9` total in-flight.
4. **Time budget** — `task.max_wall_seconds`, capped at
   `max_wall_seconds_ceiling` (default **300**). Cooperative cancel.

All four caps emit hook events on rejection so the operator UI can
explain "why didn't this fan out".

## Memory-host federation contract

"Children inherit memory_host" (roadmap row 4-4C wording) needs a
concrete contract. The choice:

| Option | Behaviour | Verdict |
|---|---|---|
| Share the parent's `Arc<dyn MemoryHost>` directly | child can `query` and `upsert` and `delete` | **rejected** — children are time-bounded best-effort scouts; letting them mutate memory means a buggy child contaminates parent's memory long after it's gone |
| Pass a fresh `LocalSqliteHost` keyed by child's session | total isolation | **rejected** — defeats the point; researcher child needs to query the shared knowledge base, otherwise it's reading from an empty store |
| **Wrap the parent's host in a read-only decorator** | child can `query` against the same federation; `upsert`/`delete` return `Err` | **chosen** |

Implementation: new `ReadOnlyMemoryHost` adapter in
`corlinman-memory-host`:

```rust
pub struct ReadOnlyMemoryHost {
    inner: Arc<dyn MemoryHost>,
}

#[async_trait]
impl MemoryHost for ReadOnlyMemoryHost {
    fn name(&self) -> &str { self.inner.name() }
    async fn query(&self, req: MemoryQuery) -> Result<Vec<MemoryHit>> {
        self.inner.query(req).await
    }
    async fn upsert(&self, _: MemoryDoc) -> Result<String> {
        Err(anyhow!("subagent memory host is read-only"))
    }
    async fn delete(&self, _: &str) -> Result<()> {
        Err(anyhow!("subagent memory host is read-only"))
    }
}
```

Lives next to `FederatedMemoryHost` (`corlinman-memory-host/src/federation.rs:1-79`)
in a new `read_only.rs` module. Children writing to memory becomes a
proposal-via-evolution-loop story for a future iteration (the parent
synthesises and *the parent* upserts, not the child).

## Tool exposure

Three policies, chosen at spawn time:

1. **Inherit parent's allowlist** (default when
   `task.tool_allowlist is None`) — child sees the same set; except
   `subagent.spawn` is auto-pruned at `depth = max_depth - 1` to
   prevent recursion cap bypass.
2. **Custom subset** (when `task.tool_allowlist is not None`) —
   must be `⊆ parent.tools_allowed`; superset → `finish_reason="rejected"`
   with `error="tool_allowlist_escalation"`. **No privilege
   escalation via delegation.**
3. **Empty list** — pure LLM call, no tools. Useful for
   "summarise this text" where tools would just waste rounds.

Filtering lives in `subagent/runner.py::_filter_tools_for_child`.
At `depth < max_depth - 1` the child *can* spawn grandchildren,
subject to all four caps.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `task_spec_serialises_round_trip` | types | `TaskSpec`/`TaskResult` JSON-roundtrip; defaults populate |
| `spawn_child_happy_path_returns_output` | runner | mock provider returns text; `output_text` matches; `finish_reason="stop"`; child session has 2 messages |
| `child_session_key_distinct_from_parent` | runner | `child_session_key != parent_session_key`; `::child::N` shape |
| `child_persona_row_freshly_created` | runner + persona | new `agent_persona_state` row for `child_agent_id`; defaults applied; parent row unchanged |
| `child_persona_row_under_same_tenant` | runner + persona | child row's `tenant_id` matches parent's; composite-PK constraint satisfied |
| `inherited_memory_host_is_readonly` | memory-host | child's `upsert`/`delete` return `Err`; `query` returns same hits as parent |
| `child_timeout_returns_partial_output` | supervisor | provider sleeps 5s, `max_wall_seconds=1`; `finish_reason="timeout"`, partial output preserved |
| `child_timeout_decrements_concurrency` | supervisor | post-timeout, in-flight count returns to baseline |
| `depth_cap_blocks_grandchild_at_depth_2` | supervisor | spawn at `depth>=max_depth` returns `depth_capped`; child loop not invoked; hook fires |
| `parallel_siblings_complete_independently` | supervisor + sessions | 3 concurrent spawns succeed; 3 distinct session_keys; no SQLite busy errors |
| `concurrency_cap_rejects_fourth_when_three_in_flight` | supervisor | 4th spawn returns `finish_reason="rejected"` immediately |
| `tenant_quota_caps_across_parents` | supervisor | two parents collectively cannot exceed per-tenant ceiling |
| `tool_allowlist_escalation_rejected` | runner | child asks for tool parent doesn't have → `error="tool_allowlist_escalation"` |
| `subagent_spawn_pruned_at_depth_n_minus_1` | runner | child at `depth=max_depth-1` does not see `subagent.spawn` in its tools |
| `child_error_propagates_via_tool_result_envelope` | runner + servicer | child raises; parent's `ToolResult.is_error=True`; loop continues |
| `cross_tenant_spawn_rejected` | supervisor | spawn attempting tenant override → rejected at API surface |
| `parent_chat_history_not_visible_to_child` | runner | child's `ChatStart.messages` = system prompt + goal, no parent history |
| `evolution_signals_link_child_to_parent_trace` | evolution | child signal carries `parent_trace_id`; join query returns subtree |
| `e2e_research_fanout_beats_serial_walltime` | integration | Wave 4 acceptance — 3-way fan-out completes in `< 0.7 *` serial baseline |

## Config knobs

```toml
[subagent]
enabled = true
max_concurrent_per_parent = 3
max_concurrent_per_tenant = 15
max_depth = 2
default_timeout_seconds = 60
max_wall_seconds_ceiling = 300        # caps task.max_wall_seconds from above
default_tool_allowlist = "inherit"    # "inherit" | "empty" | ["explicit", "list"]
default_max_tool_calls = 12

[subagent.observability]
emit_hook_events = true               # SubagentSpawned/Completed/TimedOut/DepthCapped
roll_up_evolution_signals = true      # signals query joins child→parent on trace_id
```

Lives next to `[scheduler]` and `[evolution.shadow.sandbox]` in
`corlinman.toml`. Validated through the existing `corlinman_core::config`
typed-config pipeline (`rust/crates/corlinman-core/src/config.rs`).

## Open questions for the implementation iteration

1. **Should child see parent's chat history?** Default: **no**
   (clean context is the whole point). But "summarise this
   conversation so far" needs it. Resolution: add
   `task.include_parent_history: bool = False` plus
   `[subagent].max_inherited_history_pairs = 5`. Opt-in; token-bounded.
2. **Result-merge format**: design fixes JSON for v1 — LLMs consume
   JSON tool results well. Alternatives (typed schema, free-form
   markdown) revisit in W5 once usage patterns surface.
3. **Operator UI tree visualisation**: (a) inline in
   `/admin/sessions/:id` as a collapsible tree under the spawn
   message; (b) a standalone `/admin/subagents` page with a
   tree-graph. Lean: **inline first**. UI work is out of scope here
   — D3 captures the data model only.
4. **Do children's evolution signals roll up to parent?** Lean:
   write-time linkage via `parent_trace_id`, query-time aggregation.
   Engine treats child signals as separate clusters by default; an
   explicit "include subagents" flag joins them. Prevents accidental
   amplification of child noise into parent-scoped proposals.
5. **Orphaned children when parent connection drops** — child runs
   to completion (or timeout), supervisor emits `Orphaned` hook
   event, result discarded. Not a queued background job.

## Implementation order — 10 iterations

Each item is a single bounded iteration (~30 min - 2.5 hours).

1. **Types + tool-name reservation** — `corlinman_agent/subagent/api.py`
   with `TaskSpec` / `TaskResult` / `ParentContext` dataclasses;
   reserve the `subagent.spawn` tool name in the agent registry
   (`corlinman_agent/agents/registry.py`); add `[subagent]` fields to
   `corlinman_core::config`. Tests: `task_spec_serialises_round_trip`
   + config parse with defaults.
2. **`ReadOnlyMemoryHost` adapter** — new module next to
   `corlinman-memory-host/src/federation.rs`; impl `MemoryHost`;
   query passes through, upsert/delete return `Err`. Tests:
   `inherited_memory_host_is_readonly`; RRF roundtrip through
   `FederatedMemoryHost` still works.
3. **`SubagentSupervisor` skeleton** — new Rust crate
   `corlinman-subagent`; `Supervisor` with per-parent / per-tenant /
   depth `DashMap` counters; `try_acquire(parent_ctx) -> Option<Slot>`
   increments on hit, returns `None` on cap; drop-guard decrements.
   Tests: `concurrency_cap_rejects_fourth_when_three_in_flight`,
   `tenant_quota_caps_across_parents`,
   `depth_cap_blocks_grandchild_at_depth_2` (rejection path only).
4. **`run_child` Python runner — happy path** — build child
   `ChatStart`, seed persona via `corlinman_persona.seed_from_card`
   with mangled `agent_id`, drive a fresh `ReasoningLoop`, drain
   into `TaskResult`. Mock provider; no timeout / cap / filtering
   yet. Tests: `spawn_child_happy_path_returns_output`,
   `child_session_key_distinct_from_parent`,
   `child_persona_row_freshly_created`,
   `child_persona_row_under_same_tenant`,
   `parent_chat_history_not_visible_to_child`.
5. **PyO3 bridge** — `corlinman-subagent` exposes a `pyo3` module;
   tool wrapper acquires a slot, `Python::with_gil` re-enters
   `run_child`, releases slot in `finally`. Tests: spawn-via-bridge
   happy path; concurrency counter correctness across FFI (sleeping
   mock holds slot, Rust side observes cap).
6. **Timeout enforcement** — wrap `run_child` in
   `tokio::time::timeout(...)`; on expiry call
   `loop.cancel("subagent_timeout")`, await 2s, force-drop;
   `finish_reason="timeout"`. Tests:
   `child_timeout_returns_partial_output`,
   `child_timeout_decrements_concurrency`.
7. **Tool-allowlist filtering + escalation reject** —
   `_filter_tools_for_child` = `parent.tools_allowed ∩
   (task.tool_allowlist or parent.tools_allowed)` minus
   `subagent.spawn` at `depth >= max_depth - 1`; escalation beyond
   parent's set rejects with `tool_allowlist_escalation`. Tests:
   `tool_allowlist_escalation_rejected`,
   `subagent_spawn_pruned_at_depth_n_minus_1`.
8. **Tool-wrapper registration + parent-loop integration** —
   register `subagent.spawn` so the LLM emits
   `ToolCallEvent("subagent.spawn", {...})`; gateway dispatcher
   routes into `Supervisor::spawn`; `TaskResult` JSON becomes the
   `ToolResult.content` fed via
   `reasoning_loop.py:166-172`. E2E through the agent servicer
   (`corlinman_server/agent_servicer.py:110-190`); parent loop sees
   tool result and continues.
9. **Hook events + evolution-signal linking** — emit
   `SubagentSpawned/Completed/TimedOut/DepthCapped` on the hook bus
   (mirrors the `EngineRunCompleted/Failed` shape at
   `corlinman-scheduler/src/lib.rs:14-19`); child signals carry
   `parent_trace_id`. Tests:
   `evolution_signals_link_child_to_parent_trace` + bus assertions
   for all four event kinds.
10. **E2E Wave 4 acceptance benchmark** — research-fan-out scenario
    ("research X, summarize, draft 3 angles") with 3 children each
    on a sub-topic. Measure wall-clock vs serial baseline.
    Acceptance: `< 0.7 *` serial (`phase4-roadmap.md:309,429`).
    Lives in `python/packages/corlinman-server/tests/integration/`.

## Out of scope (D3)

- **Long-running children that survive parent** — no queued
  background jobs, no cron-triggered subagents. Background-job
  delegation is Phase 5 (would compose with `corlinman-scheduler`'s
  `ActionSpec::RunAgent` stub at `scheduler/src/jobs.rs:39-41`).
- **Subagent-to-subagent peer messaging** — siblings cannot
  communicate at runtime; the parent composes outputs.
  Peer-messaging needs a shared bus and a different governance model.
- **Cross-tenant delegation** — rejected at the API surface; a
  "shared infra agent" pattern is a federation question (W2 work),
  not a delegation one.
- **Subagent persona inheritance from parent** — fresh-only by
  design (`phase4-roadmap.md:415`); inheriting parent's mood/fatigue
  defeats isolation.
- **Operator UI for the subagent tree** — see open question 3;
  data model captured here, UI lands as a follow-up.
- **Subagent-driven memory writes** — children read federated memory
  read-only; only the parent (or evolution loop) writes.
