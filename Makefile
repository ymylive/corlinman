# corlinman root Makefile. Keep thin — real logic in scripts/ or cargo/uv/pnpm.
.DEFAULT_GOAL := help
.PHONY: help dev build rust-build-fast test lint fmt proto docker ci clean

help:
	@echo "corlinman make targets:"
	@echo "  dev      one-shot developer bootstrap (hooks, rust, uv, pnpm, proto)"
	@echo "  build    cargo + uv + pnpm production builds"
	@echo "  rust-build-fast  Rust dogfood build using the faster release-thin profile"
	@echo "  test     cargo nextest + pytest (non-live) + pnpm test"
	@echo "  lint     fmt check + clippy -D warnings + ruff + mypy + ui typecheck"
	@echo "  fmt      cargo fmt + ruff format"
	@echo "  proto    regenerate Python gRPC stubs"
	@echo "  docker   build runtime image (no push)"
	@echo "  ci       run every .github/workflows/ci.yml job locally"

dev:
	bash scripts/dev-setup.sh

build:
	cargo build --release -p corlinman-gateway -p corlinman-cli
	uv sync --frozen --no-dev
	pnpm -C ui build

rust-build-fast:
	cargo build --profile release-thin -p corlinman-gateway -p corlinman-cli

test:
	cargo nextest run --workspace
	uv run pytest -m "not live_llm and not live_transport"
	pnpm -C ui test

lint:
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets -- -D warnings
	uv run ruff check .
	uv run mypy .
	pnpm -C ui typecheck

fmt:
	cargo fmt --all
	uv run ruff format .

proto:
	bash scripts/gen-proto.sh

docker:
	docker buildx build --target runtime -f docker/Dockerfile -t corlinman:dev .

# Mirror of .github/workflows/ci.yml — runs every job in series so a
# developer can reproduce CI failures locally. See docs/ci-status.md for
# per-job expectations and known yellow items.
ci:
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets -- -D warnings
	cargo nextest run --workspace
	uv sync --dev
	uv run ruff check .
	uv run mypy python/packages/
	uv run pytest -m "not live_llm and not live_transport"
	pnpm install --frozen-lockfile
	pnpm -C ui typecheck
	pnpm -C ui lint
	pnpm -C ui exec vitest run --passWithNoTests
	bash scripts/gen-proto.sh
	git diff --exit-code python/packages/corlinman-grpc/src/corlinman_grpc/_generated/
	cargo modules structure --package corlinman-gateway --lib --no-fns
	uv run lint-imports
