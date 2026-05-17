#!/usr/bin/env bash
# corlinman one-line installer (Python plane, v1.x).
#
# Usage (any one of):
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode docker
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode native
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode native --china
#
# Modes:
#   docker  (default) — builds a Docker image locally from this repo, brings
#                       up corlinman + newapi via compose. Needs Docker
#                       Engine 24+ with the compose v2 plugin.
#   native            — installs uv, clones the repo, runs `uv sync
#                       --all-packages`, registers a systemd unit invoking
#                       `corlinman-gateway`. Requires root or sudo on Linux.
#
# Flags:
#   --china           Use mirrors (Tsinghua PyPI, NPM-mirror, USTC Docker,
#                     ghproxy.com for raw.githubusercontent.com). Autodetected
#                     when `curl https://pypi.org` is slow (>3s).
#   --version <ref>   Git ref / branch to install from (default: main).
#
# Environment overrides:
#   CORLINMAN_PREFIX     install root for --mode native (default: /opt/corlinman)
#   CORLINMAN_DATA_DIR   data dir (default: $CORLINMAN_PREFIX/data or ~/.corlinman)
#   CORLINMAN_PORT       gateway port (default: 6005)

set -euo pipefail

MODE="docker"
REF="${CORLINMAN_VERSION:-main}"
PREFIX="${CORLINMAN_PREFIX:-/opt/corlinman}"
DATA_DIR="${CORLINMAN_DATA_DIR:-${PREFIX}/data}"
PORT="${CORLINMAN_PORT:-6005}"
REPO="ymylive/corlinman"
USE_CHINA=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --mode=*) MODE="${1#--mode=}"; shift ;;
        --version) REF="$2"; shift 2 ;;
        --version=*) REF="${1#--version=}"; shift ;;
        --china) USE_CHINA="1"; shift ;;
        -h|--help)
            head -28 "$0" | sed -n '2,$p' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "required tool '$1' not on PATH"; }

