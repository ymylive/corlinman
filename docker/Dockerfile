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

# Cook dependencies (cached layer unless deps change)
COPY --from=rust-planner /recipe.json /recipe.json
RUN cargo chef cook --release --recipe-path /recipe.json

# Copy sources and build binaries
COPY Cargo.toml Cargo.lock* ./
COPY rust-toolchain.toml ./
COPY rust ./rust
COPY proto ./proto

RUN cargo build --release \
      -p corlinman-gateway \
      -p corlinman-cli

# ---------------------------------------------------------------------------
# Stage 3: py-builder — uv sync + generate Python gRPC stubs
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS py-builder
WORKDIR /build

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      bash \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv

COPY pyproject.toml uv.lock* ./
COPY python ./python
COPY proto ./proto
COPY scripts ./scripts

# Create venv via uv sync. --no-editable copies wheels into the venv
# instead of emitting .pth shims that reference /build/* source paths —
# otherwise the runtime image can't import workspace packages because
# their source tree isn't carried over from the builder.
RUN uv sync --frozen --no-dev --no-editable

# Generate Python gRPC stubs into corlinman_grpc package
RUN chmod +x scripts/gen-proto.sh && scripts/gen-proto.sh

# ---------------------------------------------------------------------------
# Stage 4: ui-builder — pnpm install + Next.js build + export
# ---------------------------------------------------------------------------
FROM node:20-slim AS ui-builder
WORKDIR /build

RUN corepack enable \
 && corepack prepare pnpm@latest --activate

COPY pnpm-workspace.yaml package.json* pnpm-lock.yaml* ./
COPY ui ./ui

RUN pnpm install --frozen-lockfile \
 && pnpm -C ui build

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

# Rust binaries
COPY --from=rust-builder /build/target/release/corlinman-gateway /usr/local/bin/corlinman-gateway
COPY --from=rust-builder /build/target/release/corlinman         /usr/local/bin/corlinman

# Python venv (uv-managed)
COPY --from=py-builder /opt/venv /opt/venv

# Next.js exported static bundle
COPY --from=ui-builder /build/ui/out /app/ui-static

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CORLINMAN_UI_DIR=/app/ui-static \
    CORLINMAN_DATA_DIR=/data

EXPOSE 6005
WORKDIR /data
USER corlinman

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:6005/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["corlinman-gateway", "--config", "/data/config.toml"]
