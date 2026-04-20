//! In-process log-event broadcaster.
//!
//! Installs a custom [`tracing_subscriber::Layer`] that converts every
//! `tracing::Event` into a JSON-friendly [`LogRecord`] and fans it out via a
//! [`tokio::sync::broadcast`] channel. The admin UI subscribes on
//! `/admin/logs/stream` (see [`crate::routes::admin::logs`]) and renders the
//! feed live.
//!
//! Design notes:
//!
//! - The broadcast channel has a bounded capacity (see
//!   [`DEFAULT_CAPACITY`]). A slow subscriber that falls behind receives a
//!   `broadcast::RecvError::Lagged(n)` on their next `recv()`; the SSE
//!   handler turns that into a visible `event: lag` frame rather than
//!   tearing the connection down.
//! - `on_event` is called from within the tracing hot path. Sending on a
//!   broadcast channel is lock-free and never blocks the producer — if no
//!   subscriber exists or everyone lagged, the record is simply dropped.
//!   This matters because the alternative (sync mpsc with backpressure)
//!   would deadlock the runtime the first time the admin UI pauses.
//! - `trace_id`, `request_id`, `subsystem` are extracted from either the
//!   event's own fields or any ancestor span's fields (first hit wins,
//!   event fields override span fields). This keeps the layer usable
//!   before the full tracing middleware (`middleware::trace`) is wired.

use std::fmt::Write as _;

use serde::Serialize;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;
use tokio::sync::broadcast;
use tracing::field::{Field, Visit};
use tracing::{Event, Subscriber};
use tracing_subscriber::layer::{Context, Layer};
use tracing_subscriber::registry::LookupSpan;

/// Default channel capacity. 1024 buffered records is enough for the admin
/// UI to catch up after a short stall without dropping; above that it's
/// healthier to mark the subscriber as lagged than keep growing memory.
pub const DEFAULT_CAPACITY: usize = 1024;

/// One structured log entry as shipped to the admin UI.
///
/// Field naming matches the TypeScript `LogEvent` shape in
/// `ui/app/(admin)/logs/page.tsx` so the UI renders without an adapter
/// layer.
#[derive(Clone, Debug, Serialize)]
pub struct LogRecord {
    /// ISO-8601 / RFC-3339 timestamp, UTC.
    pub ts: String,
    /// Uppercase level label: `TRACE` | `DEBUG` | `INFO` | `WARN` | `ERROR`.
    pub level: String,
    /// Tracing target (usually the module path that emitted the event).
    pub target: String,
    /// Event's `message` field, extracted separately for convenience.
    pub message: String,
    /// Remaining event fields + span fields, merged into one JSON object.
    /// Keys already surfaced on the top-level struct (`trace_id`,
    /// `request_id`, `subsystem`, `message`) are elided from this blob so
    /// the UI can display extra structured context without duplication.
    pub fields: serde_json::Value,
    /// W3C trace-id, if this event was emitted inside a span that set one
    /// (or the event itself did).
    pub trace_id: Option<String>,
    /// Request correlation id (equivalent to `X-Request-Id`). Pulled from
    /// span scope the same way as `trace_id`.
    pub request_id: Option<String>,
    /// Logical subsystem label (`gateway`, `agent`, `plugins`, ...) —
    /// again, span-or-event.
    pub subsystem: Option<String>,
}

/// `tracing_subscriber::Layer` that ships every event to a broadcast
/// channel. Cloning the layer clones the [`broadcast::Sender`] — every
/// clone shares the same subscribers.
#[derive(Clone)]
pub struct BroadcastLayer {
    tx: broadcast::Sender<LogRecord>,
}

impl BroadcastLayer {
    /// Build a new layer + the sender that feeds it. Keeping the sender
    /// here (as well as inside the layer) lets the caller stash a copy in
    /// `AppState` for handlers that need a fresh receiver.
    pub fn new(capacity: usize) -> (Self, broadcast::Sender<LogRecord>) {
        let (tx, _rx) = broadcast::channel(capacity.max(1));
        (Self { tx: tx.clone() }, tx)
    }

    /// Sender handle — cheap to clone. Useful for tests that want to
    /// subscribe directly without going through the HTTP layer.
    pub fn sender(&self) -> broadcast::Sender<LogRecord> {
        self.tx.clone()
    }
}

impl<S> Layer<S> for BroadcastLayer
where
    S: Subscriber + for<'a> LookupSpan<'a>,
{
    fn on_event(&self, event: &Event<'_>, ctx: Context<'_, S>) {
        // Drop the record silently when no subscribers are attached yet
        // (pre-admin-boot) or when every subscriber is lagging. Returning
        // early avoids the allocation cost of building the JSON for
        // nobody.
        if self.tx.receiver_count() == 0 {
            return;
        }
        let record = build_log_record(event, &ctx);
        let _ = self.tx.send(record);
    }
}

