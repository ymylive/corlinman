# Profiles

A **profile** is an isolated agent instance — its own persona, its own
memory, its own skill library, its own per-session SQLite. One gateway,
many agents, zero cross-contamination.

The concept is lifted directly from [hermes-agent][hermes]: one
engineer, many AI workspaces (a research bot, a coding pair, a "weekend
projects" persona) on the same machine. Or one team, one corlinman
deployment, separate personas per teammate.

Profiles do **not** replace the existing `agents/*.yaml` registry — see
[Compatibility](#compatibility-with-agentsyaml) at the bottom.

---

## Mental model

```text
~/.corlinman/profiles/
├── default/                     ← protected, auto-created on first boot
│   ├── SOUL.md                  ← persona (markdown)
│   ├── MEMORY.md                ← agent-distilled memory (~2k chars)
│   ├── USER.md                  ← user facts the agent has learned (~1k chars)
│   ├── state.db                 ← per-profile session/conversation SQLite
│   └── skills/                  ← per-profile SKILL.md library
│       ├── code-review/
│       └── changelog-writer/
├── research-bot/                ← cloned from default
│   ├── SOUL.md
│   ├── MEMORY.md
│   ├── USER.md
│   ├── state.db
│   └── skills/
└── index.sqlite                 ← registry: slug → display_name + parent + timestamps
```

Each profile is a self-contained directory; the registry table at
`profiles/index.sqlite` is the only index. To delete a profile from
disk, drop its row + remove the directory — nothing else points at it.

---

## Anatomy of a profile

| File / dir   | What lives there                                                       |
| ------------ | ---------------------------------------------------------------------- |
| `SOUL.md`    | The persona prompt. Markdown, hot-editable from `/(admin)/profiles`.   |
| `MEMORY.md`  | Distilled agent memory. ~2k char budget — updated by the curator.     |
| `USER.md`    | Facts the agent has learned about *you*. ~1k char budget.              |
| `state.db`   | Per-profile SQLite holding session history and the curator state row.  |
| `skills/`    | One subdirectory per skill, each with a `SKILL.md` + optional sidecars. |

The placeholder markdown files exist for every profile (even empty)
so the SOUL editor can always open a file. See
[`profiles/paths.py`][paths] for the canonical layout.

---

## Slug rules

A profile slug is the directory name **and** the URL path component:

```text
^[a-z0-9][a-z0-9_-]{0,63}$
```

In plain English:

- Lowercase only — no case ambiguity on case-sensitive filesystems.
- Alphanumeric plus `-` and `_` — safe on NTFS / APFS / ext4.
- First character is alphanumeric — avoids `-` being mistaken for a CLI flag.
- 1 to 64 characters — short enough for paths, long enough to be descriptive.

The regex lives in [`profiles/paths.py`][paths] (`SLUG_REGEX`). The
slug `default` is reserved for the bootstrap profile and is rejected
with `409 profile_protected` on delete.

---

## Creating a profile

### Via the UI (recommended)

1. Sign in, then visit `/(admin)/profiles`.
2. Click **Create profile**.
3. Type a slug (the regex hint shows live on the field).
4. Pick a parent in the **Clone from** dropdown (typically `default`)
   or leave it blank for an empty profile.
5. Save.

The UI calls `POST /admin/profiles` underneath; the slug regex is
validated client-side **and** server-side.

![Create profile modal](assets/profiles-create.png "TODO: screenshot")
<!-- TODO: screenshot of /(admin)/profiles create modal -->

### Via the API

```bash
curl -X POST http://localhost:6005/admin/profiles \
  -H "Cookie: corlinman_session=$SESSION_COOKIE" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "research-bot",
    "display_name": "Research bot",
    "clone_from": "default",
    "description": "Reads papers + summarises them into MEMORY.md"
  }'
```

Returns `201` with the new profile row. The directory tree
(`SOUL.md`, `MEMORY.md`, `USER.md`, `state.db`, `skills/`) is
materialised before the response returns.

---

## Cloning

When `clone_from` is set, the store copies the parent's:

- `SOUL.md`, `MEMORY.md`, `USER.md` — top-level markdown files.
- `skills/` — recursive copy (every subdirectory, every SKILL.md).

**Not** copied:

- `state.db` — per-profile session history. Cloning a chat history
  across personas would conflate two distinct identities.
- The registry row itself — the child gets its own
  `parent_slug` pointer so you can see the lineage in
  `GET /admin/profiles`.

The copy is shallow-friendly: SKILL.md sidecars (the
`.usage.json` files described in the [Evolution doc][evolution]) ride
along with the copy, so a cloned skill starts with its parent's
usage stats. This is intentional — pretend the cloned skill has been
"used" by the new profile so the curator's lifecycle clock doesn't
restart from zero on day one.

---

## The protected `default` profile

`default` is created on first boot if it doesn't exist. The seed
materialises the directory tree with empty SOUL/MEMORY/USER and an
empty `skills/`. You can edit the persona freely — but you cannot
delete the row:

```http
DELETE /admin/profiles/default
→ 409 {"error":"profile_protected","slug":"default", ...}
```

Why: the gateway falls back to `default` whenever a chat request
arrives without an explicit profile binding (no header, no localStorage
hint, no channel→profile mapping). Losing `default` would mean every
unbound request 503s.

---

## Switching profiles in the UI

Every admin page header carries a **Profile switcher** dropdown:

```text
[ Active: research-bot ▾ ]   [ Settings ]   [ ☀ / ☾ ]
```

The active slug is persisted to `localStorage` under the key
`corlinman_active_profile`, so a reload re-selects the same profile.
The dropdown lists every registered slug plus a **Create new profile**
shortcut to `/(admin)/profiles`.

The active profile drives:

- Which `SOUL.md` / `MEMORY.md` the agent reads on the next chat turn.
- Which `state.db` records the conversation.
- Which `skills/` directory the curator and skill registry walk.
- Which `curator_state` row gates the lifecycle loop.

> The switcher only affects the *admin UI's* view. Chat requests
> coming in over `/v1/chat/completions` carry a header
> (`X-Corlinman-Profile: <slug>`); requests without it land on
> `default`.

---

## API reference

All routes mount behind admin auth (session cookie). The base URL is
`http://localhost:6005`.

### Profile CRUD

| Method | Path                          | Body / params                                   | Response                 |
| ------ | ----------------------------- | ----------------------------------------------- | ------------------------ |
| GET    | `/admin/profiles`             | —                                               | `200` `[ProfileOut, ...]` |
| POST   | `/admin/profiles`             | `{slug, display_name?, clone_from?, description?}` | `201` `ProfileOut`     |
| GET    | `/admin/profiles/{slug}`      | —                                               | `200` `ProfileOut`        |
| PATCH  | `/admin/profiles/{slug}`      | `{display_name?, description?}`                 | `200` `ProfileOut`        |
| DELETE | `/admin/profiles/{slug}`      | —                                               | `204` (or `409` on `default`) |

### SOUL editor

| Method | Path                              | Body / params       | Response                  |
| ------ | --------------------------------- | ------------------- | ------------------------- |
| GET    | `/admin/profiles/{slug}/soul`     | —                   | `200` `{content: "..."}`  |
| PUT    | `/admin/profiles/{slug}/soul`     | `{content: "..."}`  | `200` `{content: "..."}`  |

The `PUT` is an atomic write: tempfile + `os.replace`. A crash
mid-write leaves the previous SOUL intact (which matters because the
persona is read on every chat turn — a partial file would corrupt the
agent mid-conversation).

### Error envelope

Every 4xx response carries a typed envelope so the UI doesn't have to
parse error strings:

```json
{ "detail": { "error": "profile_protected", "slug": "default", "message": "..." } }
```

Common error codes:

| Code                        | When                                                    |
| --------------------------- | ------------------------------------------------------- |
| `invalid_slug`              | Slug fails `SLUG_REGEX` (422)                           |
| `profile_exists`            | Slug already registered (409)                           |
| `parent_not_found`          | `clone_from` slug doesn't exist (404)                   |
| `profile_not_found`         | Slug missing on GET/PATCH/DELETE/soul (404)             |
| `profile_protected`         | DELETE on `default` (409)                               |
| `profile_store_missing`     | Gateway not fully booted yet (503) — retry              |

---

## Compatibility with `agents/*.yaml`

Profiles are **parallel** to the existing `agents/*.yaml` registry, not
a replacement. The decision was recorded in
[`PLAN_EASY_SETUP.md`](PLAN_EASY_SETUP.md) §6.

- The four shipped agents (`agents/orchestrator.yaml` etc) keep working
  exactly as before — same loading path, same `/admin/agents` UI, same
  routing.
- A profile is a richer construct: it bundles persona + memory + skills
  + state into one isolated workspace. Agents are slimmer — they're a
  YAML routing definition.
- One profile **can** internally use the YAML-defined agents; an agent
  cannot scope to a profile (yet — see
  [`docs/roadmap.md`](roadmap.md)).

If you only ever ran one agent, you only ever need the `default`
profile. The whole multi-profile surface is opt-in.

---

## See also

- [Quickstart](quickstart.md) — the 60-second boot path
- [Evolution & Curator](evolution-curator.md) — how the curator walks
  each profile's `skills/` directory
- [`profiles/paths.py`][paths] — the canonical on-disk layout
- [`profiles/store.py`][store] — the SQLite-backed registry + clone logic
- [`routes_admin_a/profiles.py`][routes] — the 7-endpoint FastAPI surface

[paths]: ../python/packages/corlinman-server/src/corlinman_server/profiles/paths.py
[store]: ../python/packages/corlinman-server/src/corlinman_server/profiles/store.py
[routes]: ../python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/profiles.py
[hermes]: https://github.com/yamamoto-toru/hermes-agent
[evolution]: evolution-curator.md
