# corlinman 0.1.0 — first tagged release

**2026-04-21**

Self-hosted LLM toolbox — single binary gateway, multi-provider AI plane,
plugin runtime, RAG, channel adapters, admin UI, and full observability.
Pre-alpha status, but every critical path is test-covered and the
scenario runner (`corlinman qa run`) is green on 7 of 8 scenarios
(the 8th is marked `requires_live` and exercised by a screencast).

## What's in the box

- **Gateway**: OpenAI-compatible `/v1/chat/completions`, streaming + non-stream,
  session history, tool-call loop, real `/health` probes.
- **Agent (Python)**: Anthropic / OpenAI / Google / DeepSeek / Qwen / GLM
  providers; gRPC only — no HTTP surface.
- **Plugins**: sync / async / service types; JSON-RPC 2.0 stdio or gRPC.
- **RAG**: BM25 + HNSW + RRF + optional cross-encoder rerank.
- **Channels**: QQ (OneBot v11), Telegram; rate limit, multimodal, user binding.
- **Admin UI**: Next.js 15 dashboard covering every control plane.
- **Observability**: OTel OTLP + Prometheus three-tier metrics + 20+ doctor
  checks.
- **CLI**: `corlinman {onboard,doctor,plugins,config,dev,vector,qa}`.

## Install

**Build from source.** A prebuilt docker image on `ghcr.io/ymylive/corlinman`
is planned for 0.1.1 once a docker-equipped build host is available.

```bash
git clone https://github.com/ymylive/corlinman
cd corlinman
./scripts/dev-setup.sh        # deps + proto + hooks
cargo build --release -p corlinman-gateway -p corlinman-cli
./target/release/corlinman onboard
./target/release/corlinman dev
```

Data lives in `~/.corlinman/`. Override with `--data-dir` or
`CORLINMAN_DATA_DIR`.

## Perf baseline

In-process, Apple silicon, dev profile — see
[`docs/perf-baseline-1.0.md`](docs/perf-baseline-1.0.md) for the full
methodology.

| workload | p50 | p99 |
|---|---:|---:|
| `/v1/chat/completions` (gateway overhead) | 0.02 ms | 0.03 ms |
| RAG hybrid (128 chunks, `top_k=5`) | 0.27 ms | 0.31 ms |
| Plugin stdio roundtrip (python echo) | 16.99 ms | 31.31 ms |

## Known gaps

- **No prebuilt docker image yet** — build from source for now.
  `ghcr.io/ymylive/corlinman:0.1.0` will ship with 0.1.1.
- **Dashboard screenshot placeholder** in `README.md`
  (`docs/assets/dashboard.png`) pending the install screencast.
- **Fresh-install scenario** (`qa/scenarios/fresh-install.yaml`) is marked
  `requires_live: true`; covered by the S8 T4 screencast, not CI.

## What's next (0.1.1 and post-1.0)

- Docker images on `ghcr.io` (amd64 + arm64, with / without ML extras).
- Installation screencast (Asciinema).
- Release comms: blog post + Zhihu + HN / r/selfhosted / r/LocalLLaMA.
- Plugin SDK packages (`npm`, `PyPI`, `crates.io`) — see
  `docs/roadmap.md` §11.

## Full changelog

See [`CHANGELOG.md`](CHANGELOG.md) for the complete list of changes.
