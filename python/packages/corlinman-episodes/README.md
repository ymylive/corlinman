# corlinman-episodes

Phase 4 W4 D1 — **episodic memory**. A frozen, summarised, embedded
narrative layer above raw session messages and below evolution history.

Per-tenant `<data_dir>/tenants/<slug>/episodes.sqlite` carries one row
per episode (a session-range or signal-driven slice of activity);
`{{episodes.*}}` placeholders surface them back into prompts.

See `docs/design/phase4-w4-d1-design.md` for the full schema, the
distillation job shape, the importance rubric, and the placeholder
contract. **D2 (`{{goals.*}}`) blocks on D1.**

This package owns the Python plane:

- `config.py` — `EpisodesConfig` dataclass mirroring `[episodes]` TOML.
- `store.py` — SQLite open + schema + idempotency CRUD.
- `sources.py` — multi-stream join (sessions, signals, history, hooks).
- `distiller.py` — LLM call → `summary_text` (PII-redacted both ways).
- `classifier.py` / `importance.py` — pure functions over a bundle.
- `embed.py` — second-pass embedding writer via `corlinman-embedding`.
- `runner.py` / `cli.py` — `episodes_run_once` end-to-end + CLI.

The Rust gateway side (placeholder resolver, admin route, UI button)
ships under D1 iters 8-10.
