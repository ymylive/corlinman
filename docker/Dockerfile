# corlinman v1.x — pure Python multi-stage Dockerfile.
#
# Stages:
#   1. py-builder  → install workspace + deps into a venv with uv.
#   2. runtime     → slim Python 3.12 image, copies the prebuilt venv,
#                    bakes the UI static export at /app/ui-static.
#
# Build (no China mirror):
#   docker buildx build -f docker/Dockerfile --target runtime -t corlinman:dev .
#
# Build (China mirror — uses Tsinghua PyPI + ghproxy):
#   docker buildx build -f docker/Dockerfile --target runtime \
#       --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#       --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#       -t corlinman:dev .

# ---------------------------------------------------------------------------
# Stage 1: builder — install workspace into /opt/venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS py-builder

ARG PIP_INDEX=https://pypi.org/simple
ARG UV_INDEX_URL=https://pypi.org/simple
ARG DEBIAN_MIRROR=deb.debian.org

# Optional Debian mirror swap (saves minutes in CN networks).
RUN if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
            || sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        protobuf-compiler \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

# Install uv — pinned for reproducibility.
ENV UV_INSTALL_DIR=/usr/local/bin
ENV UV_INDEX_URL=${UV_INDEX_URL}
ENV PIP_INDEX_URL=${PIP_INDEX}
RUN curl -fsSL https://astral.sh/uv/0.5.30/install.sh | sh

WORKDIR /build

# Copy the workspace files needed for resolution first (cache-friendly).
COPY pyproject.toml uv.lock README.md ./
COPY python ./python
COPY proto ./proto
COPY scripts/gen-proto.sh ./scripts/gen-proto.sh

# Sync the entire workspace into /opt/venv. --frozen ensures we use uv.lock.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --all-packages --frozen --no-dev

# Regenerate proto stubs (the generated files are committed but rebuild
# defensively — keeps the image self-consistent even if a stale commit
# slips through).
RUN bash scripts/gen-proto.sh

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim image with just the venv + sources + UI static
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_MIRROR=deb.debian.org
RUN if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
            || sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Bring across the prebuilt venv and only the source the venv needs
# (editable installs reference /app/python).
COPY --from=py-builder /opt/venv /opt/venv
COPY --from=py-builder /build/python /app/python
COPY --from=py-builder /build/proto /app/proto

# Activate the venv.
ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONUNBUFFERED=1

# Optional UI static export. Build context must contain ui/out/ for this
# to land; if missing, the layer is a no-op (cp -r failure swallowed).
COPY ui/out* /app/ui-static/

# Default supervisor: launch agent sidecar + gateway in parallel.
COPY docker/start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 6005
WORKDIR /app

ENV CORLINMAN_DATA_DIR=/data
ENV CORLINMAN_UI_DIR=/app/ui-static
ENV BIND=0.0.0.0
ENV PORT=6005

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:6005/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
