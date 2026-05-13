#!/usr/bin/env bash
# One-command maintainer release: cross-build every target, attach
# every artifact to the given GitHub Release.
#
# Usage:
#   ./scripts/release-all.sh v0.5.0-newapi             # uploads to existing release/tag
#   ./scripts/release-all.sh v0.5.1 --create           # creates the release first
#   ./scripts/release-all.sh v0.5.1 --create --parallel
#
# Pre-requisites:
#   - docker daemon up (colima start)
#   - cross installed (cargo install cross --git ...)
#   - gh CLI authenticated (gh auth login)
#   - rustup targets: aarch64-apple-darwin + both *-linux-musl
#
# What it does:
#   1. ./scripts/build-release.sh all  (or --parallel)
#   2. gh release upload <TAG> dist/*.tar.gz dist/*.sha256 --clobber

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <tag> [--create] [--parallel] [--profile <name>]" >&2
    exit 1
fi

TAG="$1"; shift
CREATE=0
BUILD_ARGS=("all")

while [[ $# -gt 0 ]]; do
    case "$1" in
        --create)        CREATE=1; shift ;;
        --parallel)      BUILD_ARGS+=("--parallel"); shift ;;
        --profile)       BUILD_ARGS+=("--profile" "$2"); shift 2 ;;
        --profile=*)     BUILD_ARGS+=("$1"); shift ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
done

# Position "all" after flags so build-release.sh resolves --parallel etc. first.
TARGETS_ARG="${BUILD_ARGS[0]}"
unset 'BUILD_ARGS[0]'

echo "==> step 1/3: cross-build all targets"
./scripts/build-release.sh "${BUILD_ARGS[@]}" "$TARGETS_ARG"

if [[ $CREATE -eq 1 ]]; then
    echo "==> step 2/3: creating release $TAG"
    gh release view "$TAG" >/dev/null 2>&1 && {
        echo "release $TAG already exists, skipping create" >&2
    } || gh release create "$TAG" --title "$TAG" --generate-notes
else
    echo "==> step 2/3: skipping create (use --create to make a new release)"
fi

echo "==> step 3/3: uploading artifacts to $TAG"
gh release upload "$TAG" \
    dist/*.tar.gz \
    dist/*.sha256 \
    --clobber

echo
echo "✅ release $TAG updated. assets:"
gh release view "$TAG" --json assets --jq '.assets[].name'
