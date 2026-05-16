#!/usr/bin/env bash
set -euo pipefail

PROFILE="${PROFILE:-dev}"
PACKAGES="${PACKAGES:-corlinman-gateway corlinman-cli}"
CLEAN="${CLEAN:-0}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/.target-baseline}"
export CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"
export RUSTUP_HOME="${RUSTUP_HOME:-$HOME/.rustup}"

if [[ "$CLEAN" == "1" ]]; then
  rm -rf "$CARGO_TARGET_DIR"
fi

profile_args=()
case "$PROFILE" in
  dev) ;;
  release) profile_args+=(--release) ;;
  *) profile_args+=(--profile "$PROFILE") ;;
esac

package_args=()
for package in $PACKAGES; do
  package_args+=(-p "$package")
done

echo "Repo: $ROOT"
echo "Target: $CARGO_TARGET_DIR"
echo "Command: cargo build ${profile_args[*]} ${package_args[*]}"

start="$(date +%s)"
set +e
cargo build "${profile_args[@]}" "${package_args[@]}"
exit_code="$?"
set -e
end="$(date +%s)"
echo "ElapsedSeconds: $((end - start))"

if [[ "$exit_code" -ne 0 ]]; then
  exit "$exit_code"
fi

profile_dir="$PROFILE"
if [[ "$PROFILE" == "dev" ]]; then
  profile_dir="debug"
elif [[ "$PROFILE" == "release" ]]; then
  profile_dir="release"
fi

for binary in corlinman-gateway corlinman; do
  path="$CARGO_TARGET_DIR/$profile_dir/$binary"
  if [[ -f "$path" ]]; then
    ls -lh "$path"
  fi
done

if command -v sccache >/dev/null 2>&1; then
  sccache --show-stats
fi
