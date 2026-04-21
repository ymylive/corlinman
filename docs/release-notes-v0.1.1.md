# corlinman 0.1.1 — deployment hotfix

Released 2026-04-21. All changes since v0.1.0 are docker / runtime
fixes discovered the first time the 1.0.0 image was built against a
real server. No code behaviour changes outside the boot path.

## Fixes

- **`docker/Dockerfile`**: drop stale `pnpm -C ui export` step —
  Next.js 14 removed `next export`; `output: "export"` in
  `ui/next.config.ts` already emits the static bundle during
  `next build`. ([a4ae205](../../commit/a4ae205))
- **`docker/Dockerfile`**: bump rust base from `1.85-slim` to
  `1.95-slim` to match the project's `rust-toolchain.toml`. The old
  pin stopped working when `cargo-chef` 0.1.77's transitive
  `cargo-platform 0.3.2` raised its MSRV to `rustc 1.88`.
  ([b36e249](../../commit/b36e249))
- **`docker/Dockerfile`**: add `binutils` + `g++` to the rust-builder
  apt layer (required by `link-cplusplus`) and force the BFD linker
  via `RUSTFLAGS=-C link-arg=-fuse-ld=bfd` — `lld` SIGSEGVs under
  Rosetta 2 / QEMU user-mode emulation when building amd64 images on
  Apple Silicon. Also corrects the runtime `COPY` of the CLI binary:
  cargo emits `/build/target/release/corlinman` (per `[[bin]] name`),
  not `corlinman-cli`. ([035fc73](../../commit/035fc73))
- **`rust/crates/corlinman-gateway/src/main.rs`**: honour `BIND` env
  var. Previously `resolve_addr()` hardcoded `127.0.0.1`, so
  `docker run -p 6005:6005` never reached the listener. Containerised
  deploys now set `BIND=0.0.0.0`; developer laptops keep `127.0.0.1`
  as default. ([3a3940c](../../commit/3a3940c))
- **`docker/Dockerfile`**: carry the python source tree into the
  runtime image. `uv sync --no-editable` ignores workspace members, so
  the venv's `.pth` shims pointed at `/build/python/packages/*/src/`
  which doesn't exist in the runtime layer — `corlinman-python-server`
  died at `ModuleNotFoundError`. Adding `COPY --from=py-builder
  /build/python /build/python` resolves the editable paths (~2 MB).
  ([3a3940c](../../commit/3a3940c))

## Runtime env knobs surfaced

- `BIND` — listen address for the axum gateway (default `127.0.0.1`,
  set to `0.0.0.0` in containers).
- `OPENAI_BASE_URL` — picked up by `AsyncOpenAI` when
  `[providers.openai].base_url` isn't threaded through (known gap in
  `corlinman_providers.registry`; see Known Issues).

## Known issues carried over from v0.1.0

- `corlinman_providers.registry.resolve()` still ignores
  `[providers.*]` settings from `config.toml`. Until a deeper fix
  lands, point non-default OpenAI-compatible backends at the right
  host via `OPENAI_BASE_URL` env.
- The docker image does not supervise the python agent out of the box;
  production deploys need a startup script that spawns
  `corlinman-python-server` alongside `corlinman-gateway` (see
  `docker/start.sh` pattern).

## Upgrade

```bash
docker compose pull && docker compose up -d
```

Or rebuild locally:

```bash
docker buildx build --platform linux/amd64 \
  -f docker/Dockerfile -t corlinman:v0.1.1 --target runtime --load .
```
