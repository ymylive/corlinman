# corlinman-goals

Goal hierarchies for the agent. Stores `short`/`mid`/`long`-tier goals
and per-window reflection scores in `agent_goals.sqlite`, sibling to
`agent_state.sqlite` (`corlinman-persona`) and `episodes.sqlite`
(`corlinman-episodes`).

The package owns:

- `store.py` — async SQLite store + schema migrations.
- `state.py` — `Goal` / `GoalEvaluation` dataclasses.
- (later iters) `placeholders.py`, `cli.py`, `reflection.py`,
  `evaluator.py`.

See `docs/design/phase4-w4-d2-design.md` for the full design.
