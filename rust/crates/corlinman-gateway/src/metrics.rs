//! Prometheus metrics facade for the gateway.
//!
//! Since S7 the actual metric handles live in [`corlinman_core::metrics`] so
//! the plugin / agent-client / vector crates can instrument their own call
//! sites without a reverse dependency on this crate. This module re-exports
//! every handle under its historical path so existing call sites compile
//! unchanged and the Prometheus metric *names* are identical.
//!
//! # Naming + labels
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

pub use corlinman_core::metrics::{
    encode, AGENT_GRPC_INFLIGHT, AGENT_MUTES_TOTAL, APPROVALS_TOTAL, BACKOFF_RETRIES,
    CHANNELS_RATE_LIMITED, CHAT_STREAM_DURATION, EVOLUTION_CHUNKS_DELETED, EVOLUTION_CHUNKS_MERGED,
    EVOLUTION_PROPOSALS_APPLIED, EVOLUTION_PROPOSALS_DECISION, EVOLUTION_PROPOSALS_LISTED,
    EVOLUTION_SIGNALS_DROPPED, EVOLUTION_SIGNALS_OBSERVED, EVOLUTION_SIGNALS_QUEUE_DEPTH,
    FILE_FETCHER_BYTES_TOTAL, FILE_FETCHER_FETCHES_TOTAL, HOOK_EMITS_TOTAL,
    HOOK_SUBSCRIBERS_CURRENT, HTTP_REQUESTS, LOG_FILES_REMOVED, PLUGIN_EXECUTE_DURATION,
    PLUGIN_EXECUTE_TOTAL, PROTOCOL_DISPATCH_ERRORS, PROTOCOL_DISPATCH_TOTAL,
    RATE_LIMIT_TRIGGERS_TOTAL, REGISTRY, SKILL_INVOCATIONS_TOTAL, TELEGRAM_MEDIA_TOTAL,
    TELEGRAM_UPDATES_TOTAL, VECTOR_QUERY_DURATION, WSTOOL_INVOKES_TOTAL, WSTOOL_INVOKE_DURATION,
    WSTOOL_RUNNERS_CONNECTED,
};

/// Eagerly touch every metric so names appear in `/metrics` even before the
/// first data point. Also pre-registers a `startup`-labelled sentinel so
/// label-aware series exist from the first scrape.
pub fn init() {
    corlinman_core::metrics::init();

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
    CHAT_STREAM_DURATION
        .with_label_values(&["startup", "startup"])
        .observe(0.0);
    PLUGIN_EXECUTE_DURATION
        .with_label_values(&["startup"])
        .observe(0.0);
    VECTOR_QUERY_DURATION
        .with_label_values(&["startup"])
        .observe(0.0);

    // B1–B4 sentinels so each new family has at least one labelled sample.
    PROTOCOL_DISPATCH_TOTAL
        .with_label_values(&["startup"])
        .inc_by(0.0);
    PROTOCOL_DISPATCH_ERRORS
        .with_label_values(&["startup", "startup"])
        .inc_by(0.0);
    WSTOOL_INVOKES_TOTAL
        .with_label_values(&["startup", "true"])
        .inc_by(0.0);
    WSTOOL_INVOKE_DURATION
        .with_label_values(&["startup"])
        .observe(0.0);
    FILE_FETCHER_FETCHES_TOTAL
        .with_label_values(&["startup", "true"])
        .inc_by(0.0);
    FILE_FETCHER_BYTES_TOTAL
        .with_label_values(&["startup"])
        .inc_by(0.0);
    TELEGRAM_UPDATES_TOTAL
        .with_label_values(&["startup", "startup"])
        .inc_by(0.0);
    TELEGRAM_MEDIA_TOTAL
        .with_label_values(&["startup"])
        .inc_by(0.0);
    HOOK_EMITS_TOTAL
        .with_label_values(&["startup", "startup"])
        .inc_by(0.0);
    HOOK_SUBSCRIBERS_CURRENT
        .with_label_values(&["startup"])
        .set(0);
    SKILL_INVOCATIONS_TOTAL
        .with_label_values(&["startup"])
        .inc_by(0.0);
    AGENT_MUTES_TOTAL
        .with_label_values(&["startup"])
        .inc_by(0.0);
    RATE_LIMIT_TRIGGERS_TOTAL
        .with_label_values(&["startup"])
        .inc_by(0.0);
    APPROVALS_TOTAL.with_label_values(&["startup"]).inc_by(0.0);
    EVOLUTION_SIGNALS_OBSERVED
        .with_label_values(&["startup", "info"])
        .inc_by(0.0);
    EVOLUTION_SIGNALS_DROPPED.inc_by(0.0);
    EVOLUTION_SIGNALS_QUEUE_DEPTH.set(0);
    EVOLUTION_PROPOSALS_LISTED.inc_by(0.0);
    EVOLUTION_PROPOSALS_DECISION
        .with_label_values(&["startup"])
        .inc_by(0.0);
    EVOLUTION_PROPOSALS_APPLIED
        .with_label_values(&["startup", "startup"])
        .inc_by(0.0);
    EVOLUTION_CHUNKS_MERGED.inc_by(0.0);
    EVOLUTION_CHUNKS_DELETED.inc_by(0.0);
    LOG_FILES_REMOVED
        .with_label_values(&["startup"])
        .inc_by(0.0);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The text encoder output must contain every metric name declared here.
    #[test]
    fn encode_contains_all_metric_names() {
        init();
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
        assert!(
            body.contains(r#"route="/v1/chat/completions""#),
            "missing labelled counter sample:\n{body}"
        );
    }

    #[test]
    fn http_counter_increments() {
        init();
        let before = HTTP_REQUESTS.with_label_values(&["/health", "200"]).get();
        HTTP_REQUESTS.with_label_values(&["/health", "200"]).inc();
        let after = HTTP_REQUESTS.with_label_values(&["/health", "200"]).get();
        assert_eq!(after, before + 1.0);
    }

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
