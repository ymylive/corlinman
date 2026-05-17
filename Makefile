# corlinman root Makefile. Keep thin — real logic in scripts/ or uv/pnpm.
.DEFAULT_GOAL := help
.PHONY: help dev build test lint fmt proto docker ci clean

help:
	@echo "corlinman make targets:"
	@echo "  dev      one-shot developer bootstrap (hooks, uv, pnpm, proto)"
	@echo "  build    uv + pnpm production builds"
	@echo "  test     pytest (non-live) + pnpm test"
	@echo "  lint     ruff + mypy + ui typecheck"
	@echo "  fmt      ruff format"
	@echo "  proto    regenerate Python gRPC stubs"
	@echo "  docker   build runtime image (no push)"
	@echo "  ci       run every .github/workflows/ci.yml job locally"

dev:
	bash scripts/dev-setup.sh

build:
	uv sync --all-packages --frozen --no-dev
	pnpm -C ui build

test:
	uv run pytest -m "not live_llm and not live_transport"
	pnpm -C ui test

lint:
	uv run ruff check .
	uv run mypy python/packages/
	pnpm -C ui typecheck

fmt:
	uv run ruff format .

proto:
	bash scripts/gen-proto.sh

docker:
	docker buildx build --target runtime -f docker/Dockerfile -t corlinman:dev .

# Mirror of .github/workflows/ci.yml — runs every job in series so a
# developer can reproduce CI failures locally. See docs/ci-status.md for
# per-job expectations and known yellow items.
ci:
	uv sync --all-packages --dev
	uv run ruff check .
	uv run mypy python/packages/
	uv run pytest -m "not live_llm and not live_transport"
	pnpm install --frozen-lockfile
	pnpm -C ui typecheck
	pnpm -C ui lint
	pnpm -C ui exec vitest run --passWithNoTests
	bash scripts/gen-proto.sh
	git diff --exit-code python/packages/corlinman-grpc/src/corlinman_grpc/_generated/
	uv run lint-imports
