# corlinman-evolution-engine

Phase 2 of the auto-evolution subsystem. See
`docs/design/auto-evolution.md` for the full architecture.

This package is the Python side of the loop:

1. Read recent rows from `evolution_signals` (written by the Rust gateway
   observer).
2. Group signals by `(event_kind, target)`.
3. For clusters that meet `min_cluster_size`, scan `kb.sqlite` for
   near-duplicate chunks and emit `memory_op` proposals.
4. Insert the proposals into `evolution_proposals` with `status = pending`.

Phase 2 is intentionally narrow:

- Only the `memory_op` kind. Other kinds (`skill_update`, `prompt_template`,
  ...) are Phase 3+.
- No LLM calls. Near-duplicate detection uses Jaccard token overlap; no
  embeddings.
- No `ShadowTester`, no `AutoRollback`. `memory_op` is low-risk and
  auto-approved downstream.
- No scheduler integration. Run via `corlinman-evolution-engine run-once`.

## CLI

```
corlinman-evolution-engine run-once \
    --evolution-db /data/evolution.sqlite \
    --kb-db        /data/kb.sqlite \
    --lookback-days 7 \
    --min-cluster-size 3
```

## Public API

```python
from corlinman_evolution_engine import EngineConfig, EvolutionEngine, RunSummary
```
