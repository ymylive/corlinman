#!/usr/bin/env bash
# Generate Python gRPC stubs for corlinman from proto/*.proto via grpcio-tools.
#
# Usage: bash scripts/gen-proto.sh
#
# Inputs:  proto/corlinman/v1/*.proto
# Outputs: python/packages/corlinman-grpc/src/corlinman_grpc/_generated/
#          └── corlinman/v1/*_pb2.py{,i} + *_pb2_grpc.py
#
# After generation we rewrite the protoc-emitted
#   `from corlinman.v1 import foo_pb2 as ...`
# to
#   `from corlinman_grpc._generated.corlinman.v1 import foo_pb2 as ...`
# so the modules are importable through the installed `corlinman_grpc`
# package without polluting the top-level `corlinman` namespace.
#
# NOTE: the proto files use explicit renames (PluginToolCall / PluginToolResult /
# PluginAwaitingApproval in plugin.proto; EmbeddingVector in embedding.proto) to
# avoid descriptor-pool collisions with agent.proto / vector.proto. All six
# stubs can be eager-imported into the same process — see corlinman_grpc/__init__.py.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

PROTO_DIR="proto"
OUT_DIR="python/packages/corlinman-grpc/src/corlinman_grpc/_generated"

if [[ ! -d "$PROTO_DIR/corlinman/v1" ]]; then
  echo "gen-proto: $PROTO_DIR/corlinman/v1 missing; skipping" >&2
  exit 0
fi

# Fresh output (stubs are fully regenerated each run).
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
touch "$OUT_DIR/__init__.py"

# Collect .proto files portably (macOS ships bash 3.2 without `mapfile`).
PROTOS=()
while IFS= read -r -d '' f; do
  PROTOS+=("$f")
done < <(find "$PROTO_DIR/corlinman/v1" -maxdepth 1 -name '*.proto' -print0 | sort -z)

if [[ ${#PROTOS[@]} -eq 0 ]]; then
  echo "gen-proto: no .proto files under $PROTO_DIR/corlinman/v1"
  exit 0
fi

echo "gen-proto: generating ${#PROTOS[@]} proto(s) -> $OUT_DIR"

# grpcio-tools is in dev deps (pinned in top-level pyproject.toml so stubs are
# byte-identical across machines); requires `uv sync --dev` first.
# Compile all .proto files in a single protoc invocation so the generated
# descriptor-pool wiring is consistent.
uv run --quiet python -m grpc_tools.protoc \
  -I"$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "${PROTOS[@]}"

# Make the nested dirs proper Python packages.
touch "$OUT_DIR/corlinman/__init__.py" \
      "$OUT_DIR/corlinman/v1/__init__.py"

# Rewrite generated imports so the stubs resolve within corlinman_grpc
# without requiring a top-level `corlinman` package on sys.path.
#   from corlinman.v1 import foo_pb2 as ...
# ->
#   from corlinman_grpc._generated.corlinman.v1 import foo_pb2 as ...
uv run --quiet python - <<'PY'
import pathlib
import re

root = pathlib.Path("python/packages/corlinman-grpc/src/corlinman_grpc/_generated/corlinman/v1")
pattern = re.compile(r"^from corlinman\.v1 import ", re.MULTILINE)
replacement = "from corlinman_grpc._generated.corlinman.v1 import "
for py in sorted(root.glob("*.py")):
    text = py.read_text()
    new = pattern.sub(replacement, text)
    if new != text:
        py.write_text(new)
# .pyi stubs use the same import form.
for pyi in sorted(root.glob("*.pyi")):
    text = pyi.read_text()
    new = pattern.sub(replacement, text)
    if new != text:
        pyi.write_text(new)
PY

# Format generated Python (ruff format is deterministic; keeps diffs small).
# `--isolated` bypasses the repo-root pyproject.toml which globally excludes
# `**/_generated/**` from linting; without it ruff prints "No Python files
# found" and leaves the protoc output untouched, causing whitespace drift
# across developer/CI machines with different protoc point releases.
uv run --quiet ruff format --isolated "$OUT_DIR" >/dev/null

echo "gen-proto: ok"
