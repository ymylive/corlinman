//! Prometheus metrics registry for corlinman (plan §9).
//!
//! A single process-wide [`Registry`] is constructed lazily on first touch.
//! Each metric is registered exactly once when its `Lazy` is first forced,
//! so importers pay only for the metrics they actually use.
//!
//! # Naming + labels
//!
//! Metric names follow the `corlinman_<subsystem>_<name>[_unit]` convention.
//! Labels are low-cardinality only — per-request ids never appear as labels,
//! they belong in tracing spans instead.
//!
//! | Metric                                     | Type      | Labels              |
//! |--------------------------------------------|-----------|---------------------|
//! | `corlinman_http_requests_total`            | CounterV  | `route`, `status`   |
//! | `corlinman_chat_stream_duration_seconds`   | HistogramV| `model`, `finish`   |
//! | `corlinman_plugin_execute_total`           | CounterV  | `plugin`, `status`  |
//! | `corlinman_plugin_execute_duration_seconds`| HistogramV| `plugin`            |
//! | `corlinman_backoff_retries_total`          | CounterV  | `reason`            |
//! | `corlinman_agent_grpc_inflight`            | IntGauge  | —                   |
//! | `corlinman_channels_rate_limited_total`    | CounterV  | `channel`, `reason` |
//! | `corlinman_vector_query_duration_seconds`  | HistogramV| `stage`             |
//!
//! # Wiring
//!
//! The `/metrics` endpoint lives in [`crate::routes::metrics`] and calls
//! [`encode`] on every scrape. Call-site instrumentation for the plugin /
//! vector / channel crates is deliberately *declared* here so the symbols
//! exist before the parallel agents land their real implementations — see
//! the TODO list at the bottom of this module for the handoff.

use once_cell::sync::Lazy;
use prometheus::{CounterVec, HistogramOpts, HistogramVec, IntGauge, Opts, Registry};

/// Process-wide registry. All Lazy metrics below register into this.
pub static REGISTRY: Lazy<Registry> = Lazy::new(Registry::new);

/// `corlinman_http_requests_total{route, status}`.
///
/// Incremented by the gateway tracing middleware on every HTTP response.
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
///
/// Observed when an SSE chat stream closes. Buckets cover 50 ms .. 120 s to
/// catch both short completions and long reasoning loops with tool calls.
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
/// Declared here; populated by `corlinman-plugins::runtime` (parallel M7
/// agent). `status ∈ {"ok", "error", "timeout", "oom", "denied"}`.
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
///
/// Bumped by `corlinman-agent-client::retry::with_retry` on each scheduled
/// retry tick. `reason` mirrors `FailoverReason::as_str`.
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

/// `corlinman_agent_grpc_inflight` gauge — active `Agent.Chat` streams.
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
///
/// Bumped by [`corlinman_channels::router::ChannelRouter`] every time a
/// message is silently dropped by a token-bucket check. `reason ∈
/// {"group", "sender"}`; `channel` mirrors `ChannelBinding.channel`
/// (e.g. `"qq"`, later `"telegram"`).
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

/// `corlinman_vector_query_duration_seconds{stage}` — `stage ∈ {hnsw, bm25, fuse}`.
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

/// Encode the registry in Prometheus text-exposition v0.0.4 format.
pub fn encode() -> Vec<u8> {
    use prometheus::Encoder;
    let mut buf = Vec::new();
    let encoder = prometheus::TextEncoder::new();
    // `encode` only fails on I/O; a `Vec<u8>` sink can't fail.
    let _ = encoder.encode(&REGISTRY.gather(), &mut buf);
    buf
}

