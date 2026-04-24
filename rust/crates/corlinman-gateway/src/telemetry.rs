//! OpenTelemetry OTLP exporter wiring + file-sink layer for the gateway.
//!
//! # Contract
//!
//! - OTLP exporter is activated only when `OTEL_EXPORTER_OTLP_ENDPOINT`
//!   is set. Unset → every OTLP function here returns `None` and tracing
//!   falls back to the existing stdout / broadcast layers.
//! - Failing init is warn-and-continue. The gateway must never refuse to
//!   start because a collector is unreachable or a log file cannot be
//!   opened.
//! - Service name is `corlinman-gateway` unless `OTEL_SERVICE_NAME`
//!   overrides. The service version matches the crate version.
//! - Exporter protocol is gRPC (tonic). The collector endpoint is read
//!   verbatim from `OTEL_EXPORTER_OTLP_ENDPOINT` (e.g.
//!   `http://localhost:4317`).
//!
//! # Layering
//!
//! The caller (`main.rs::init_tracing`) composes a `tracing_subscriber`
//! registry with the JSON stdout layer + log broadcast layer it already
//! builds, plus this module's [`otel_layer`] when enabled and optionally
//! [`build_file_layer`] when `[logging.file]` is configured. That way the
//! gateway continues to function if OTLP or the file sink is disabled.
//!
//! # File-sink caveats
//!
//! The returned [`tracing_appender::non_blocking::WorkerGuard`] **must**
//! be held for the process lifetime. Dropping it stops the background
//! writer thread and any further events are silently discarded. See
//! [`build_file_layer`].
//!
//! # Propagation
//!
//! The global text-map propagator is set to W3C TraceContext so outgoing
//! gRPC requests carry `traceparent`. The tonic client interceptor that
//! injects the header lives in the agent-client crate (see
//! `corlinman_agent_client::trace_propagate`).

use std::env;
use std::path::{Path, PathBuf};

use corlinman_core::config::{FileLoggingConfig, RotationKind};
use opentelemetry::trace::TracerProvider as _;
use opentelemetry::{global, KeyValue};
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::Tracer;
use opentelemetry_sdk::Resource;
use tracing_appender::non_blocking::{NonBlocking, WorkerGuard};
use tracing_appender::rolling::{RollingFileAppender, Rotation};

/// Try to configure an OTLP exporter from the environment.
///
/// Returns `Some(tracer)` when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and
/// the exporter pipeline initialises cleanly, `None` otherwise. Errors
/// downgrade to a `tracing::warn` and `None` so boot continues.
pub fn try_init_tracer() -> Option<Tracer> {
    let endpoint = env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok()?;
    if endpoint.trim().is_empty() {
        return None;
    }

    // Install the W3C propagator *before* building the pipeline so any
    // async work spawned during init still gets traceparent injection.
    global::set_text_map_propagator(TraceContextPropagator::new());

    let service_name =
        env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "corlinman-gateway".to_string());
    let resource = Resource::new(vec![
        KeyValue::new("service.name", service_name),
        KeyValue::new("service.version", env!("CARGO_PKG_VERSION")),
    ]);

    let exporter = opentelemetry_otlp::new_exporter()
        .tonic()
        .with_endpoint(endpoint.clone());

    let pipeline = opentelemetry_otlp::new_pipeline()
        .tracing()
        .with_exporter(exporter)
        .with_trace_config(opentelemetry_sdk::trace::Config::default().with_resource(resource))
        .install_batch(opentelemetry_sdk::runtime::Tokio);

    match pipeline {
        Ok(provider) => {
            tracing::info!(endpoint = %endpoint, "otlp tracer initialised");
            let tracer = provider.tracer("corlinman-gateway");
            // Install globally so other crates can obtain tracers too.
            global::set_tracer_provider(provider);
            Some(tracer)
        }
        Err(err) => {
            tracing::warn!(endpoint = %endpoint, error = %err, "otlp init failed; continuing");
            None
        }
    }
}

/// Best-effort flush + shutdown of the global tracer provider. Safe to
/// call even when [`try_init_tracer`] was never invoked.
pub fn shutdown() {
    global::shutdown_tracer_provider();
}

// ---------------------------------------------------------------------------
// P0-1: file-sink layer (rolling log files backed by `tracing-appender`).
// ---------------------------------------------------------------------------

/// Output of [`build_file_layer`].
///
/// * `writer` is a cloneable, non-blocking [`std::io::Write`]-ish handle
///   produced by [`tracing_appender::non_blocking`]. The caller wires it
///   into a `fmt::layer().with_writer(writer)` layer on the registry so
///   the file sink gets JSON-formatted events identical to stdout.
/// * `guard` keeps the background writer thread alive. **It must not be
///   dropped until the process shuts down**, otherwise the worker joins
///   and subsequent writes are dropped.
/// * `dir` / `prefix` are returned so the retention task knows where
///   (and with which filename prefix) the rotated files live.
pub struct FileSink {
    pub writer: NonBlocking,
    pub guard: WorkerGuard,
    pub dir: PathBuf,
    pub prefix: String,
}

