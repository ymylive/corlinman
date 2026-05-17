# corlinman-auto-rollback

Python port of the Rust crates ``corlinman-auto-rollback`` (library) and
``corlinman-auto-rollback-cli`` (binary), merged into one package.

AutoRollback watches recently-applied ``EvolutionProposal`` rows in the
grace window, computes a metrics delta against the apply-time baseline
snapshot stored in ``evolution_history.metrics_baseline``, and triggers
a revert through an injected ``Applier`` whenever the delta breaches
the configured threshold.

## Public API

- ``AutoRollbackMonitor`` — orchestrates one ``run_once`` pass.
- ``RunSummary`` — per-run counters (inspected / breached / triggered /
  succeeded / failed / errors).
- ``EvolutionAutoRollbackConfig`` + ``AutoRollbackThresholds`` — config
  shape mirrored from ``corlinman-core``'s Rust struct.
- ``Applier`` / ``RevertError`` — thin protocol the monitor calls into.
- ``MetricSnapshot`` / ``MetricDelta`` / ``KindDelta`` /
  ``capture_snapshot`` / ``compute_delta`` / ``breaches_threshold`` /
  ``watched_event_kinds`` — metrics primitives.

## CLI

```
corlinman-auto-rollback run-once --config <path-to-corlinman.toml>
```

The CLI deliberately mirrors the Rust binary's shape. Wiring it to a
concrete ``Applier`` (the gateway's ``EvolutionApplier`` in Rust) is the
caller's responsibility — see TODOs.
