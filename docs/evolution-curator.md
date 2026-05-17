# Evolution & curator

corlinman runs a **self-evolution loop** ported from
[hermes-agent][hermes]. The idea: the agent gets better at serving
*you* — not in some abstract benchmark sense, but in the specific,
personal sense of remembering what you asked, dropping skills it never
uses, and patching skills you've corrected in chat.

The mental model is one sentence:

> *You say "stop X" → the next session starts already knowing.*

This page is the operator's guide: what the three subsystems do, how
they're glued together, how to drive them from the UI, what the safety
boundaries are, and how it compares to the source pattern in
hermes-agent.

For the implementation plan that produced this surface, see
[`PLAN_EASY_SETUP.md`](PLAN_EASY_SETUP.md) §1.2 + Wave 4.

---

## 1. The three subsystems

```text
┌──────────────────────────────────────────────────────────────────┐
│                      Curator loop  (deterministic)               │
│  active ──30d idle──▶ stale ──90d idle──▶ archived               │
│           ▲                                                      │
│           └──── reactivate-on-use ────                           │
│  Only `origin = agent-created` skills are managed.               │
│  `pinned = true` skills are permanently protected.               │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│             Background review fork  (LLM-driven)                 │
│  Spawn isolated mini-agent with restricted toolset:              │
│    skill_manage + memory_write   (NO terminal, NO net, NO file)  │
│  Inherits parent's provider/model. Writes inside profile_root.   │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│           User-correction routing  (heuristic)                   │
│  Regex over user chat messages → EVENT_USER_CORRECTION signal    │
│  → routed to background fork → patches the implicated SKILL.md.  │
└──────────────────────────────────────────────────────────────────┘
```

The three subsystems are intentionally separable. You can run the
curator loop alone (deterministic, no LLM cost) and disable the
background fork. You can disable user-correction detection without
touching the rest. They share a data layout but not control flow.

### 1.1 Curator loop (deterministic)

A pure lifecycle pass over the profile's `skills/` directory. No LLM
involved — just timestamps and counts.

| Transition          | Trigger                                     |
| ------------------- | ------------------------------------------- |
| `active → stale`    | `now - last_used_at > stale_after_days` (default 30) |
| `stale → archived`  | `now - last_used_at > archive_after_days` (default 90) |
| `*  → active`       | `last_used_at` advances (the skill got used again) |

Only skills with `origin = "agent-created"` are eligible. Hand-written
skills (`origin = "user-requested"`) and shipped skills (`origin =
"bundled"`) are out of scope, even if you forget about them. The
`pinned = true` flag protects any skill permanently — set it from the
UI on something you want kept around.

The loop is driven by `maybe_run_curator(profile_slug, ...)`. It
short-circuits when `now - curator_state.last_review_at <
interval_hours` (default 168h = 7 days), unless the caller passes
`force=True` (the UI's **Run now** button does).

Implementation: [`gateway/evolution/curator.py`][curator-py].

### 1.2 Background review fork (LLM-driven)

When a signal arrives that warrants a model-driven response (an idle
reflection tick, an unused-skill detection, a user correction), the
applier spawns a **background review** — a one-shot LLM call with a
strict tool schema.

The fork's tool whitelist is hard-coded:

```python
WHITELISTED_TOOLS = frozenset({"skill_manage", "memory_write"})
```

- `skill_manage(action, name, ...)` — `create`, `edit`, `patch`,
  `delete` over the profile's `skills/` directory. The dispatcher
  validates `name` against a strict regex and joins it against
  `profile_root` so a malicious tool call can never escape the
  profile directory.
- `memory_write(target, action, content)` — `append` or `replace` on
  `MEMORY.md` or `USER.md`.

**Anything else** in the LLM's tool output is dropped with
`skipped_reason="not_whitelisted"`. The fork has no terminal access,
no network access, no general file write — by construction.

The fork inherits the parent's provider + model so it costs whatever
your normal chat costs. It runs with a narrow prompt template tailored
to the trigger (see `evolution/prompts/{memory,skill,combined,curator,user_preference_patch}.md`).

Implementation: [`gateway/evolution/background_review.py`][bg-py].

### 1.3 User-correction routing (heuristic)

The detector subscribes to user messages and runs a small set of regex
patterns ranked by specificity:

| Kind                | Example matches                                  | Weight |
| ------------------- | ------------------------------------------------ | ------ |
| `rejection`         | "no, I said …", "that's not what I asked"        | 0.90   |
| `rejection`         | "I already said …", "I told you …"               | 0.85   |
| `imperative`        | "stop", "don't", "cut it out"                    | 0.85   |
| `pattern_critique`  | "you always", "you keep", "you never"            | 0.80   |
| `negative_reaction` | "I hate when …", "please don't", "annoying"      | 0.75   |
| `reformulation`     | "actually,", "wait,", "no wait"                  | 0.60   |

A match writes an `EvolutionSignal` row with
`event_kind = "user.correction"` and the payload
`{text, session_id, matched_pattern, weight}`. The detector is
sub-millisecond, deterministic, and explainable — operators can grep
`matched_pattern` to see *why* the signal fired.

Downstream, `UserCorrectionApplier.apply(signal)` decides whether to
spawn a background fork. It drops the signal when:

- The payload `weight` is below `min_weight` (default 0.7 — the
  weakest "reformulation" pattern is suppressed by default).
- The per-session rate-limit window hasn't elapsed (default 30s per
  `(profile, session_id)`).