/// Build a non-blocking rolling file-sink from `cfg`.
///
/// Returns `None` when the sink is disabled (empty `path`) or when the
/// parent directory cannot be created. The latter is warn-and-continue:
/// the gateway keeps serving with stdout logging only.
///
/// The returned [`FileSink`] is meant to be turned into a
/// `tracing_subscriber` layer by the caller via
/// `fmt::layer().with_writer(sink.writer.clone()).json()`.
pub fn build_file_layer(cfg: &FileLoggingConfig) -> Option<FileSink> {
    let path = cfg.path.as_path();
    if path.as_os_str().is_empty() {
        return None;
    }
    let (dir, prefix) = split_dir_and_prefix(path)?;

    if let Err(err) = std::fs::create_dir_all(&dir) {
        tracing::warn!(
            dir = %dir.display(),
            error = %err,
            "file log sink: mkdir failed; keeping stdout-only logging",
        );
        return None;
    }

    let rotation = rotation_for(cfg.rotation);
    let appender = RollingFileAppender::new(rotation, &dir, &prefix);
    let (writer, guard) = tracing_appender::non_blocking(appender);

    tracing::info!(
        dir = %dir.display(),
        prefix,
        rotation = ?cfg.rotation,
        retention_days = cfg.retention_days,
        max_size_mb = cfg.max_size_mb,
        "file log sink initialised",
    );

    Some(FileSink {
        writer,
        guard,
        dir,
        prefix,
    })
}

/// Map our schema enum to the `tracing-appender` rotation constant.
pub(crate) fn rotation_for(kind: RotationKind) -> Rotation {
    match kind {
        RotationKind::Daily => Rotation::DAILY,
        RotationKind::Hourly => Rotation::HOURLY,
        RotationKind::Minutely => Rotation::MINUTELY,
        RotationKind::Never => Rotation::NEVER,
    }
}

/// Split a user-supplied `path` (e.g. `/data/logs/gateway.log`) into the
/// parent directory (`/data/logs`) and file-name prefix (`gateway.log`)
/// expected by `RollingFileAppender::new`.
///
/// Returns `None` when the path has no file-name component (e.g. a bare
/// directory such as `/data/logs/`), in which case the file sink is
/// skipped rather than written into an unpredictable location.
pub(crate) fn split_dir_and_prefix(path: &Path) -> Option<(PathBuf, String)> {
    // `Path::file_name` strips a trailing slash, so `/var/log/` would
    // otherwise decode as `log` and have us spraying files into `/var`.
    // Reject that shape explicitly: operators must name the file.
    let raw = path.to_str()?;
    if raw.ends_with('/') || raw.ends_with(std::path::MAIN_SEPARATOR) {
        return None;
    }
    let file_name = path.file_name()?.to_str()?.to_string();
    if file_name.is_empty() {
        return None;
    }
    let dir = path.parent().map(PathBuf::from).unwrap_or_else(|| {
        // `file_name` with no parent → current dir.
        PathBuf::from(".")
    });
    Some((dir, file_name))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_is_noop_without_endpoint() {
        // SAFETY: test-local env mutation, acceptable risk.
        unsafe {
            std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        }
        assert!(try_init_tracer().is_none());
    }

    #[test]
    fn empty_endpoint_is_noop() {
        unsafe {
            std::env::set_var("OTEL_EXPORTER_OTLP_ENDPOINT", "   ");
        }
        assert!(try_init_tracer().is_none());
        unsafe {
            std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        }
    }

    #[test]
    fn split_dir_and_prefix_extracts_components() {
        let (dir, prefix) =
            split_dir_and_prefix(Path::new("/var/log/gateway.log")).expect("valid path");
        assert_eq!(dir, PathBuf::from("/var/log"));
        assert_eq!(prefix, "gateway.log");
    }

    #[test]
    fn split_dir_and_prefix_rejects_pure_dir() {
        // Trailing slash means no file_name component.
        assert!(split_dir_and_prefix(Path::new("/var/log/")).is_none());
    }

    #[test]
    fn rotation_mapping_is_exhaustive() {
        // Just exercise the match — protection against a future enum
        // variant being added without a branch here.
        assert!(matches!(rotation_for(RotationKind::Daily), Rotation::DAILY));
        let _ = rotation_for(RotationKind::Hourly);
        let _ = rotation_for(RotationKind::Minutely);
        let _ = rotation_for(RotationKind::Never);
    }

    #[test]
    fn build_file_layer_empty_path_is_noop() {
        let cfg = FileLoggingConfig {
            path: PathBuf::new(),
            max_size_mb: 5,
            retention_days: 7,
            rotation: RotationKind::Daily,
        };
        assert!(build_file_layer(&cfg).is_none());
    }

    #[test]
    fn build_file_layer_creates_file_via_rolling_appender() {
        // Integration-style: drive the real `RollingFileAppender` through
        // our helper and prove it lands a file in the expected directory
        // with the expected prefix.
        use std::io::Write;

        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("gateway.log");
        let cfg = FileLoggingConfig {
            path: path.clone(),
            max_size_mb: 5,
            retention_days: 7,
            rotation: RotationKind::Daily,
        };

        let mut sink = build_file_layer(&cfg).expect("sink built");
        assert_eq!(sink.dir, dir.path());
        assert_eq!(sink.prefix, "gateway.log");

        // Push one line through the non-blocking writer so the appender
        // touches disk, then drop the guard so the worker flushes.
        writeln!(sink.writer, r#"{{"level":"INFO","msg":"hello"}}"#).unwrap();
        drop(sink.guard);

        // The daily appender writes `<prefix>.<YYYY-MM-DD>`; look for
        // any entry that starts with our prefix.
        let entries: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        assert!(
            entries.iter().any(|n| n.starts_with("gateway.log")),
            "expected a gateway.log.* file, got {entries:?}",
        );
    }
}
