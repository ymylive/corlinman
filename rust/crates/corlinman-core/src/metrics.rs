//! Process-wide Prometheus registry shared by every corlinman-* crate.
//!
//! Lives here (the leaf crate every other crate depends on) so subsystems
//! like the plugin runtime, agent-client retry loop, and vector searcher
//! can observe into the same registry the gateway's `/metrics` handler
//! encodes. Before S7 these handles lived in `corlinman-gateway::metrics`
//! but that created a reverse-dependency problem for the three call-site
//! crates — hence the move. `corlinman-gateway::metrics` re-exports every
//! symbol declared here so existing metric names stay unchanged.
//!
//! Naming + labels are documented next to each metric. Keep it
//! low-cardinality — per-request ids belong in tracing spans.

use once_cell::sync::Lazy;
use prometheus::{
    Counter, CounterVec, HistogramOpts, HistogramVec, IntGauge, IntGaugeVec, Opts, Registry,
};

/// Process-wide registry. All `Lazy` metrics below register into this.
pub static REGISTRY: Lazy<Registry> = Lazy::new(Registry::new);

/// `corlinman_http_requests_total{route, status}`.
pub static HTTP_REQUESTS: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new("corlinman_http_requests_total", "Total HTTP requests"),
        &["route", "status"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register http_requests");
    cv
});

/// `corlinman_chat_stream_duration_seconds{model, finish}`.
pub static CHAT_STREAM_DURATION: Lazy<HistogramVec> = Lazy::new(|| {
    let opts = HistogramOpts::new(
        "corlinman_chat_stream_duration_seconds",
        "End-to-end SSE chat stream duration",
    )
    .buckets(vec![
        0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
    ]);
    let hv = HistogramVec::new(opts, &["model", "finish"]).expect("valid metric");
    REGISTRY
        .register(Box::new(hv.clone()))
        .expect("register chat_stream_duration");
    hv
});

/// `corlinman_plugin_execute_total{plugin, status}`.
///
/// `status ∈ {"ok", "error", "timeout", "oom", "denied", "cancelled"}`.
pub static PLUGIN_EXECUTE_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new("corlinman_plugin_execute_total", "Plugin tool invocations"),
        &["plugin", "status"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register plugin_execute_total");
    cv
});

/// `corlinman_plugin_execute_duration_seconds{plugin}`.
pub static PLUGIN_EXECUTE_DURATION: Lazy<HistogramVec> = Lazy::new(|| {
    let opts = HistogramOpts::new(
        "corlinman_plugin_execute_duration_seconds",
        "Plugin tool invocation wall time",
    )
    .buckets(vec![
        0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
    ]);
    let hv = HistogramVec::new(opts, &["plugin"]).expect("valid metric");
    REGISTRY
        .register(Box::new(hv.clone()))
        .expect("register plugin_execute_duration");
    hv
});

/// `corlinman_backoff_retries_total{reason}`.
pub static BACKOFF_RETRIES: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_backoff_retries_total",
            "Retries performed by the agent-client backoff scheduler",
        ),
        &["reason"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register backoff_retries");
    cv
});

/// `corlinman_agent_grpc_inflight` — active `Agent.Chat` streams.
pub static AGENT_GRPC_INFLIGHT: Lazy<IntGauge> = Lazy::new(|| {
    let g = IntGauge::new(
        "corlinman_agent_grpc_inflight",
        "In-flight Agent.Chat gRPC bidi streams",
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(g.clone()))
        .expect("register agent_grpc_inflight");
    g
});

/// `corlinman_channels_rate_limited_total{channel, reason}`.
pub static CHANNELS_RATE_LIMITED: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_channels_rate_limited_total",
            "Inbound channel messages silently dropped by a rate-limit check",
        ),
        &["channel", "reason"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register channels_rate_limited");
    cv
});

/// `corlinman_vector_query_duration_seconds{stage}` —
/// `stage ∈ {"hnsw", "bm25", "fuse", "rerank"}`.
pub static VECTOR_QUERY_DURATION: Lazy<HistogramVec> = Lazy::new(|| {
    let opts = HistogramOpts::new(
        "corlinman_vector_query_duration_seconds",
        "Hybrid vector query timing per stage",
    )
    .buckets(vec![
        0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
    ]);
    let hv = HistogramVec::new(opts, &["stage"]).expect("valid metric");
    REGISTRY
        .register(Box::new(hv.clone()))
        .expect("register vector_query_duration");
    hv
});

// ---------------------------------------------------------------------------
// B1–B4 instrumentation (protocol dispatch, WsTool, FileFetcher, Telegram,
// hooks, skills, agent mutes, rate-limits, approvals).
//
// These metrics are additive; existing dashboards keep working. Labels stay
// low-cardinality (protocols + tools + schemes + chat types + decisions).
// ---------------------------------------------------------------------------

