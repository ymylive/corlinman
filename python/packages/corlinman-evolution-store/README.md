# corlinman-evolution-store

Python port of the Rust crate `corlinman-evolution`. Owns the
EvolutionLoop persistence layer: SQLite-backed async repos for
`evolution_signals`, `evolution_proposals`, `evolution_history`, and
`apply_intent_log`.

The SQLite schema is the cross-language contract — Rust observers and
the Python `EvolutionEngine` both bind to the same `evolution.sqlite`
file.
