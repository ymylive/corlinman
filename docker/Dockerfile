# syntax=docker/dockerfile:1.7
#
# corlinman multi-stage build
# Stages: rust-planner -> rust-builder -> py-builder -> ui-builder -> runtime
# Plan ref: §10 Docker 部署
#
# Build context: repo root (so COPY sees rust/ python/ ui/ proto/ scripts/ ...)
#   docker buildx build --target runtime -f docker/Dockerfile .

# ---------------------------------------------------------------------------
# Stage 1: rust-planner — prepare cargo-chef recipe
# ---------------------------------------------------------------------------
FROM rust:1.95-slim AS rust-planner
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      pkg-config \
      libssl-dev \
      protobuf-compiler \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && cargo install cargo-chef --locked --version ^0.1

COPY Cargo.toml Cargo.lock* ./
COPY rust-toolchain.toml ./
COPY rust ./rust
COPY proto ./proto

RUN cargo chef prepare --recipe-path /recipe.json

# ---------------------------------------------------------------------------
# Stage 2: rust-builder — cook deps, build release binaries
# ---------------------------------------------------------------------------
FROM rust:1.95-slim AS rust-builder
WORKDIR /build

# `binutils` brings `ld.bfd`; we force it via RUSTFLAGS below because
# `lld` (rust's default for x86_64-linux-gnu) SIGSEGVs under Rosetta 2
# / QEMU user-mode emulation on Apple Silicon hosts. BFD is slower but
# stable under emulation and produces identical binaries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      pkg-config \
      libssl-dev \
      protobuf-compiler \
      ca-certificates \
      binutils \
      g++ \
 && rm -rf /var/lib/apt/lists/* \
 && cargo install cargo-chef --locked --version ^0.1

ENV RUSTFLAGS="-C link-arg=-fuse-ld=bfd"

# Cook dependencies (cached layer unless deps change). BuildKit cache
# mounts keep the cargo registry + git checkouts + the build target dir
# warm across rebuilds on the same host — first cold build still pays
# the full ~12 min, but every subsequent rebuild with the same lockfile
# drops to ~90 s. Mount `id=` lets concurrent multi-arch builds share.
COPY --from=rust-planner /recipe.json /recipe.json
RUN --mount=type=cache,id=cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=cargo-target,target=/build/target,sharing=locked \
    cargo chef cook --release --recipe-path /recipe.json

# Copy sources and build binaries
COPY Cargo.toml Cargo.lock* ./
COPY rust-toolchain.toml ./
COPY rust ./rust
COPY proto ./proto

# Same three cache mounts; without target/ persisting across the cook
# and the build steps cargo would re-link the world. The final binary
# copy uses an explicit cp because the cache mount is gone after RUN
# exits — release/<bin> lives only inside the mount during the build.
RUN --mount=type=cache,id=cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=cargo-target,target=/build/target,sharing=locked \
    cargo build --release \
      -p corlinman-gateway \
      -p corlinman-cli \
      -p corlinman-shadow-tester \
      -p corlinman-auto-rollback-cli \
 && mkdir -p /out \
 && cp target/release/corlinman-gateway \
       target/release/corlinman \
       target/release/corlinman-shadow-tester \
       target/release/corlinman-auto-rollback /out/

# ---------------------------------------------------------------------------
# Stage 3: py-builder — uv sync + generate Python gRPC stubs
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS py-builder
WORKDIR /build

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_HTTP_TIMEOUT=180

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      bash \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir \
      --retries 5 \
      --timeout 180 \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      uv

COPY pyproject.toml uv.lock* ./
COPY python ./python
COPY proto ./proto
COPY scripts ./scripts

# Create venv via uv sync. --no-editable copies wheels into the venv
# instead of emitting .pth shims that reference /build/* source paths —
# otherwise the runtime image can't import workspace packages because
# their source tree isn't carried over from the builder.
# BuildKit cache mount keeps uv's wheel cache warm across rebuilds so
# the second build skips PyPI fetches entirely.
RUN --mount=type=cache,id=uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Generate Python gRPC stubs into corlinman_grpc package
RUN chmod +x scripts/gen-proto.sh && scripts/gen-proto.sh

# ---------------------------------------------------------------------------
# (Stage 4 removed) — UI is built outside the image and served by nginx from
# `/opt/corlinman/ui-static/`. Bundling it into the container was dead weight
# and the in-emulator node build segfaults under Rosetta 2 on Apple Silicon.
# Operators pointing at gateway-served UI should place a built bundle under
# `$CORLINMAN_UI_DIR` via a bind mount.
#
# ---------------------------------------------------------------------------
# Stage 5: runtime — slim final image
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# nodejs needed to run node-based plugins (stdio children)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      tini \
      nodejs \
      curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 corlinman \
 && useradd --system --uid 1000 --gid corlinman --home-dir /data --shell /bin/bash corlinman \
 && mkdir -p /data /app/ui-static \
 && chown -R corlinman:corlinman /data /app

# Rust binaries. Sourced from /out/ rather than target/release because
# the BuildKit cache mount on target/ is gone after the build RUN exits —
# the rust-builder stage explicitly copies the four binaries to /out
# (which survives) so this stage can pull them.
COPY --from=rust-builder /out/corlinman-gateway        /usr/local/bin/corlinman-gateway
COPY --from=rust-builder /out/corlinman                /usr/local/bin/corlinman
# Phase 3 W1 CLIs invoked by the in-gateway scheduler. Without these the
# shadow_tester / auto_rollback cron jobs would log spawn_failed every fire.
COPY --from=rust-builder /out/corlinman-shadow-tester  /usr/local/bin/corlinman-shadow-tester
COPY --from=rust-builder /out/corlinman-auto-rollback  /usr/local/bin/corlinman-auto-rollback

# Python venv (uv-managed). Workspace packages install as editable
# regardless of `--no-editable` (uv treats `workspace = true` members as
# always-editable), so also carry the source tree the .pth files point
# to. Small (~2 MB) and makes imports resolve at runtime.
COPY --from=py-builder /opt/venv /opt/venv
COPY --from=py-builder /build/python /build/python

# Admin UI: NOT bundled — production uses nginx to serve the static export
# from the host. A bind mount at /app/ui-static works if anyone wants the
# gateway to serve it directly instead.

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CORLINMAN_UI_DIR=/app/ui-static \
    CORLINMAN_DATA_DIR=/data

# Build-time sanity check: every CLI the in-gateway scheduler shells out
# to must be present, otherwise jobs spawn_fail every fire. Fails the
# build loudly (exit 1) instead of letting the container boot into a
# silently-broken state.
RUN ls /usr/local/bin/corlinman-gateway \
       /usr/local/bin/corlinman \
       /usr/local/bin/corlinman-shadow-tester \
       /usr/local/bin/corlinman-auto-rollback >&2 \
 && ls /opt/venv/bin/corlinman-evolution-engine \
       /opt/venv/bin/corlinman-persona \
       /opt/venv/bin/corlinman-user-model >&2 \
 || (echo "MISSING CLI" >&2 && exit 1)

EXPOSE 6005
WORKDIR /data
USER corlinman

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:6005/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["corlinman-gateway", "--config", "/data/config.toml"]