/// Eagerly touch every Lazy so names appear in `/metrics` even before the
/// first data point. Called once during gateway bootstrap and in tests.
///
/// For `CounterVec` / `HistogramVec` we also pre-register a low-cost
/// sentinel label (`startup`) with an `inc_by(0)` / no-op observation so
/// `/metrics` exposes the metric family from the very first scrape — this
/// matters for dashboards and alert rules that reference the series by
/// name before any real traffic has arrived.
pub fn init() {
    Lazy::force(&HTTP_REQUESTS);
    Lazy::force(&CHAT_STREAM_DURATION);
    Lazy::force(&PLUGIN_EXECUTE_TOTAL);
    Lazy::force(&PLUGIN_EXECUTE_DURATION);
    Lazy::force(&BACKOFF_RETRIES);
    Lazy::force(&AGENT_GRPC_INFLIGHT);
    Lazy::force(&CHANNELS_RATE_LIMITED);
    Lazy::force(&VECTOR_QUERY_DURATION);

    // Zero-valued sentinels. `inc_by(0.0)` on a counter realises the series
    // without bumping the count.
    HTTP_REQUESTS
        .with_label_values(&["startup", "0"])
        .inc_by(0.0);
    PLUGIN_EXECUTE_TOTAL
        .with_label_values(&["startup", "ok"])
        .inc_by(0.0);
    BACKOFF_RETRIES.with_label_values(&["startup"]).inc_by(0.0);
    CHANNELS_RATE_LIMITED
        .with_label_values(&["startup", "startup"])
        .inc_by(0.0);
    // Histograms don't have a cheap "no-op"; a single 0-second observation
    // is negligible and confined to a `startup` label so it's easy to
    // ignore in dashboards.
    CHAT_STREAM_DURATION
        .with_label_values(&["startup", "startup"])
        .observe(0.0);
    PLUGIN_EXECUTE_DURATION
        .with_label_values(&["startup"])
        .observe(0.0);
    VECTOR_QUERY_DURATION
        .with_label_values(&["startup"])
        .observe(0.0);
}

// TODO(M7+): plugin executor call-site wiring lives in
//   `corlinman-plugins::runtime::jsonrpc_stdio::execute` — a parallel agent
//   owns that crate. The metric handles above are public so they can import
//   `corlinman_gateway::metrics::PLUGIN_EXECUTE_TOTAL` directly, or consume
//   a trait hook we add later to avoid a reverse dep.
//
// TODO(M7+): OpenTelemetry exporter (OTLP gRPC to a collector) — plan §9
//   mentions this as optional; not in scope for this slice.
//
// TODO(M7+): Grafana dashboard JSON in `ops/dashboards/corlinman.json`.

#[cfg(test)]
mod tests {
    use super::*;

    /// The text encoder output must contain every metric name declared here.
    #[test]
    fn encode_contains_all_metric_names() {
        init();
        // Touch a label to realise the series in the registry.
        HTTP_REQUESTS
            .with_label_values(&["/v1/chat/completions", "200"])
            .inc();
        CHAT_STREAM_DURATION
            .with_label_values(&["claude-sonnet-4-5", "stop"])
            .observe(0.42);
        PLUGIN_EXECUTE_TOTAL
            .with_label_values(&["FooPlugin", "ok"])
            .inc();
        PLUGIN_EXECUTE_DURATION
            .with_label_values(&["FooPlugin"])
            .observe(0.12);
        BACKOFF_RETRIES.with_label_values(&["rate_limit"]).inc();
        AGENT_GRPC_INFLIGHT.inc();
        CHANNELS_RATE_LIMITED
            .with_label_values(&["qq", "group"])
            .inc();
        VECTOR_QUERY_DURATION
            .with_label_values(&["hnsw"])
            .observe(0.008);

        let body = String::from_utf8(encode()).expect("utf8");
        for needle in [
            "corlinman_http_requests_total",
            "corlinman_chat_stream_duration_seconds",
            "corlinman_plugin_execute_total",
            "corlinman_plugin_execute_duration_seconds",
            "corlinman_backoff_retries_total",
            "corlinman_agent_grpc_inflight",
            "corlinman_channels_rate_limited_total",
            "corlinman_vector_query_duration_seconds",
        ] {
            assert!(body.contains(needle), "missing {needle} in:\n{body}");
        }
        // The line we just incremented must show up too.
        assert!(
            body.contains(r#"route="/v1/chat/completions""#),
            "missing labelled counter sample:\n{body}"
        );
    }

    /// HTTP counter increments are reflected in subsequent encodes.
    #[test]
    fn http_counter_increments() {
        init();
        let before = HTTP_REQUESTS.with_label_values(&["/health", "200"]).get();
        HTTP_REQUESTS.with_label_values(&["/health", "200"]).inc();
        let after = HTTP_REQUESTS.with_label_values(&["/health", "200"]).get();
        assert_eq!(after, before + 1.0);
    }

    /// Inflight gauge supports inc / dec.
    #[test]
    fn inflight_gauge_tracks_current_value() {
        init();
        let before = AGENT_GRPC_INFLIGHT.get();
        AGENT_GRPC_INFLIGHT.inc();
        AGENT_GRPC_INFLIGHT.inc();
        AGENT_GRPC_INFLIGHT.dec();
        assert_eq!(AGENT_GRPC_INFLIGHT.get(), before + 1);
        AGENT_GRPC_INFLIGHT.dec();
    }
}
