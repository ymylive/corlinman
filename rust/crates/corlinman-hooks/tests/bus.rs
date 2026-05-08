//! Integration tests for `HookBus`.
//!
//! Covers:
//!   (a) each event variant round-trips through the bus,
//!   (b) Critical subscribers observe an event before Normal/Low do,
//!   (c) flipping the cancel token stops further emits,
//!   (d) a dropped subscriber does not break emits for the others.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use corlinman_hooks::{HookBus, HookError, HookEvent, HookPriority};
use serde_json::json;

fn all_event_samples() -> Vec<HookEvent> {
    vec![
        HookEvent::MessageReceived {
            channel: "qq".into(),
            session_key: "s1".into(),
            content: "hi".into(),
            metadata: json!({"from": "u1"}),
            user_id: None,
        },
        HookEvent::MessageSent {
            channel: "qq".into(),
            session_key: "s1".into(),
            content: "hello".into(),
            success: true,
            user_id: None,
        },
        HookEvent::MessageTranscribed {
            session_key: "s1".into(),
            transcript: "spoken text".into(),
            media_path: "/tmp/a.ogg".into(),
            media_type: "audio/ogg".into(),
            user_id: None,
        },
        HookEvent::MessagePreprocessed {
            session_key: "s1".into(),
            transcript: "cleaned".into(),
            is_group: true,
            group_id: Some("g42".into()),
            user_id: None,
        },
        HookEvent::SessionPatch {
            session_key: "s1".into(),
            patch: json!({"foo": "bar"}),
            user_id: None,
        },
        HookEvent::AgentBootstrap {
            workspace_dir: "/ws".into(),
            session_key: "s1".into(),
            files: vec!["a.md".into(), "b.md".into()],
        },
        HookEvent::GatewayStartup {
            version: "0.1.0".into(),
        },
        HookEvent::ConfigChanged {
            section: "channels.qq".into(),
            old: json!({"enabled": false}),
            new: json!({"enabled": true}),
        },
        HookEvent::ApprovalRequested {
            id: "a1".into(),
            session_key: "s1".into(),
            plugin: "shell".into(),
            tool: "exec".into(),
            args_preview: "{}".into(),
            timeout_at_ms: 0,
            user_id: None,
        },
        HookEvent::ApprovalDecided {
            id: "a1".into(),
            decision: "allow".into(),
            decider: Some("root".into()),
            decided_at_ms: 0,
            tenant_id: None,
            user_id: None,
        },
        HookEvent::RateLimitTriggered {
            session_key: "s1".into(),
            limit_type: "channel_qq".into(),
            retry_after_ms: 0,
            user_id: None,
        },
        HookEvent::Telemetry {
            node_id: "ios-demo".into(),
            metric: "battery.level".into(),
            value: 0.87,
            tags: {
                let mut t = BTreeMap::new();
                t.insert("build".into(), "dev".into());
                t
            },
        },
        HookEvent::EngineRunCompleted {
            run_id: "r1".into(),
            proposals_generated: 3,
            duration_ms: 420,
        },
        HookEvent::EngineRunFailed {
            run_id: "r2".into(),
            error_kind: "timeout".into(),
            exit_code: None,
        },
    ]
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn each_event_round_trips() {
    let bus = HookBus::new(64);
    let mut sub = bus.subscribe(HookPriority::Normal);

    for ev in all_event_samples() {
        bus.emit(ev.clone()).await.expect("emit ok");
        let got = sub.recv().await.expect("recv ok");
        assert_eq!(got.kind(), ev.kind(), "kind mismatch for {:?}", ev.kind());
    }
}

/// Critical subscribers must observe events strictly before Normal ones.
/// We record the order of arrivals in a shared Vec; the assertion is that
/// every Critical entry for a given event index precedes the Normal one.
///
/// Runs on the single-threaded runtime on purpose: the bus's ordering
/// guarantee is cooperative (yield between tiers lets pending receivers
/// drain before the next tier is published). On a multi-thread runtime,
/// `yield_now` doesn't force a specific peer task to run before we
/// continue, which makes cross-tier arrival order a best-effort property.
#[tokio::test(flavor = "current_thread")]
async fn critical_observes_before_normal_and_low() {
    let bus = HookBus::new(64);
    let log: Arc<Mutex<Vec<(&'static str, usize)>>> = Arc::new(Mutex::new(Vec::new()));

    let mut critical = bus.subscribe(HookPriority::Critical);
    let mut normal = bus.subscribe(HookPriority::Normal);
    let mut low = bus.subscribe(HookPriority::Low);

    let log_c = Arc::clone(&log);
    let crit_task = tokio::spawn(async move {
        for i in 0..5 {
            let _ = critical.recv().await.unwrap();
            log_c.lock().unwrap().push(("critical", i));
        }
    });
    let log_n = Arc::clone(&log);
    let norm_task = tokio::spawn(async move {
        for i in 0..5 {
            let _ = normal.recv().await.unwrap();
            log_n.lock().unwrap().push(("normal", i));
        }
    });
    let log_l = Arc::clone(&log);
    let low_task = tokio::spawn(async move {
        for i in 0..5 {
            let _ = low.recv().await.unwrap();
            log_l.lock().unwrap().push(("low", i));
        }
    });

    for i in 0..5 {
        bus.emit(HookEvent::GatewayStartup {
            version: format!("v{i}"),
        })
        .await
        .unwrap();
    }

    crit_task.await.unwrap();
    norm_task.await.unwrap();
    low_task.await.unwrap();

    let entries = log.lock().unwrap().clone();
    // For every event index i, the Critical entry must appear before the
    // Normal entry, and Normal before Low.
    for i in 0..5 {
        let crit_pos = entries.iter().position(|e| *e == ("critical", i)).unwrap();
        let norm_pos = entries.iter().position(|e| *e == ("normal", i)).unwrap();
        let low_pos = entries.iter().position(|e| *e == ("low", i)).unwrap();
        assert!(
            crit_pos < norm_pos,
            "event {i}: critical ({crit_pos}) should precede normal ({norm_pos}): {entries:?}"
        );
        assert!(
            norm_pos < low_pos,
            "event {i}: normal ({norm_pos}) should precede low ({low_pos}): {entries:?}"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cancel_propagates_and_stops_downstream_emits() {
    let bus = HookBus::new(64);
    let mut sub = bus.subscribe(HookPriority::Normal);
    let cancel = bus.cancel_token();

    bus.emit(HookEvent::GatewayStartup {
        version: "pre".into(),
    })
    .await
    .unwrap();
    let got = sub.recv().await.unwrap();
    assert_eq!(got.kind(), "gateway_startup");

    cancel.cancel();

    let res = bus
        .emit(HookEvent::GatewayStartup {
            version: "post".into(),
        })
        .await;
    assert!(matches!(res, Err(HookError::Cancelled)), "got {res:?}");

    // Subscriber should not receive the second event. Use a short timeout
    // because `recv` would otherwise hang waiting for the next emit.
    let timeout = tokio::time::timeout(Duration::from_millis(50), sub.recv()).await;
    assert!(
        timeout.is_err(),
        "subscriber unexpectedly received an event after cancel: {timeout:?}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn approval_requested_round_trips_and_exposes_session_key() {
    let bus = HookBus::new(16);
    let mut sub = bus.subscribe(HookPriority::Normal);

    let ev = HookEvent::ApprovalRequested {
        id: "req-1".into(),
        session_key: "qq:group:123:u42".into(),
        plugin: "shell".into(),
        tool: "exec".into(),
        args_preview: "{\"cmd\":\"ls\"}".into(),
        timeout_at_ms: 1_700_000_000_000,
        user_id: None,
    };
    bus.emit(ev.clone()).await.expect("emit ok");
    let got = sub.recv().await.expect("recv ok");
    assert_eq!(got.kind(), "approval_requested");
    assert_eq!(got.session_key(), Some("qq:group:123:u42"));

    // Serde round-trip so the admin UI / python bridge wire contract is pinned.
    let json = serde_json::to_string(&ev).unwrap();
    assert!(json.contains("\"kind\":\"ApprovalRequested\""));
    let back: HookEvent = serde_json::from_str(&json).unwrap();
    assert_eq!(back.kind(), "approval_requested");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn approval_decided_round_trips_and_is_session_scoped_none() {
    let bus = HookBus::new(16);
    let mut sub = bus.subscribe(HookPriority::Normal);

    let ev = HookEvent::ApprovalDecided {
        id: "req-1".into(),
        decision: "allow".into(),
        decider: Some("admin".into()),
        decided_at_ms: 1_700_000_000_500,
        tenant_id: None,
        user_id: None,
    };
    bus.emit(ev.clone()).await.expect("emit ok");
    let got = sub.recv().await.expect("recv ok");
    assert_eq!(got.kind(), "approval_decided");
    // Decisions are not session-scoped on the bus (the `id` links back).
    assert_eq!(got.session_key(), None);

    let json = serde_json::to_string(&ev).unwrap();
    let back: HookEvent = serde_json::from_str(&json).unwrap();
    match back {
        HookEvent::ApprovalDecided {
            decision, decider, ..
        } => {
            assert_eq!(decision, "allow");
            assert_eq!(decider.as_deref(), Some("admin"));
        }
        other => panic!("expected ApprovalDecided, got {other:?}"),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn rate_limit_triggered_round_trips() {
    let bus = HookBus::new(16);
    let mut sub = bus.subscribe(HookPriority::Normal);

    let ev = HookEvent::RateLimitTriggered {
        session_key: "qq:group:999:u7".into(),
        limit_type: "channel_qq".into(),
        retry_after_ms: 500,
        user_id: None,
    };
    bus.emit(ev.clone()).await.expect("emit ok");
    let got = sub.recv().await.expect("recv ok");
    assert_eq!(got.kind(), "rate_limit_triggered");
    assert_eq!(got.session_key(), Some("qq:group:999:u7"));

    let json = serde_json::to_string(&ev).unwrap();
    let back: HookEvent = serde_json::from_str(&json).unwrap();
    assert_eq!(back.kind(), "rate_limit_triggered");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn telemetry_round_trips_with_stable_tag_order() {
    let bus = HookBus::new(16);
    let mut sub = bus.subscribe(HookPriority::Normal);

    let mut tags = BTreeMap::new();
    tags.insert("region".into(), "cn".into());
    tags.insert("build".into(), "dev".into());
    let ev = HookEvent::Telemetry {
        node_id: "ios-demo".into(),
        metric: "battery.level".into(),
        value: 0.42,
        tags,
    };
    bus.emit(ev.clone()).await.expect("emit ok");
    let got = sub.recv().await.expect("recv ok");
    assert_eq!(got.kind(), "telemetry");
    assert_eq!(got.session_key(), None);

    // BTreeMap → JSON key order must be sorted (build before region).
    let json = serde_json::to_string(&ev).unwrap();
    let build_at = json.find("build").expect("build tag in json");
    let region_at = json.find("region").expect("region tag in json");
    assert!(
        build_at < region_at,
        "telemetry tags must serialize in lexicographic key order: {json}"
    );
    let back: HookEvent = serde_json::from_str(&json).unwrap();
    assert_eq!(back.kind(), "telemetry");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn dropped_subscriber_does_not_break_emits() {
    let bus = HookBus::new(64);
    let mut kept = bus.subscribe(HookPriority::Normal);
    {
        let _doomed = bus.subscribe(HookPriority::Normal);
        // `_doomed` drops here.
    }

    for i in 0..3 {
        bus.emit(HookEvent::GatewayStartup {
            version: format!("v{i}"),
        })
        .await
        .expect("emit should succeed even after a subscriber dropped");
    }

    for _ in 0..3 {
        let ev = kept.recv().await.expect("kept subscriber still receives");
        assert_eq!(ev.kind(), "gateway_startup");
    }
}
