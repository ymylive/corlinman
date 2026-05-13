# Faster Rust builds

corlinman's Rust workspace is 30+ crates; a cold `cargo build --release`
can spend 5–10 minutes mostly on link time and on recompiling unchanged
dependencies. This page describes the one-time toolchain setup that
brings dev-mode rebuilds under a minute and release builds to roughly
2–3 minutes.

The repo ships with [`.cargo/config.toml`](../../.cargo/config.toml)
preconfigured. You only need to install the tools below.

## 1. Local build cache: `sccache`

Caches the output of every `rustc` invocation keyed on inputs + flags.
First build of any crate populates the cache; subsequent builds (even
on a `git switch`) re-use the artifacts.

```bash
cargo install sccache --locked
```

Optional: point sccache at S3 / Redis for shared CI cache. Defaults
fine for solo work.

Verify:

```bash
sccache --show-stats
```

## 2. Faster linker

Linking is the bottleneck for the gateway crate. `mold` on Linux and
`lld` on macOS cut link time from ~30s to ~3s.

### Linux

```bash
sudo apt-get install -y mold clang   # debian/ubuntu
# or
sudo dnf install -y mold clang       # fedora
```

`.cargo/config.toml` already wires `-fuse-ld=mold` for the standard
GNU + musl targets.

### macOS

Xcode CLT ships `ld`. For LLD: `brew install llvm`. Apple's stock
linker is fine for most workloads; this is only worth doing if your
incremental link time exceeds a few seconds.

## 3. The `dev` profile is already tuned

`Cargo.toml` enables:

- `opt-level = 0` (fast compile, no optimisation)
- `incremental = true`
- `split-debuginfo = "unpacked"` (keeps `debug = true` cost low)

So `cargo build` (no `--release`) is the right command for iteration.

## 4. `release-thin` for dogfood builds

For release-quality binaries that still build fast enough for daily
CI / pre-release dogfood, use the `release-thin` profile:

```bash
cargo build --profile release-thin -p corlinman-gateway
```

Trade-off: ~5% runtime perf vs the GA `release` profile, but 50%
faster to build.

## 5. CI cache hint

GitHub Actions cache key examples:

```yaml
- uses: actions/cache@v4
  with:
    path: |
      ~/.cargo/registry
      ~/.cargo/git
      target
      ~/Library/Caches/Mozilla.sccache
    key: cargo-${{ runner.os }}-${{ hashFiles('Cargo.lock') }}
```

## 6. Cross-compile cheat sheet

See `scripts/build-release.sh`. tl;dr: install `cross`
(`cargo install cross --git https://github.com/cross-rs/cross`),
then `cross build --release --target x86_64-unknown-linux-musl`.
The repo's `.cargo/config.toml` wires the linker per target.

## Troubleshooting

- **`error: linker 'clang' not found`** — install clang via your
  package manager or replace `linker = "clang"` in
  `.cargo/config.toml` with the linker you have. Native cargo
  defaults still work.
- **`-fuse-ld=mold: command not found`** — install mold; or drop
  the rustflags line under the affected `[target.*]` section.
- **sccache reports 0% hit rate** — first warm-up; or you switched
  `RUSTFLAGS` mid-cache-window (any new flag invalidates the
  cache key).