- The signal can't be resolved to a profile/registry/provider.

Implementation:
[`gateway/evolution/signals/user_correction.py`][det-py] +
[`gateway/evolution/applier_user_correction.py`][app-py].

---

## 2. Data layout

### SKILL.md frontmatter

Each skill is a markdown file with YAML frontmatter:

```markdown
---
name: weekly-changelog
version: 1.2.0
state: active            # active | stale | archived
origin: agent-created    # bundled | user-requested | agent-created
pinned: false
created_at: 2026-04-01T10:23:14Z
---

# Weekly changelog

The agent assembles a weekly changelog from the user's git activity…
```

The state machine writes the `state` field on transitions. The
`origin` is set at creation time and never changes. `pinned` is the
operator's escape hatch — see [§4 Operating from the UI](#4-operating-from-the-ui).

### `.usage.json` sidecar

Each skill has a sidecar `*.usage.json` alongside its `SKILL.md`:

```json
{
  "use_count": 14,
  "patch_count": 3,
  "last_used_at": "2026-05-11T09:14:02Z"
}
```

The sidecar is updated atomically every time the skill is invoked.
The curator reads `last_used_at` to drive the lifecycle transitions
above.

### `curator_state` table

Per-profile row in the evolution-store SQLite:

| Column                    | Default | Notes                                       |
| ------------------------- | ------- | ------------------------------------------- |
| `profile_slug`            | —       | PK                                          |
| `paused`                  | `false` | When `true`, `maybe_run_curator` is a no-op |
| `interval_hours`          | `168`   | 7-day default cadence                       |
| `stale_after_days`        | `30`    | active → stale threshold                    |
| `archive_after_days`      | `90`    | stale → archived threshold (must > stale)   |
| `last_review_at`          | —       | Bumped on every real run                    |
| `last_review_duration_ms` | —       | Diagnostic                                  |
| `last_review_summary`     | —       | Human-readable one-liner from last run      |
| `run_count`               | `0`     | Lifetime counter                            |

The thresholds are per-profile — a research bot can be more aggressive
(`stale_after_days = 14`) than a long-term coding assistant
(`stale_after_days = 60`).

---

## 3. Signals

Five typed event kinds flow through the `EvolutionSignal` stream:

| Constant                       | String value             | Source                              |
| ------------------------------ | ------------------------ | ----------------------------------- |
| `EVENT_USER_CORRECTION`        | `user.correction`        | User-correction detector            |
| `EVENT_IDLE_REFLECTION`        | `idle.reflection`        | Scheduled idle trigger              |
| `EVENT_SKILL_UNUSED`           | `skill.unused`           | Curator (skill crossed `stale_after_days`) |
| `EVENT_CURATOR_RUN_COMPLETED`  | `curator.run.completed`  | Curator pass succeeded              |
| `EVENT_CURATOR_RUN_FAILED`     | `curator.run.failed`     | Curator pass raised                 |

Signals are persisted into `evolution.sqlite` by
`corlinman_evolution_store.SignalsRepo`. The legacy scheduler pipeline
(documented in [`evolution-loop.md`](evolution-loop.md)) writes them
too — so the same `/admin/evolution/signals` view surfaces both
deterministic curator runs and LLM-driven background reviews. Filter
by `event_kind` to slice:

```bash
curl 'http://localhost:6005/admin/evolution/signals?event_kind=curator.run.failed' \
  -H "Cookie: corlinman_session=$SESSION_COOKIE"
```

> The signals stream is the single source of truth for "what did the
> curator do, and when". Always read it before assuming a bug — every
> success and every failure carries a row.

---

## 4. Operating from the UI

