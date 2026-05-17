# corlinman-shadow-tester (Python)

Python sibling of the Rust crate `corlinman-shadow-tester`.

The ShadowTester sits between the `EvolutionEngine` (writes `pending`
proposals) and the operator approval queue. For medium/high-risk
proposals it:

1. Loads matching eval cases from `<eval_set_dir>/<kind>/*.yaml`
   (per-kind subdirs).
2. Runs each case in an in-process sandbox against a tempdir copy of
   `kb.sqlite` — production state is never written.
3. Captures `shadow_metrics` (post-change) + `baseline_metrics_json`
   (pre-change) and an `eval_run_id` for traceability.
4. Transitions the row `pending -> shadow_running -> shadow_done`, so
   the admin UI can render a measured delta before the operator decides.

Low-risk kinds skip ShadowTester entirely and remain on the original
`pending -> approved` path.

## Layout

- `eval` — `EvalCase` / `EvalSet` types and YAML loader.
- `simulator` — `KindSimulator` protocol + per-kind impls
  (`MemoryOpSimulator`, `TagRebalanceSimulator`, `SkillUpdateSimulator`).
- `runner` — `ShadowRunner` orchestration.
- `sandbox` — execution sandbox abstraction
  (`InProcessBackend`, `DockerBackend`).