# ----- China autodetect -------------------------------------------------------
# A 3-second TTFB on pypi.org is the rough breakpoint where uv sync starts to
# painfully stall; below that we don't bother routing through a mirror.
autodetect_china() {
    if [[ -n "$USE_CHINA" ]]; then return 0; fi
    local t
    t=$(curl -o /dev/null -fsS -m 3 -w '%{time_starttransfer}' https://pypi.org/simple/ 2>/dev/null || echo "999")
    awk -v t="$t" 'BEGIN { exit !(t+0 > 3.0) }' && USE_CHINA="1"
    if [[ -n "$USE_CHINA" ]]; then
        log "slow pypi.org TTFB (${t}s) — enabling --china mirrors"
    fi
}

# Mirror endpoints used when USE_CHINA is set.
GITHUB_RAW="https://raw.githubusercontent.com"
PIP_INDEX="https://pypi.org/simple"
DOCKER_REGISTRY_MIRROR=""
apply_china_mirrors() {
    if [[ -z "$USE_CHINA" ]]; then return 0; fi
    GITHUB_RAW="https://ghproxy.com/https://raw.githubusercontent.com"
    PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
    DOCKER_REGISTRY_MIRROR="https://docker.1panel.live"  # 1Panel mirror, stable in CN
    export UV_INDEX_URL="$PIP_INDEX"
    export PIP_INDEX_URL="$PIP_INDEX"
    log "China mode ON: pip→tuna, raw.github→ghproxy, docker→1panel"
}

# ----- Docker path ------------------------------------------------------------
install_docker() {
    require docker
    if ! docker compose version >/dev/null 2>&1; then
        die "docker compose v2 plugin required. install Docker Engine 24+."
    fi

    # Configure Docker daemon to use the CN registry mirror, if needed and not
    # already present. Best-effort: a write failure (non-root, exotic distro)
    # just falls back to upstream.
    if [[ -n "$USE_CHINA" && -n "$DOCKER_REGISTRY_MIRROR" ]]; then
        if [[ ! -f /etc/docker/daemon.json ]] || ! grep -q "$DOCKER_REGISTRY_MIRROR" /etc/docker/daemon.json 2>/dev/null; then
            log "registering docker registry mirror $DOCKER_REGISTRY_MIRROR"
            sudo mkdir -p /etc/docker || true
            echo "{\"registry-mirrors\": [\"$DOCKER_REGISTRY_MIRROR\"]}" | sudo tee /etc/docker/daemon.json >/dev/null || \
                warn "failed to write /etc/docker/daemon.json; continuing"
            sudo systemctl restart docker || warn "could not restart docker; continuing"
        fi
    fi

    log "cloning repo (ref=$REF) into $PREFIX"
    sudo mkdir -p "$PREFIX"
    sudo chown -R "$(id -u):$(id -g)" "$PREFIX"
    if [[ -d "$PREFIX/repo/.git" ]]; then
        git -C "$PREFIX/repo" fetch --depth 1 origin "$REF"
        git -C "$PREFIX/repo" checkout "$REF"
        git -C "$PREFIX/repo" reset --hard FETCH_HEAD
    else
        git clone --depth 1 --branch "$REF" "https://github.com/${REPO}.git" "$PREFIX/repo"
    fi

    log "building image"
    local extra_args=()
    if [[ -n "$USE_CHINA" ]]; then
        extra_args+=(
            --build-arg "PIP_INDEX=$PIP_INDEX"
            --build-arg "UV_INDEX_URL=$PIP_INDEX"
            --build-arg "DEBIAN_MIRROR=mirrors.tuna.tsinghua.edu.cn"
        )
    fi
    (cd "$PREFIX/repo" && docker buildx build "${extra_args[@]}" \
        -f docker/Dockerfile --target runtime -t corlinman:local --load .)

    log "writing compose override"
    mkdir -p "$DATA_DIR"
    cat > "$PREFIX/corlinman.yml" <<EOF
services:
  corlinman:
    image: corlinman:local
    container_name: corlinman
    restart: unless-stopped
    ports:
      - "${PORT}:6005"
    volumes:
      - "${DATA_DIR}:/data"
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      BIND: 0.0.0.0
      CORLINMAN_DATA_DIR: /data
      CORLINMAN_CONFIG: /data/config.toml
EOF

    log "starting"
    (cd "$PREFIX" && docker compose -f corlinman.yml up -d)

    cat <<EOF

✅ corlinman running at http://localhost:${PORT}
   open http://localhost:${PORT}/onboard to walk the 4-step wizard.
   logs: docker compose -f $PREFIX/corlinman.yml logs -f
   stop: docker compose -f $PREFIX/corlinman.yml down
EOF
}

# ----- Native path ------------------------------------------------------------
install_native() {
    require curl
    require git
    if [[ "$(uname -s)" != "Linux" && "$(uname -s)" != "Darwin" ]]; then
        die "unsupported OS for native mode: $(uname -s)"
    fi

    # Install uv if missing — fast Python package manager, single binary.
    if ! command -v uv >/dev/null 2>&1; then
        log "installing uv"
        if [[ -n "$USE_CHINA" ]]; then
            # Astral installer mirror via ghproxy
            curl -fsSL "${GITHUB_RAW/raw.githubusercontent.com/astral.sh}/uv/install.sh" | sh \
                || curl -fsSL https://astral.sh/uv/install.sh | sh
        else
            curl -fsSL https://astral.sh/uv/install.sh | sh
        fi
        export PATH="$HOME/.local/bin:$PATH"
    fi
    require uv

    log "cloning repo (ref=$REF) into $PREFIX"
    sudo mkdir -p "$PREFIX"
    sudo chown -R "$(id -u):$(id -g)" "$PREFIX"
    if [[ -d "$PREFIX/repo/.git" ]]; then
        git -C "$PREFIX/repo" fetch --depth 1 origin "$REF"
        git -C "$PREFIX/repo" checkout "$REF"
        git -C "$PREFIX/repo" reset --hard FETCH_HEAD
    else
        local clone_url="https://github.com/${REPO}.git"
        [[ -n "$USE_CHINA" ]] && clone_url="https://ghproxy.com/https://github.com/${REPO}.git"
        git clone --depth 1 --branch "$REF" "$clone_url" "$PREFIX/repo" \
            || git clone --depth 1 --branch "$REF" "https://github.com/${REPO}.git" "$PREFIX/repo"
    fi

    log "uv sync --all-packages (this can take a few minutes on first install)"
    (cd "$PREFIX/repo" && uv sync --all-packages --frozen --no-dev)

    mkdir -p "$DATA_DIR"

    if [[ "$(uname -s)" == "Linux" ]]; then
        log "writing systemd unit"
        local uv_path; uv_path="$(command -v uv)"
        sudo tee /etc/systemd/system/corlinman.service >/dev/null <<EOF
[Unit]
Description=corlinman gateway (Python)
After=network.target

[Service]
Type=simple
WorkingDirectory=${PREFIX}/repo
ExecStart=${uv_path} run corlinman-gateway --config ${DATA_DIR}/config.toml --port ${PORT}
Environment=CORLINMAN_DATA_DIR=${DATA_DIR}
Environment=BIND=0.0.0.0
Environment=PORT=${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now corlinman
        log "service status: $(systemctl is-active corlinman)"
    fi

    cat <<EOF

✅ corlinman installed under $PREFIX/repo
   data dir: $DATA_DIR
   gateway port: $PORT
   open: http://localhost:${PORT}/onboard
   manual run: cd $PREFIX/repo && uv run corlinman-gateway

EOF
}

# ----- entry -----------------------------------------------------------------
main() {
    autodetect_china
    apply_china_mirrors
    case "$MODE" in
        docker) install_docker ;;
        native) install_native ;;
        *) die "unknown --mode: $MODE (expected: docker | native)" ;;
    esac
}

main "$@"
