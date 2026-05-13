#!/usr/bin/env bash
# Cross-compile corlinman release binaries.
#
# Outputs sha256-checksummed tar.gz archives under dist/.
#
# Usage:
#   ./scripts/build-release.sh                       # all targets sequential
#   ./scripts/build-release.sh linux-x86_64          # one target
#   ./scripts/build-release.sh --parallel linux      # both Linux musl targets at once
#   ./scripts/build-release.sh --profile release-thin macos-aarch64
#
# Aliases:
#   linux-x86_64   → x86_64-unknown-linux-musl   (Linux servers, Intel/AMD)
#   linux-aarch64  → aarch64-unknown-linux-musl  (Linux servers, ARM / Pi / Graviton)
#   macos-aarch64  → aarch64-apple-darwin        (Apple Silicon Macs)
#   linux          → both linux-* aliases above
#
# Build tooling:
#   - Linux targets run through `cross` (Docker required). On macOS hosts
#     this means QEMU emulation under colima — slow but reproducible.
#     cross-rs images include a glibc base; Cross.toml adds protoc.
#   - macOS target runs cargo directly (native).
#
# Optimisations baked in:
#   - Per-target CARGO_TARGET_DIR (target-<alias>/) so parallel builds
#     don't fight over the workspace lock.
#   - --parallel runs both Linux builds concurrently.
#   - jemalloc-under-QEMU spam filtered from stdout.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BINS=(
    "corlinman-gateway"
    "corlinman"
    "corlinman-auto-rollback"
    "corlinman-shadow-tester"
)

VERSION="$(grep -E '^version' Cargo.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIST="$ROOT/dist"
mkdir -p "$DIST"

PROFILE="release"
PARALLEL=0

resolve_target_alias() {
    case "$1" in
        linux-x86_64)  echo "x86_64-unknown-linux-musl" ;;
        linux-aarch64) echo "aarch64-unknown-linux-musl" ;;
        macos-aarch64) echo "aarch64-apple-darwin" ;;
        *) echo "$1" ;;
    esac
}

# Friendly alias → target dir suffix (so each target gets its own target/).
target_dir_for() {
    case "$1" in
        x86_64-unknown-linux-musl)  echo "target-x86_64-linux-musl" ;;
        aarch64-unknown-linux-musl) echo "target-aarch64-linux-musl" ;;
        aarch64-apple-darwin)       echo "target-aarch64-darwin" ;;
        *)                           echo "target-$1" ;;
    esac
}

TARGETS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --parallel)      PARALLEL=1; shift ;;
        --profile)       PROFILE="$2"; shift 2 ;;
        --profile=*)     PROFILE="${1#--profile=}"; shift ;;
        linux)
            TARGETS+=("$(resolve_target_alias linux-x86_64)")
            TARGETS+=("$(resolve_target_alias linux-aarch64)")
            shift
            ;;
        all)
            TARGETS+=("$(resolve_target_alias linux-x86_64)")
            TARGETS+=("$(resolve_target_alias linux-aarch64)")
            TARGETS+=("$(resolve_target_alias macos-aarch64)")
            shift
            ;;
        -*)
            echo "unknown flag: $1" >&2; exit 1 ;;
        *)
            TARGETS+=("$(resolve_target_alias "$1")")
            shift
            ;;
    esac
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
    # Default to all three when no positional arg.
    TARGETS=(
        "x86_64-unknown-linux-musl"
        "aarch64-unknown-linux-musl"
        "aarch64-apple-darwin"
    )
fi

# Filter jemalloc-under-QEMU spam from cross build output so progress
# stays scannable. Compiling lines pass through; jemalloc lines drop.
filter_noise() {
    grep -v -E '^<jemalloc>:' || true
}

build_target() {
    local target="$1"
    local builder
    case "$target" in
        *-linux-*) builder="cross" ;;
        *)         builder="cargo" ;;
    esac
    local td
    td="$(target_dir_for "$target")"
    local profile_flag
    if [[ "$PROFILE" == "release" ]]; then
        profile_flag="--release"
    else
        profile_flag="--profile $PROFILE"
    fi
    echo "==> $target via $builder ($PROFILE, CARGO_TARGET_DIR=$td)"
    CARGO_TARGET_DIR="$td" "$builder" build $profile_flag --target "$target" \
        $(for b in "${BINS[@]}"; do echo "--bin $b"; done) 2>&1 | filter_noise
}

# Output profile dir varies between `release` and `release-<name>`.
profile_dirname() {
    case "$PROFILE" in
        release) echo "release" ;;
        *)       echo "$PROFILE" ;;
    esac
}

package_target() {
    local target="$1"
    local td prof_dir
    td="$(target_dir_for "$target")"
    prof_dir="$(profile_dirname)"
    local outdir="$DIST/corlinman-${VERSION}-${target}"
    rm -rf "$outdir"
    mkdir -p "$outdir"
    for b in "${BINS[@]}"; do
        local src="$td/$target/$prof_dir/$b"
        if [[ ! -x "$src" ]]; then
            echo "missing built bin: $src" >&2
            return 1
        fi
        cp "$src" "$outdir/"
        if command -v strip >/dev/null 2>&1; then
            strip "$outdir/$b" 2>/dev/null || true
        fi
    done
    cat > "$outdir/README" <<EOF
corlinman ${VERSION} (git ${GIT_SHA})
target: ${target}
profile: ${PROFILE}
binaries: ${BINS[@]}

Install:
  curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh \\
    | bash -s -- --mode native

Or unpack manually under /opt/corlinman/bin/ and run corlinman-gateway.

Source: https://github.com/ymylive/corlinman/tree/${GIT_SHA}
EOF
    local archive="corlinman-${VERSION}-${target}.tar.gz"
    (cd "$DIST" && tar -czf "$archive" "$(basename "$outdir")")
    (cd "$DIST" && shasum -a 256 "$archive" > "$archive.sha256")
    echo "    ⇒ $DIST/$archive"
    cat "$DIST/$archive.sha256"
}

if [[ "$PARALLEL" -eq 1 ]] && [[ ${#TARGETS[@]} -gt 1 ]]; then
    echo "==> parallel mode: ${#TARGETS[@]} targets"
    pids=()
    for t in "${TARGETS[@]}"; do
        (
            build_target "$t" 2>&1 | sed "s/^/[$t] /"
            package_target "$t" 2>&1 | sed "s/^/[$t] /"
        ) &
        pids+=("$!")
    done
    fails=0
    for pid in "${pids[@]}"; do
        wait "$pid" || fails=$((fails + 1))
    done
    if [[ $fails -gt 0 ]]; then
        echo "$fails target(s) failed" >&2
        exit 1
    fi
else
    for t in "${TARGETS[@]}"; do
        build_target "$t"
        package_target "$t"
    done
fi

echo
echo "==> dist/ contents:"
ls -lh "$DIST"
