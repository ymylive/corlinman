#!/usr/bin/env bash
# corlinman developer setup — one-shot bootstrap for a fresh clone.
# Usage: bash scripts/dev-setup.sh
# Idempotent: safe to re-run.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

echo "==> [1/4] Installing git hooks (core.hooksPath=.git-hooks)"
git config core.hooksPath .git-hooks
chmod +x .git-hooks/pre-commit
echo "    done. FAST_COMMIT=1 bypasses hooks."

echo "==> [2/4] Python env (uv sync --all-packages --dev)"
if ! command -v uv >/dev/null 2>&1; then
  echo "    uv not found; install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi
uv sync --all-packages --dev
echo "    venv at $(uv run python -c 'import sys; print(sys.prefix)')"

echo "==> [3/4] UI deps (pnpm install)"
if ! command -v pnpm >/dev/null 2>&1; then
  if command -v corepack >/dev/null 2>&1; then
    corepack enable
  else
    echo "    pnpm/corepack not found; install Node 20 first" >&2
    exit 1
  fi
fi
pnpm install
echo "    done."

echo "==> [4/4] Generating Python gRPC stubs (scripts/gen-proto.sh)"
chmod +x scripts/gen-proto.sh
bash scripts/gen-proto.sh
echo "    done."

echo ""
echo "corlinman dev-setup complete."
echo "Next: make dev   (or)   uv run corlinman --help   (or)   uv run corlinman-gateway"