The page is `/(admin)/evolution`. It has three panes.

### 4.1 Profiles pane

One row per profile, showing:

- The lifecycle histogram (active / stale / archived counts).
- The origin histogram (bundled / user-requested / agent-created).
- Last-run timestamp and run count.
- Pause toggle and threshold editors.

![Curator profiles pane](assets/evolution-profiles.png "TODO: screenshot")
<!-- TODO: screenshot of /(admin)/evolution profiles pane -->

### 4.2 Run / preview pane

Two buttons per profile:

- **Preview (dry run)** — runs `maybe_run_curator(..., dry_run=True,
  force=True)`. Returns the would-be transitions without writing
  back to SKILL.md or bumping `last_review_at`. Use this to see what
  the next real run *would* do.
- **Run now** — runs `maybe_run_curator(..., dry_run=False,
  force=True)`. Persists every transition, emits the corresponding
  signals, bumps `last_review_at`.

The preview / run response is the same `CuratorReportOut` envelope —
the only difference is whether the side effects landed.

### 4.3 Skills pane

A filterable list of every skill in the active profile, with badges
for `state` (active / stale / archived) and `origin` (bundled /
user-requested / agent-created). Each row carries:

- The skill name and description.
- The version (semver from frontmatter).
- A pin toggle. Clicking writes `pinned` back to the SKILL.md
  frontmatter so the next registry load picks it up — without
  the writeback the pin would silently revert on every gateway
  restart.

Filters: `state`, `origin`, and a substring search across name +
description.

---

## 5. Safety guarantees

The whole subsystem is built around the assumption that the
background fork might do something unwise. The boundaries are:

1. **Path-traversal defence.** Every `skill_manage` call validates
   the `name` argument against a strict regex (`^[a-z0-9][a-z0-9_-]*$`)
   *before* joining it against `profile_root`. A skill named
   `../../../etc/passwd` is rejected at the dispatcher; the fork
   cannot write outside the profile's `skills/` directory.
2. **Tool whitelist.** `WHITELISTED_TOOLS = {"skill_manage",
   "memory_write"}`. Anything else from the LLM's tool output is
   dropped with `skipped_reason="not_whitelisted"`. The fork has no
   terminal, no network, no general file write.
3. **`spawn_background_review` never raises.** The function is
   exception-safe by contract. On provider failure / timeout /
   serialisation error it returns a `BackgroundReviewReport` with
   `error` populated. The chat hot path is therefore never blocked
   or degraded by a curator failure.
4. **Rate limiting.** The user-correction applier holds an in-memory
   `(profile, session) → last_fire_at` map with a 30-second window
   by default. A chatty user spamming "stop" three times in five
   seconds collapses to a single review fork.
5. **Origin gate.** Only `origin = "agent-created"` skills enter
   curator scope. Your hand-written skills (`origin =
   "user-requested"`) and the bundled defaults (`origin =
   "bundled"`) are immune to lifecycle transitions and to
   `skill_manage(action="delete")` invocations.
6. **Pin gate.** Any skill with `pinned = true` is filtered out
   before the curator's pass and rejected by the background fork's
   skill-delete dispatcher.

The combination means a misbehaving model, a hallucinated tool
call, or a malformed prompt can at worst write an empty SKILL.md
into one profile's `skills/` directory — and even that can be undone
by deleting the file.

---

## 6. API surface

All routes mount behind admin auth. Base URL: `http://localhost:6005`.

### Per-profile curator state

| Method | Path                                          | Body / params                                          | Response             |
| ------ | --------------------------------------------- | ------------------------------------------------------ | -------------------- |
| GET    | `/admin/curator/profiles`                     | —                                                      | `CuratorProfilesResponse` |
| POST   | `/admin/curator/{slug}/preview`               | —                                                      | `CuratorReportOut`   |
| POST   | `/admin/curator/{slug}/run`                   | —                                                      | `CuratorReportOut`   |
| POST   | `/admin/curator/{slug}/pause`                 | `{paused: true \| false}`                              | `CuratorStateOut`    |
| PATCH  | `/admin/curator/{slug}/thresholds`            | `{interval_hours?, stale_after_days?, archive_after_days?}` | `CuratorStateOut` |

### Per-profile skills

| Method | Path                                          | Body / params                                                                 | Response               |
| ------ | --------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------- |
| GET    | `/admin/curator/{slug}/skills`                | `?state=active\|stale\|archived`, `?origin=...`, `?search=...` (all optional) | `SkillsListResponse`   |
| POST   | `/admin/curator/{slug}/skills/{name}/pin`     | `{pinned: true \| false}`                                                     | `SkillSummaryOut`      |

