#!/usr/bin/env bash
# Cross-compile corlinman release binaries for Linux x86_64 + aarch64
# (musl, static) and macOS aarch64 (native). Outputs sha256-checksummed
# tar.gz archives under dist/.
#
# Usage:
#   ./scripts/build-release.sh                 # all three targets
#   ./scripts/build-release.sh linux-x86_64    # one target
#
# Requires:
#   - cargo + rustup with targets installed:
#       rustup target add x86_64-unknown-linux-musl
#       rustup target add aarch64-unknown-linux-musl
#   - cross (for the Linux musl builds, runs inside a Docker toolchain):
#       cargo install cross --git https://github.com/cross-rs/cross
#   - docker daemon up (cross needs it)
#
# On macOS the native target (aarch64-apple-darwin) is built directly,
# no cross/Docker involvement.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Binaries to ship. Keep in sync with [[bin]] sections in Cargo.toml.
# `cargo build --bin <name>` only resolves bins that have a real
# [[bin]] entry; library-only crates (corlinman-mcp) are skipped here
# even though they ship as part of the gateway runtime.
BINS=(
    "corlinman-gateway"
    "corlinman"               # corlinman-cli's bin name
    "corlinman-auto-rollback" # rollback CLI helper
    "corlinman-shadow-tester" # shadow-router tester
)

VERSION="$(grep -E '^version' Cargo.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIST="$ROOT/dist"
mkdir -p "$DIST"

TARGETS_ALL=(
    "x86_64-unknown-linux-musl"
    "aarch64-unknown-linux-musl"
    "aarch64-apple-darwin"
)

# Friendly aliases → real rustc target triples. Resolved via case for
# bash 3.2 compatibility (macOS default).
resolve_target_alias() {
    case "$1" in
        linux-x86_64)  echo "x86_64-unknown-linux-musl" ;;
        linux-aarch64) echo "aarch64-unknown-linux-musl" ;;
        macos-aarch64) echo "aarch64-apple-darwin" ;;
        *) echo "$1" ;;
    esac
}

if [[ $# -gt 0 ]]; then
    TARGETS=("$(resolve_target_alias "$1")")
else
    TARGETS=("${TARGETS_ALL[@]}")
fi

build_target() {
    local target="$1"
    local builder
    case "$target" in
        *-linux-*) builder="cross" ;;
        *-apple-*) builder="cargo" ;;
        *) builder="cargo" ;;
    esac
    echo "==> building $target with $builder"
    "$builder" build --release --target "$target" \
        $(for b in "${BINS[@]}"; do echo "--bin $b"; done)
}

package_target() {
    local target="$1"
    local outdir="$DIST/corlinman-${VERSION}-${target}"
    rm -rf "$outdir"
    mkdir -p "$outdir"
    for b in "${BINS[@]}"; do
        local src="target/$target/release/$b"
        if [[ ! -x "$src" ]]; then
            echo "missing built bin: $src" >&2
            return 1
        fi
        cp "$src" "$outdir/"
        # strip — best-effort; cargo's `strip = "symbols"` on the
        # release profile already removes debug symbols, so this
        # is belt-and-braces.
        if command -v strip >/dev/null 2>&1; then
            strip "$outdir/$b" 2>/dev/null || true
        fi
    done
    # README so a human downloading the artifact knows what's inside.
    cat > "$outdir/README" <<EOF
corlinman ${VERSION} (git ${GIT_SHA})
target: ${target}
binaries: ${BINS[@]}

Install: see deploy/install.sh for one-line setup, or extract
this archive into /opt/corlinman/bin/ and run corlinman-gateway.

Source: https://github.com/ymylive/corlinman/tree/${GIT_SHA}
EOF
    local archive="corlinman-${VERSION}-${target}.tar.gz"
    (cd "$DIST" && tar -czf "$archive" "$(basename "$outdir")")
    (cd "$DIST" && shasum -a 256 "$archive" > "$archive.sha256")
    echo "    ⇒ $DIST/$archive"
    cat "$DIST/$archive.sha256"
}

for t in "${TARGETS[@]}"; do
    build_target "$t"
    package_target "$t"
done

echo
echo "==> dist/ contents:"
ls -lh "$DIST"