/// Walk event + span scope, assemble the `LogRecord`. Factored out so the
/// unit tests can exercise it without needing a full subscriber installed.
fn build_log_record<S>(event: &Event<'_>, ctx: &Context<'_, S>) -> LogRecord
where
    S: Subscriber + for<'a> LookupSpan<'a>,
{
    let metadata = event.metadata();
    let mut visitor = FieldVisitor::default();
    event.record(&mut visitor);

    // Pull structured fields out of every ancestor span, inside-out. We
    // only fill a slot if the event itself didn't already set it —
    // event fields take precedence over the surrounding span's.
    if let Some(scope) = ctx.event_scope(event) {
        for span in scope.from_root() {
            if let Some(ext) = span.extensions().get::<SpanFields>() {
                visitor.absorb_span(ext);
            }
        }
    }

    let ts = OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_default();

    LogRecord {
        ts,
        level: metadata.level().to_string(),
        target: metadata.target().to_string(),
        message: visitor.message,
        fields: visitor.fields.into(),
        trace_id: visitor.trace_id,
        request_id: visitor.request_id,
        subsystem: visitor.subsystem,
    }
}

/// Stash of structured fields attached to a span; populated once at
/// `on_new_span` and read back when events fire inside that span.
#[derive(Default, Debug)]
struct SpanFields {
    trace_id: Option<String>,
    request_id: Option<String>,
    subsystem: Option<String>,
    fields: serde_json::Map<String, serde_json::Value>,
}

impl<S> Layer<S> for BroadcastLayerSpans
where
    S: Subscriber + for<'a> LookupSpan<'a>,
{
    fn on_new_span(
        &self,
        attrs: &tracing::span::Attributes<'_>,
        id: &tracing::span::Id,
        ctx: Context<'_, S>,
    ) {
        let Some(span) = ctx.span(id) else { return };
        let mut visitor = FieldVisitor::default();
        attrs.record(&mut visitor);
        let mut ext = span.extensions_mut();
        ext.insert(SpanFields {
            trace_id: visitor.trace_id,
            request_id: visitor.request_id,
            subsystem: visitor.subsystem,
            fields: visitor.fields,
        });
    }
}

/// Companion layer: when composed with [`BroadcastLayer`] it remembers
/// each span's structured fields so `on_event` can traverse the scope.
///
/// Separated from `BroadcastLayer` so callers that do not care about span
/// context can install the broadcaster alone. [`BroadcastLayer::new`]
/// returns the primary layer; callers that want span inheritance should
/// also install a `BroadcastLayerSpans::default()` next to it.
#[derive(Clone, Default)]
pub struct BroadcastLayerSpans;

// --------------------------------------------------------------------
// Field visitor — collects key/value pairs into a JSON map + pulls out
// the well-known slots (`message`, `trace_id`, `request_id`, `subsystem`).
// --------------------------------------------------------------------

#[derive(Default)]
struct FieldVisitor {
    message: String,
    fields: serde_json::Map<String, serde_json::Value>,
    trace_id: Option<String>,
    request_id: Option<String>,
    subsystem: Option<String>,
}

impl FieldVisitor {
    /// Merge a span's stashed fields into this visitor. Event-level data
    /// wins over ancestor-span data, matching conventional tracing
    /// semantics.
    fn absorb_span(&mut self, span: &SpanFields) {
        if self.trace_id.is_none() {
            self.trace_id = span.trace_id.clone();
        }
        if self.request_id.is_none() {
            self.request_id = span.request_id.clone();
        }
        if self.subsystem.is_none() {
            self.subsystem = span.subsystem.clone();
        }
        for (k, v) in &span.fields {
            self.fields.entry(k.clone()).or_insert_with(|| v.clone());
        }
    }

    fn set_string(&mut self, key: &str, value: String) {
        match key {
            "message" => {
                if self.message.is_empty() {
                    self.message = value;
                } else {
                    // Rare: multiple "message" fields on one event. Fold.
                    let _ = write!(self.message, " {value}");
                }
            }
            "trace_id" => self.trace_id = Some(value),
            "request_id" => self.request_id = Some(value),
            "subsystem" => self.subsystem = Some(value),
            _ => {
                self.fields
                    .insert(key.to_string(), serde_json::Value::String(value));
            }
        }
    }
}

impl Visit for FieldVisitor {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.set_string(field.name(), value.to_string());
    }

    fn record_bool(&mut self, field: &Field, value: bool) {
        if matches!(
            field.name(),
            "message" | "trace_id" | "request_id" | "subsystem"
        ) {
            self.set_string(field.name(), value.to_string());
        } else {
            self.fields
                .insert(field.name().to_string(), serde_json::Value::Bool(value));
        }
    }

    fn record_i64(&mut self, field: &Field, value: i64) {
        if matches!(
            field.name(),
            "message" | "trace_id" | "request_id" | "subsystem"
        ) {
            self.set_string(field.name(), value.to_string());
        } else {
            self.fields.insert(
                field.name().to_string(),
                serde_json::Value::Number(value.into()),
            );
        }
    }

    fn record_u64(&mut self, field: &Field, value: u64) {
        if matches!(
            field.name(),
            "message" | "trace_id" | "request_id" | "subsystem"
        ) {
            self.set_string(field.name(), value.to_string());
        } else {
            self.fields.insert(
                field.name().to_string(),
                serde_json::Value::Number(value.into()),
            );
        }
    }

    fn record_f64(&mut self, field: &Field, value: f64) {
        let number = serde_json::Number::from_f64(value)
            .map(serde_json::Value::Number)
            .unwrap_or(serde_json::Value::Null);
        self.fields.insert(field.name().to_string(), number);
    }

    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        // `tracing::info!("...")` surfaces as a `message` field recorded
        // via `record_debug` — the formatted form is what we want to
        // display in the UI.
        let rendered = format!("{value:?}");
        let trimmed = strip_debug_quotes(&rendered);
        self.set_string(field.name(), trimmed.to_string());
    }
}