### Signals

| Method | Path                          | Body / params                                  | Response                  |
| ------ | ----------------------------- | ---------------------------------------------- | ------------------------- |
| GET    | `/admin/evolution/signals`    | `?event_kind=user.correction\|...` (optional)  | `[EvolutionSignalOut, ...]` |

(The signals surface predates this wave — see
[`evolution-loop.md`](evolution-loop.md) for the scheduled-engine
context.)

### Common error codes

| Code                          | When                                       |
| ----------------------------- | ------------------------------------------ |
| `profile_not_found`           | Slug doesn't resolve in the profile store (404) |
| `profile_store_missing`       | Gateway not fully booted (503; retry)      |
| `curator_state_repo_missing`  | evolution-store not wired (503)            |
| `skill_registry_factory_missing` | skill registry not wired (503)          |
| `curator_module_missing`      | `corlinman_server.gateway.evolution` import failed (503) |
| `evolution_store_missing`     | `corlinman_evolution_store` import failed (503) |
| `curator_paused`              | `/run` called on a paused profile (409)    |
| `invalid_thresholds`          | `archive_after_days <= stale_after_days` (422) |
| `skill_not_found`             | Skill name doesn't resolve in the registry (404) |
| `skill_write_failed`          | Filesystem write failed (500)              |
| `registry_load_failed`        | Skills directory unreadable (500)          |

---

## 7. Comparison to hermes-agent

corlinman's port is faithful in spirit, different in shape. A few
deliberate divergences:

| Concern              | hermes-agent                                | corlinman                                   |
| -------------------- | ------------------------------------------- | ------------------------------------------- |
| Curator state store  | `.curator_state` JSON file in profile dir   | `curator_state` SQLite row (one per profile) |
| Why                  | Single-user, single-process — file is fine. | Multi-profile querying — `SELECT * WHERE last_review_at < ...` beats glob + parse. |
| Fork mechanism       | `spawn_background_review` → AIAgent fork    | One-shot LLM call with strict tool schema   |
| Why                  | Hermes is the agent runtime itself.        | corlinman calls into provider SDKs; no nested AIAgent process to fork. |
| User-correction      | Implicit — picked up inside background review prompt | First-class signal with detection heuristic |
| Why                  | Hermes pipes every user turn through the review prompt. | corlinman runs the detector synchronously; only matches fire a fork. Saves tokens, makes "did it match?" greppable. |

The bones are the same — curator loop, background review fork,
deterministic lifecycle, pinned/origin gates. The seams between them
are tighter because corlinman is a long-running service rather than a
CLI session.

---

## 8. Future work

The current port is the minimum viable shape. Known follow-ups:

- **Small-model intent classifier** to replace the regex detector in
  §1.3. The regex catches the obvious 80% and misses the rest;
  a 1B-parameter classifier would lift recall without giving up
  determinism.
- **Cross-profile skill sharing** via an ACP-style local registry.
  Today every profile owns its skills outright; future work would
  let profile B reference a skill in profile A's library with a
  symlink-style binding that the curator still respects.
- **Curator dry-run scheduling**. Today **Preview** is a manual
  click; a scheduled dry-run that emits a `curator.run.preview`
  signal would let operators see drift over time without
  committing.

If you want to drive any of these, open an issue or grab the relevant
file from §6 and the surrounding tests in
`python/packages/corlinman-server/tests/gateway/evolution/`.

---

## See also

- [Quickstart](quickstart.md) — boot + first login
- [Profiles](profiles.md) — the per-profile isolation the curator runs against
- [Evolution loop (scheduled engine)](evolution-loop.md) — the legacy daily run that emits signals
- [`gateway/evolution/`][evo-dir] — the package the three subsystems live in
- [`PLAN_EASY_SETUP.md`](PLAN_EASY_SETUP.md) §1.2 + Wave 4 — design rationale

[curator-py]: ../python/packages/corlinman-server/src/corlinman_server/gateway/evolution/curator.py
[bg-py]: ../python/packages/corlinman-server/src/corlinman_server/gateway/evolution/background_review.py
[det-py]: ../python/packages/corlinman-server/src/corlinman_server/gateway/evolution/signals/user_correction.py
[app-py]: ../python/packages/corlinman-server/src/corlinman_server/gateway/evolution/applier_user_correction.py
[evo-dir]: ../python/packages/corlinman-server/src/corlinman_server/gateway/evolution/
[hermes]: https://github.com/yamamoto-toru/hermes-agent
