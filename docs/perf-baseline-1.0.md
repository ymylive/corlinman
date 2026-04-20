# Perf baseline — corlinman 0.1.0

Captured by `corlinman qa bench --iterations 500 --warmup 50` on
2026-04-21. The runner drives three critical paths **in-process**
(no real network, no real provider) so the numbers reflect
corlinman's own overhead, not upstream latency.

**Reference box**

- Apple silicon, `Darwin arm64`.
- `rustc 1.95.0`, `cargo build` in `dev` profile.
- Cold cargo cache warmed up once before sampling.

**Workloads**

| workload | what it exercises |
|---|---|
| `chat_completions` | `POST /v1/chat/completions` (non-stream) through the axum router, with a scripted `ChatBackend` that emits one token + `Done`. Measures gateway routing, JSON encode, tool-executor no-op, session store skipped. |
| `rag_hybrid` | `HybridSearcher::search` over 128 chunks (BM25 + usearch dense, RRF fusion, `top_k=5`). SQLite lives in a tempdir, usearch in memory. |
| `plugin_stdio` | One full spawn of a python echo plugin through `jsonrpc_stdio::execute` — includes process fork, stdin write, stdout read, child exit. |

## Results

| workload | iterations | p50 | p99 | mean |
|---|---:|---:|---:|---:|
| `chat_completions` | 500 | 0.02 ms | 0.03 ms | 0.03 ms |
| `rag_hybrid`       | 500 | 0.27 ms | 0.31 ms | 0.27 ms |
| `plugin_stdio`     | 80  | 16.99 ms | 31.31 ms | 17.18 ms |

### Notes

- `chat_completions` is inherently synthetic — no real provider latency
  is added. A real Anthropic/OpenAI call typically adds 300–2000 ms
  end-to-end; gateway overhead sits in the fraction-of-a-ms range, which
  is what we wanted to confirm before 1.0.
- `plugin_stdio` is dominated by python interpreter startup (~15 ms on
  this box). The in-process handshake and JSON-RPC parse are a few
  hundred microseconds on top. Service plugins (gRPC) stay warm between
  calls and side-step this cost; see `corlinman-plugins::runtime::service_grpc`.
- `rag_hybrid` p99 is roughly 1.1× the p50, indicating the fused ranker is
  stable. FTS5 `MATCH` dominates the per-query cost at this corpus size.

## Regression policy

CI should fail if any p99 regresses by more than 20 % vs this baseline.
That's a ceiling on silent perf drift — not a target. Small improvements
are expected as we move to ML-feature-gated deps and the service plugin
cache.

Re-run the bench and overwrite this file whenever you land a change that
moves p50 beyond ±10 %:

```bash
corlinman qa bench --iterations 500 --warmup 50 --report docs/perf-baseline-1.0.md
```

The `--report` flag overwrites the Markdown table section above but
preserves nothing else — refresh the prose by hand when the hardware
or schema changes.