/// `corlinman_protocol_dispatch_total{protocol}` — dispatch attempts per
/// protocol (`"block" | "openai_function"`).
pub static PROTOCOL_DISPATCH_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_protocol_dispatch_total",
            "Protocol dispatch attempts broken down by protocol",
        ),
        &["protocol"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register protocol_dispatch_total");
    cv
});

/// `corlinman_protocol_dispatch_errors_total{protocol, code}`.
pub static PROTOCOL_DISPATCH_ERRORS: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_protocol_dispatch_errors_total",
            "Protocol dispatch errors by short machine code",
        ),
        &["protocol", "code"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register protocol_dispatch_errors_total");
    cv
});

/// `corlinman_wstool_invokes_total{tool, ok}`.
pub static WSTOOL_INVOKES_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_wstool_invokes_total",
            "WsTool invocations, labelled by tool name and ok=true|false",
        ),
        &["tool", "ok"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register wstool_invokes_total");
    cv
});

/// `corlinman_wstool_invoke_duration_seconds` — WsTool invoke wall time.
pub static WSTOOL_INVOKE_DURATION: Lazy<HistogramVec> = Lazy::new(|| {
    let opts = HistogramOpts::new(
        "corlinman_wstool_invoke_duration_seconds",
        "End-to-end wall time of a WsTool invoke from queue to terminal frame",
    )
    .buckets(vec![0.01, 0.05, 0.1, 0.5, 1.0, 5.0]);
    let hv = HistogramVec::new(opts, &["tool"]).expect("valid metric");
    REGISTRY
        .register(Box::new(hv.clone()))
        .expect("register wstool_invoke_duration");
    hv
});

/// `corlinman_wstool_runners_connected` — currently connected WsTool runners.
pub static WSTOOL_RUNNERS_CONNECTED: Lazy<IntGauge> = Lazy::new(|| {
    let g = IntGauge::new(
        "corlinman_wstool_runners_connected",
        "Currently connected WsTool runner WebSockets",
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(g.clone()))
        .expect("register wstool_runners_connected");
    g
});

/// `corlinman_file_fetcher_fetches_total{scheme, ok}`.
pub static FILE_FETCHER_FETCHES_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_file_fetcher_fetches_total",
            "FileFetcher fetch attempts by URI scheme + ok=true|false",
        ),
        &["scheme", "ok"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register file_fetcher_fetches_total");
    cv
});

/// `corlinman_file_fetcher_bytes_total{scheme}` — total bytes transferred.
pub static FILE_FETCHER_BYTES_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_file_fetcher_bytes_total",
            "Bytes successfully fetched by the FileFetcher, per scheme",
        ),
        &["scheme"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register file_fetcher_bytes_total");
    cv
});

/// `corlinman_telegram_updates_total{chat_type, mention_reason}`.
pub static TELEGRAM_UPDATES_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_telegram_updates_total",
            "Telegram webhook updates processed, by chat type + routing reason",
        ),
        &["chat_type", "mention_reason"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register telegram_updates_total");
    cv
});

/// `corlinman_telegram_media_total{kind}` — `photo|voice|document|text`.
pub static TELEGRAM_MEDIA_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_telegram_media_total",
            "Telegram media attachments observed, by kind",
        ),
        &["kind"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register telegram_media_total");
    cv
});

/// `corlinman_hook_emits_total{event_kind, priority}`.
pub static HOOK_EMITS_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_hook_emits_total",
            "HookBus emits by event kind and priority tier fanned to",
        ),
        &["event_kind", "priority"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register hook_emits_total");
    cv
});

/// `corlinman_hook_subscribers_current{priority}` — per-tier subscriber count.
pub static HOOK_SUBSCRIBERS_CURRENT: Lazy<IntGaugeVec> = Lazy::new(|| {
    let opts = Opts::new(
        "corlinman_hook_subscribers_current",
        "HookBus subscribers currently attached to each priority tier",
    );
    let g = IntGaugeVec::new(opts, &["priority"]).expect("valid metric");
    REGISTRY
        .register(Box::new(g.clone()))
        .expect("register hook_subscribers_current");
    g
});

/// `corlinman_skill_invocations_total{skill_name}`.
pub static SKILL_INVOCATIONS_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_skill_invocations_total",
            "Skill invocations observed on the hot path",
        ),
        &["skill_name"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register skill_invocations_total");
    cv
});

/// `corlinman_agent_mutes_total{expanded_agent}` — expanded-agent mute events.
pub static AGENT_MUTES_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_agent_mutes_total",
            "Expanded-agent mute toggles, by expanded_agent identifier",
        ),
        &["expanded_agent"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register agent_mutes_total");
    cv
});