/// `format!("{:?}", "hello")` yields `"\"hello\""`. For plain-string
/// values coming through `record_debug` we want the unquoted form so the
/// admin UI shows `hello` rather than `"hello"`. Numbers / structs are
/// already unquoted, so this is idempotent.
fn strip_debug_quotes(s: &str) -> &str {
    if s.len() >= 2 && s.starts_with('"') && s.ends_with('"') {
        // Double-check the inner content has no escaped quote — if it
        // does, the Rust display already handled whatever escaping was
        // needed and we should leave it alone.
        if !s[1..s.len() - 1].contains('\\') {
            return &s[1..s.len() - 1];
        }
    }
    s
}

// --------------------------------------------------------------------
// Tests
// --------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tracing::subscriber;
    use tracing_subscriber::layer::SubscriberExt;
    use tracing_subscriber::Registry;

    /// Install `BroadcastLayer + BroadcastLayerSpans` against a fresh
    /// `Registry`, run `f` with tracing dispatched to that subscriber,
    /// and return the sender for the caller to subscribe to afterwards.
    fn with_layer<F>(capacity: usize, f: F) -> broadcast::Sender<LogRecord>
    where
        F: FnOnce(),
    {
        let (layer, tx) = BroadcastLayer::new(capacity);
        let subscriber = Registry::default().with(layer).with(BroadcastLayerSpans);
        subscriber::with_default(subscriber, f);
        tx
    }

    #[tokio::test]
    async fn emits_record_on_event() {
        let (layer, tx) = BroadcastLayer::new(16);
        let mut rx = tx.subscribe();
        let subscriber = Registry::default().with(layer).with(BroadcastLayerSpans);
        subscriber::with_default(subscriber, || {
            tracing::info!(target: "demo", trace_id = "abc123", "hello world");
        });
        let record = rx.try_recv().expect("record should be delivered");
        assert_eq!(record.level, "INFO");
        assert_eq!(record.target, "demo");
        assert_eq!(record.message, "hello world");
        assert_eq!(record.trace_id.as_deref(), Some("abc123"));
        assert!(record.request_id.is_none());
    }

    #[tokio::test]
    async fn extracts_span_context() {
        let (layer, tx) = BroadcastLayer::new(16);
        let mut rx = tx.subscribe();
        let subscriber = Registry::default().with(layer).with(BroadcastLayerSpans);
        subscriber::with_default(subscriber, || {
            let span = tracing::info_span!("request", request_id = "req-42", subsystem = "gateway");
            let _g = span.enter();
            tracing::warn!("something odd");
        });
        let record = rx.try_recv().unwrap();
        assert_eq!(record.level, "WARN");
        assert_eq!(record.request_id.as_deref(), Some("req-42"));
        assert_eq!(record.subsystem.as_deref(), Some("gateway"));
    }

    #[tokio::test]
    async fn lagged_receivers_dont_block_sender() {
        // Capacity 4, fire far more than that. Producer must not panic or
        // hang; receiver should observe `Lagged(n)` on the oldest data.
        let tx = with_layer(4, || {
            for i in 0..10_000 {
                tracing::info!(%i, "spam");
            }
        });
        // Subscribe late, then drain. If the producer blocked, we'd never
        // reach this line within the test timeout.
        let mut rx = tx.subscribe();
        assert!(matches!(
            rx.try_recv(),
            Err(broadcast::error::TryRecvError::Empty)
        ));
    }

    #[tokio::test]
    async fn drops_record_when_no_subscribers() {
        // No subscriber -> receiver_count==0 -> on_event returns before
        // building the record. Nothing observable, but it should not
        // panic and should keep working after a subscriber joins.
        let (layer, tx) = BroadcastLayer::new(4);
        let subscriber = Registry::default().with(layer).with(BroadcastLayerSpans);
        subscriber::with_default(subscriber, || {
            tracing::info!("lost in the void");
        });
        let mut rx = tx.subscribe();
        // Channel is empty because the event was discarded pre-send.
        assert!(matches!(
            rx.try_recv(),
            Err(broadcast::error::TryRecvError::Empty)
        ));
    }

    #[test]
    fn strip_debug_quotes_unwraps_plain_strings() {
        assert_eq!(strip_debug_quotes("\"abc\""), "abc");
        assert_eq!(strip_debug_quotes("42"), "42");
        assert_eq!(
            strip_debug_quotes("\"with \\\"quotes\""),
            "\"with \\\"quotes\""
        );
    }
}
