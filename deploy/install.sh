#!/usr/bin/env bash
# corlinman one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode docker
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode native
#
# Modes:
#   docker  (default) — pulls the latest prebuilt image + docker-compose,
#                       brings up corlinman + newapi together. Requires
#                       Docker Engine 24+ with the compose v2 plugin.
#   native            — downloads the prebuilt tarball for the host arch,
#                       installs binaries under /opt/corlinman/bin, writes
#                       a systemd unit. Requires root or sudo.
#
# Environment overrides:
#   CORLINMAN_VERSION    pin a release tag (default: latest)
#   CORLINMAN_PREFIX     install root for --mode native (default: /opt/corlinman)
#   CORLINMAN_DATA_DIR   data dir (default: $CORLINMAN_PREFIX/data)
#   CORLINMAN_PORT       gateway port (default: 6005)

set -euo pipefail

MODE="docker"
VERSION="${CORLINMAN_VERSION:-latest}"
PREFIX="${CORLINMAN_PREFIX:-/opt/corlinman}"
DATA_DIR="${CORLINMAN_DATA_DIR:-$PREFIX/data}"
PORT="${CORLINMAN_PORT:-6005}"
REPO="ymylive/corlinman"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --mode=*) MODE="${1#--mode=}"; shift ;;
        --version) VERSION="$2"; shift 2 ;;
        *)
            echo "unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

detect_arch() {
    local m
    m="$(uname -m)"
    case "$m" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64)
            local os
            os="$(uname -s)"
            if [[ "$os" == "Darwin" ]]; then
                echo "aarch64"
            else
                # Linux aarch64 prebuilts aren't currently published — numkong
                # cross-compile blocker. Fall back to source build.
                die "Linux aarch64 prebuilts are not yet published. Build from source:
  git clone https://github.com/ymylive/corlinman && cd corlinman
  cargo build --release -p corlinman-gateway -p corlinman-cli
See docs/dev/build-fast.md for native build tips."
            fi
            ;;
        *) die "unsupported arch: $m" ;;
    esac
}

detect_os() {
    case "$(uname -s)" in
        Linux*)   echo "linux" ;;
        Darwin*)  echo "macos" ;;
        *)        die "unsupported OS: $(uname -s)" ;;
    esac
}

require() {
    command -v "$1" >/dev/null 2>&1 || die "required tool '$1' not on PATH"
}

resolve_release_tag() {
    if [[ "$VERSION" == "latest" ]]; then
        require curl
        VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
            | grep -E '"tag_name"' | head -1 | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
        [[ -n "$VERSION" ]] || die "could not resolve latest release tag from GitHub"
    fi
    log "target version: $VERSION"
}

install_docker() {
    require docker
    if ! docker compose version >/dev/null 2>&1; then
        die "docker compose v2 plugin required (docker compose ...). install Docker Engine 24+."
    fi
    log "downloading docker-compose snippet"
    mkdir -p "$PREFIX"
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/${VERSION}/docker/compose/newapi.yml" \
        -o "$PREFIX/newapi.yml"

    # Compose file for corlinman itself. Inlined so the installer
    # works against any release tag without pre-baking a separate
    # compose file in the repo.
    cat > "$PREFIX/corlinman.yml" <<EOF
services:
  corlinman:
    image: ghcr.io/${REPO}:${VERSION}
    container_name: corlinman
    ports:
      - "${PORT}:6005"
    volumes:
      - "${DATA_DIR}:/var/lib/corlinman"
    environment:
      - CORLINMAN_DATA_DIR=/var/lib/corlinman
      - RUST_LOG=info
    restart: unless-stopped
    depends_on:
      newapi:
        condition: service_healthy
EOF

    mkdir -p "$DATA_DIR" "$PREFIX/newapi-data"
    log "starting compose stack at $PREFIX"
    (cd "$PREFIX" && docker compose -f corlinman.yml -f newapi.yml up -d)

    cat <<EOF

✅ corlinman is starting at http://localhost:${PORT}
   newapi admin at http://localhost:3000 (default creds: root / 123456 — change immediately)

Next steps:
  1. Open http://localhost:3000, log in, create a user token + system access token.
  2. Open http://localhost:${PORT}/onboard to walk the 4-step wizard.

Logs:  docker compose -f $PREFIX/corlinman.yml -f $PREFIX/newapi.yml logs -f
Stop:  docker compose -f $PREFIX/corlinman.yml -f $PREFIX/newapi.yml down

EOF
}

install_native() {
    require curl
    require tar
    local os arch tarball url
    os="$(detect_os)"
    arch="$(detect_arch)"
    if [[ "$os" == "linux" ]]; then
        tarball="corlinman-${VERSION#v}-${arch}-unknown-linux-gnu.tar.gz"
    else
        tarball="corlinman-${VERSION#v}-${arch}-apple-darwin.tar.gz"
    fi
    url="https://github.com/${REPO}/releases/download/${VERSION}/${tarball}"
    log "downloading ${tarball}"
    local tmp
    tmp="$(mktemp -d)"
    curl -fsSL "$url" -o "$tmp/$tarball"

    # `[[ -f "$url" ]]` always returns false on a URL string — the
    # previous code accidentally relied on the HEAD-probe fallback.
    # Drop the dead branch; HEAD-probe is the only meaningful check.
    if curl -fsSI "$url.sha256" >/dev/null 2>&1; then
        curl -fsSL "$url.sha256" -o "$tmp/$tarball.sha256"
        (cd "$tmp" && shasum -a 256 -c "$tarball.sha256") || die "checksum mismatch"
    else
        warn "no checksum file at $url.sha256 — skipping verification"
    fi

    log "installing to $PREFIX/bin"
    sudo mkdir -p "$PREFIX/bin" "$DATA_DIR"
    sudo tar -xzf "$tmp/$tarball" -C "$tmp"
    # Copy every binary release.yml packages. Names must stay in sync
    # with the `for b in …; do cp … done` loop in
    # .github/workflows/release.yml's Package step.
    for bin in corlinman-gateway corlinman corlinman-auto-rollback corlinman-shadow-tester; do
        sudo cp "$tmp"/corlinman-*/"$bin" "$PREFIX/bin/"
    done

    if [[ "$os" == "linux" ]]; then
        log "writing systemd unit"
        sudo tee /etc/systemd/system/corlinman.service >/dev/null <<EOF
[Unit]
Description=corlinman gateway
After=network.target

[Service]
Type=simple
ExecStart=$PREFIX/bin/corlinman-gateway start
Environment=CORLINMAN_DATA_DIR=$DATA_DIR
Environment=RUST_LOG=info
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now corlinman
        log "service started: systemctl status corlinman"
    fi

    cat <<EOF

✅ corlinman ${VERSION} installed to $PREFIX
   Data dir: $DATA_DIR
   Gateway port: $PORT
   Open: http://localhost:${PORT}/onboard

EOF
}

main() {
    resolve_release_tag
    case "$MODE" in
        docker) install_docker ;;
        native) install_native ;;
        *) die "unknown --mode: $MODE (expected: docker | native)" ;;
    esac
}

main "$@"
