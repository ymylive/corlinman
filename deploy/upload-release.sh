#!/usr/bin/env bash
# Push the local dist/ prebuilt artifacts to a target server via
# rsync over SSH, then restart the corlinman service. Used by the
# maintainer to roll out a release without going through CI.
#
# Pre-requisites:
#   - scripts/build-release.sh ran successfully — dist/ contains
#     fresh corlinman-<version>-<target>.tar.gz + .sha256 files.
#   - You can ssh to $TARGET_HOST as $TARGET_USER without a password
#     prompt (key auth, agent forwarding fine).
#   - The remote host has /opt/corlinman/bin already (run
#     deploy/install.sh --mode native first; this script only
#     replaces the binaries inside, doesn't bootstrap).
#
# Usage:
#   TARGET_USER=ops TARGET_HOST=corlinman.prod.example \
#     ./deploy/upload-release.sh
#
# Optional env:
#   TARGET_PREFIX  remote install root (default: /opt/corlinman)
#   TARGET_ARCH    arch to push (default: x86_64-unknown-linux-gnu)
#   SERVICE_NAME   systemd unit (default: corlinman)

set -euo pipefail

: "${TARGET_USER:?Set TARGET_USER}"
: "${TARGET_HOST:?Set TARGET_HOST}"
TARGET_PREFIX="${TARGET_PREFIX:-/opt/corlinman}"
TARGET_ARCH="${TARGET_ARCH:-x86_64-unknown-linux-gnu}"
SERVICE_NAME="${SERVICE_NAME:-corlinman}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"

VERSION="$(grep -E '^version' "$ROOT/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
ARCHIVE="corlinman-${VERSION}-${TARGET_ARCH}.tar.gz"

if [[ ! -f "$DIST/$ARCHIVE" ]]; then
    echo "missing artifact: $DIST/$ARCHIVE" >&2
    echo "run scripts/build-release.sh first." >&2
    exit 1
fi

if [[ ! -f "$DIST/$ARCHIVE.sha256" ]]; then
    echo "missing checksum: $DIST/$ARCHIVE.sha256" >&2
    exit 1
fi

echo "==> uploading $ARCHIVE to $TARGET_USER@$TARGET_HOST"
rsync -avz --progress \
    "$DIST/$ARCHIVE" "$DIST/$ARCHIVE.sha256" \
    "$TARGET_USER@$TARGET_HOST:/tmp/"

echo "==> verifying + installing on remote"
ssh "$TARGET_USER@$TARGET_HOST" bash -s -- <<EOF
set -euo pipefail
cd /tmp
shasum -a 256 -c "$ARCHIVE.sha256"
sudo mkdir -p "$TARGET_PREFIX/bin"
tar -xzf "$ARCHIVE"
extracted="corlinman-${VERSION}-${TARGET_ARCH}"
sudo cp "\$extracted/corlinman-gateway" "$TARGET_PREFIX/bin/"
sudo cp "\$extracted/corlinman" "$TARGET_PREFIX/bin/"
[ -f "\$extracted/corlinman-mcp" ] && sudo cp "\$extracted/corlinman-mcp" "$TARGET_PREFIX/bin/" || true
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl is-active "$SERVICE_NAME"
rm -rf "/tmp/\$extracted" "/tmp/$ARCHIVE" "/tmp/$ARCHIVE.sha256"
EOF

echo "==> done. service status:"
ssh "$TARGET_USER@$TARGET_HOST" "systemctl status $SERVICE_NAME --no-pager | head -20"