/// `corlinman_rate_limit_triggers_total{limit_type}`.
pub static RATE_LIMIT_TRIGGERS_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_rate_limit_triggers_total",
            "Rate-limit trigger events, by the `limit_type` dimension",
        ),
        &["limit_type"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register rate_limit_triggers_total");
    cv
});

/// `corlinman_approvals_total{decision}` — `allow|deny|timeout`.
pub static APPROVALS_TOTAL: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "corlinman_approvals_total",
            "Tool-approval decisions, by `allow|deny|timeout`",
        ),
        &["decision"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register approvals_total");
    cv
});

/// `gateway_evolution_signals_observed_total{event_kind, severity}` —
/// signals the gateway's `EvolutionObserver` has persisted into
/// `evolution_signals`. Labels match the `EvolutionSignal` shape so
/// dashboards can slice by adapted hook event and severity.
pub static EVOLUTION_SIGNALS_OBSERVED: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "gateway_evolution_signals_observed_total",
            "Hook events the EvolutionObserver persisted as evolution_signals rows",
        ),
        &["event_kind", "severity"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register evolution_signals_observed_total");
    cv
});

/// `gateway_evolution_signals_dropped_total` — signals the observer had to
/// drop because the bounded write queue was full. Each increment also
/// emits a WARN log line on the offending side.
pub static EVOLUTION_SIGNALS_DROPPED: Lazy<Counter> = Lazy::new(|| {
    let c = Counter::new(
        "gateway_evolution_signals_dropped_total",
        "Hook events the EvolutionObserver dropped due to a full write queue",
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(c.clone()))
        .expect("register evolution_signals_dropped_total");
    c
});

/// `gateway_evolution_signals_queue_depth` — current depth of the
/// observer's bounded write queue. Sampled on each enqueue/dequeue so
/// dashboards can spot backpressure before the queue overflows.
pub static EVOLUTION_SIGNALS_QUEUE_DEPTH: Lazy<IntGauge> = Lazy::new(|| {
    let g = IntGauge::new(
        "gateway_evolution_signals_queue_depth",
        "Current depth of the EvolutionObserver write queue",
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(g.clone()))
        .expect("register evolution_signals_queue_depth");
    g
});

/// `gateway_evolution_proposals_listed_total` — number of times the admin
/// API served a proposal listing (regardless of result size). Mostly a
/// liveness signal for the operator UI's polling loop.
pub static EVOLUTION_PROPOSALS_LISTED: Lazy<Counter> = Lazy::new(|| {
    let c = Counter::new(
        "gateway_evolution_proposals_listed_total",
        "Calls to GET /admin/evolution served by the proposal admin API",
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(c.clone()))
        .expect("register evolution_proposals_listed_total");
    c
});

/// `gateway_evolution_proposals_decision_total{decision}` — successful
/// approve/deny transitions through the admin API. `decision` is one of
/// `approved` | `denied`.
pub static EVOLUTION_PROPOSALS_DECISION: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "gateway_evolution_proposals_decision_total",
            "Approve/deny decisions recorded against evolution_proposals",
        ),
        &["decision"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register evolution_proposals_decision_total");
    cv
});

/// `gateway_evolution_proposals_applied_total{kind, outcome}` — `apply`
/// calls served by the admin API. `kind` mirrors the proposal's
/// `EvolutionKind` (`memory_op` etc); `outcome` is `"ok"` when the
/// `EvolutionApplier` finished cleanly and `"error"` for any failure
/// path (kb mutation, history insert, proposal flip). Phase 2 only
/// applies `memory_op` proposals — other kinds emit
/// `outcome="error"` with `kind` carrying the proposal's actual kind so
/// dashboards can spot unsupported proposal traffic.
pub static EVOLUTION_PROPOSALS_APPLIED: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "gateway_evolution_proposals_applied_total",
            "Proposals transitioned approved → applied via the admin API",
        ),
        &["kind", "outcome"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register evolution_proposals_applied_total");
    cv
});

/// `gateway_evolution_proposals_rolled_back_total{kind}` — proposals
/// transitioned `applied → rolled_back` by the AutoRollback path
/// (Phase 3 W1-B). `kind` mirrors the proposal's `EvolutionKind`. Manual
/// operator-initiated rollbacks (the `rollback_of` flow) are not
/// counted here — they go through the standard apply pipeline as a
/// fresh proposal and increment `EVOLUTION_PROPOSALS_APPLIED` instead.
pub static EVOLUTION_PROPOSALS_ROLLED_BACK: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "gateway_evolution_proposals_rolled_back_total",
            "Proposals auto-reverted by the AutoRollback monitor",
        ),
        &["kind"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register evolution_proposals_rolled_back_total");
    cv
});

