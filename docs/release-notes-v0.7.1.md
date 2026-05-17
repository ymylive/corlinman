# corlinman v0.7.1 — Warm Pool

## Headline

Ships the OpenClaw-inspired warm-pool surface that v0.7.0 deferred,
plus four v0.7.0 hygiene smoke tests that confirm the end-to-end
multi-agent path works.

## Architectural note

The original v0.7.0 plan called for a new Rust crate
`corlinman-runner-pool` modelled on OpenClaw's per-session container
pool. Mapping that to corlinman's actual architecture surfaced a
mismatch: the Rust gateway talks gRPC to a **long-running** Python
servicer; chat sessions don't spawn fresh OS processes per request.
The cold-start cost lives in **provider SDK first-call setup**
(httpx client, auth, model schema validation), not in subprocess
boot.

The faithful adaptation is therefore a **Python-side pool** with the
same surface and semantics (acquire / release / prewarm / oldest-idle
eviction / bounded by both per-key and total caps), wired as a
**boot-time pre-warm hook** in the servicer. Operators call
`prewarm_providers(["claude-sonnet-4-6", "gpt-4o", ...])` immediately
after constructing the servicer; the auth handshake happens off the
hot path before the first user chat.

The pool is generic (`RunnerPool[T]`) so future releases can park
context assemblers, per-tenant providers, or sandboxed reasoning
loops in the same structure without redesigning the surface.

## What's new

- **`corlinman_server.runner_pool`** module:
  - `RunnerPool[T]` — bounded warm pool, thread-safe under an internal
    lock, factory runs *outside* the lock so a slow constructor doesn't
    serialise other keys.
  - `RunnerHandle[T]` — drop-guard with context-manager protocol.
  - `PoolStats` — hits / misses / evictions / warm_count / warm_age_s.
- **`CorlinmanAgentServicer.prewarm_providers(model_names)`** — call
  at boot for known aliases.
- **`CorlinmanAgentServicer.pool_stats()`** — snapshot for operator
  tooling.
- env: `CORLINMAN_RUNNER_POOL_WARM` (default 2),
  `CORLINMAN_RUNNER_POOL_MAX` (default 8).

## What's tested

- 12 pool tests (hit/miss accounting, per-key cap, active-total
  eviction, prewarm respects caps, handle drop-guard idempotent,
  factory runs outside lock).
- 4 servicer smoke tests for v0.7.0 (orchestrator spawn_many round-
  trip, parent_tools threading, prewarm populates pool, prewarm
  swallows resolver errors).
- All 39 `corlinman-server` tests pass. Ruff clean.

## Compatibility

- **No new required config keys.** Pool defaults (2 / 8) work without
  any operator action; `prewarm_providers` is opt-in.
- **No upstream behaviour change.** The per-chat hot path still
  delegates to the provider registry's existing memoisation. The pool
  exists for boot-time pre-resolution today and per-tenant /
  sandboxed providers in v0.8.
- **No new dependency.** Pool observability uses `structlog`
  (matches the rest of the Python side); the Rust gateway continues
  to own Prometheus exposition for end-to-end latency.

## Manual release step

```
git tag v0.7.1
git push origin main v0.7.1
```

CI takes it from there.
