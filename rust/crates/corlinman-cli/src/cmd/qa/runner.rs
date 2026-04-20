//! Dispatcher that maps [`Scenario`] → concrete handler and collects the
//! outcome. Handlers are split into one module per `kind` to keep each
//! handler's glue small.

use super::scenario::{Scenario, ScenarioKind};

mod chat_http;
mod plugin_exec;
mod rag_hybrid;

/// Outcome of running a single scenario. Stringly-typed reasons make for
/// obvious failure messages without needing a per-kind error enum.
#[derive(Debug, Clone)]
pub enum ScenarioOutcome {
    Pass,
    Skip { reason: String },
    Fail { reason: String },
}

impl ScenarioOutcome {
    /// Used by the suite summary.
    pub fn tag(&self) -> &'static str {
        match self {
            Self::Pass => "PASS",
            Self::Skip { .. } => "SKIP",
            Self::Fail { .. } => "FAIL",
        }
    }
}

/// Dispatch on scenario kind + `requires_live`. Handler errors become
/// `Fail { reason }`; anyhow errors propagate out as `Fail` too (never
/// panic — a broken scenario is a test failure, not a runner crash).
pub async fn run_scenario(sc: &Scenario, include_live: bool) -> ScenarioOutcome {
    if sc.requires_live && !include_live {
        return ScenarioOutcome::Skip {
            reason: "requires_live: true (pass --include-live to run)".into(),
        };
    }

    let res: anyhow::Result<()> = match sc.body.kind {
        ScenarioKind::ChatHttp => {
            let body = match sc.body.chat_http.as_ref() {
                Some(b) => b,
                None => {
                    return ScenarioOutcome::Fail {
                        reason: "kind=chat_http but `chat_http:` block is missing".into(),
                    };
                }
            };
            chat_http::run(body).await
        }
        ScenarioKind::PluginExecSync => {
            let body = match sc.body.plugin_exec.as_ref() {
                Some(b) => b,
                None => {
                    return ScenarioOutcome::Fail {
                        reason: "kind=plugin_exec_sync but `plugin_exec:` block is missing".into(),
                    };
                }
            };
            plugin_exec::run_sync(body).await
        }
        ScenarioKind::PluginExecAsync => {
            let body = match sc.body.plugin_exec.as_ref() {
                Some(b) => b,
                None => {
                    return ScenarioOutcome::Fail {
                        reason: "kind=plugin_exec_async but `plugin_exec:` block is missing".into(),
                    };
                }
            };
            plugin_exec::run_async(body).await
        }
        ScenarioKind::RagHybrid => {
            let body = match sc.body.rag_hybrid.as_ref() {
                Some(b) => b,
                None => {
                    return ScenarioOutcome::Fail {
                        reason: "kind=rag_hybrid but `rag_hybrid:` block is missing".into(),
                    };
                }
            };
            rag_hybrid::run(body).await
        }
        ScenarioKind::Live => {
            // Marked live and --include-live was passed (otherwise we'd have
            // short-circuited above). We still don't have a general-purpose
            // live executor, so this always reports a skip with the note.
            let note = sc
                .body
                .live
                .as_ref()
                .map(|l| l.note.clone())
                .unwrap_or_else(|| "live scenario placeholder".into());
            return ScenarioOutcome::Skip {
                reason: format!("live runner not implemented: {note}"),
            };
        }
    };

    match res {
        Ok(()) => ScenarioOutcome::Pass,
        Err(e) => ScenarioOutcome::Fail {
            reason: format!("{e:#}"),
        },
    }
}