/// `gateway_evolution_chunks_merged_total` — successful `merge_chunks:<a>,<b>`
/// applies. Each increment corresponds to one loser chunk being deleted from
/// `kb.sqlite`; the winner content stays untouched.
pub static EVOLUTION_CHUNKS_MERGED: Lazy<Counter> = Lazy::new(|| {
    let c = Counter::new(
        "gateway_evolution_chunks_merged_total",
        "memory_op merge_chunks proposals successfully applied against kb.sqlite",
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(c.clone()))
        .expect("register evolution_chunks_merged_total");
    c
});

/// `gateway_evolution_chunks_deleted_total` — successful `delete_chunk:<id>`
/// applies. Counts standalone deletions only; the loser-side delete inside
/// `merge_chunks` is tracked under `EVOLUTION_CHUNKS_MERGED`.
pub static EVOLUTION_CHUNKS_DELETED: Lazy<Counter> = Lazy::new(|| {
    let c = Counter::new(
        "gateway_evolution_chunks_deleted_total",
        "memory_op delete_chunk proposals successfully applied against kb.sqlite",
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(c.clone()))
        .expect("register evolution_chunks_deleted_total");
    c
});

/// `gateway_log_files_removed_total{reason}` — rotated log files the
/// retention task has deleted. `reason` is currently fixed to `"age"`
/// (mtime older than `logging.file.retention_days`); new eviction
/// policies would add more labels here without breaking dashboards.
pub static LOG_FILES_REMOVED: Lazy<CounterVec> = Lazy::new(|| {
    let cv = CounterVec::new(
        Opts::new(
            "gateway_log_files_removed_total",
            "Rotated gateway log files deleted by the retention task",
        ),
        &["reason"],
    )
    .expect("valid metric");
    REGISTRY
        .register(Box::new(cv.clone()))
        .expect("register log_files_removed_total");
    cv
});

/// Encode the registry in Prometheus text-exposition v0.0.4 format.
pub fn encode() -> Vec<u8> {
    use prometheus::Encoder;
    let mut buf = Vec::new();
    let encoder = prometheus::TextEncoder::new();
    let _ = encoder.encode(&REGISTRY.gather(), &mut buf);
    buf
}

/// Eagerly touch every `Lazy` so names appear in `/metrics` even before
/// the first data point. See the gateway's wrapper `init()` for sentinel
/// label wiring.
pub fn init() {
    Lazy::force(&HTTP_REQUESTS);
    Lazy::force(&CHAT_STREAM_DURATION);
    Lazy::force(&PLUGIN_EXECUTE_TOTAL);
    Lazy::force(&PLUGIN_EXECUTE_DURATION);
    Lazy::force(&BACKOFF_RETRIES);
    Lazy::force(&AGENT_GRPC_INFLIGHT);
    Lazy::force(&CHANNELS_RATE_LIMITED);
    Lazy::force(&VECTOR_QUERY_DURATION);

    // B1–B4 additions.
    Lazy::force(&PROTOCOL_DISPATCH_TOTAL);
    Lazy::force(&PROTOCOL_DISPATCH_ERRORS);
    Lazy::force(&WSTOOL_INVOKES_TOTAL);
    Lazy::force(&WSTOOL_INVOKE_DURATION);
    Lazy::force(&WSTOOL_RUNNERS_CONNECTED);
    Lazy::force(&FILE_FETCHER_FETCHES_TOTAL);
    Lazy::force(&FILE_FETCHER_BYTES_TOTAL);
    Lazy::force(&TELEGRAM_UPDATES_TOTAL);
    Lazy::force(&TELEGRAM_MEDIA_TOTAL);
    Lazy::force(&HOOK_EMITS_TOTAL);
    Lazy::force(&HOOK_SUBSCRIBERS_CURRENT);
    Lazy::force(&SKILL_INVOCATIONS_TOTAL);
    Lazy::force(&AGENT_MUTES_TOTAL);
    Lazy::force(&RATE_LIMIT_TRIGGERS_TOTAL);
    Lazy::force(&APPROVALS_TOTAL);
    Lazy::force(&EVOLUTION_SIGNALS_OBSERVED);
    Lazy::force(&EVOLUTION_SIGNALS_DROPPED);
    Lazy::force(&EVOLUTION_SIGNALS_QUEUE_DEPTH);
    Lazy::force(&EVOLUTION_PROPOSALS_LISTED);
    Lazy::force(&EVOLUTION_PROPOSALS_DECISION);
    Lazy::force(&EVOLUTION_PROPOSALS_APPLIED);
    Lazy::force(&EVOLUTION_PROPOSALS_ROLLED_BACK);
    Lazy::force(&EVOLUTION_CHUNKS_MERGED);
    Lazy::force(&EVOLUTION_CHUNKS_DELETED);
    Lazy::force(&LOG_FILES_REMOVED);
}
