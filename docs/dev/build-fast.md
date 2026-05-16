# Faster Rust builds

corlinman's Rust workspace is 30+ crates; a cold `cargo build --release`
can spend 5–10 minutes mostly on link time and on recompiling unchanged
dependencies. This page describes the one-time toolchain setup that
brings dev-mode rebuilds under a minute and release builds to roughly
2–3 minutes.

The repo keeps [`.cargo/config.toml`](../../.cargo/config.toml) portable.
Optional acceleration tools are enabled from your shell environment or
per-host Cargo config so a fresh checkout does not require them.

## Local baseline captured on 2026-05-16

Host: Windows, PowerShell, Rust 1.95.0 (`x86_64-pc-windows-msvc`).

| Command | Clean target? | Elapsed seconds | Result | Notes |
| --- | --- | ---: | --- | --- |
| `.\scripts\rust-build-baseline.ps1 -Profile dev -Clean` | yes | 26.51 | fail | Stops in `numkong v7.6.0` build script while compiling the `usearch` transitive C dependency with MSVC `cl.exe`; first fatal path is `include/numkong/cast/serial.h(884): error C2059: syntax error: "if"`. |

The baseline script is still useful on Windows because it records elapsed time
even when the build fails. The failure is a local toolchain blocker, not a
change introduced by the script: `cargo build -p corlinman-vector` fails at the
same `numkong` dependency. A GNU-target probe with the installed MinGW GCC 8
also fails; with all x86 `NK_TARGET_*` SIMD probes disabled, GCC still hits an
internal compiler error in `numkong.c`.

Until the Windows C toolchain is upgraded or `numkong`/`usearch` changes, use
Linux/macOS or CI runners for full Rust baseline numbers, and treat Windows
results as "blocked before link" measurements.

## 1. Local build cache: `sccache`

`sccache` is optional. It caches `rustc` outputs by input hash, so it helps
most after the first build, across branch switches, and when CI restores the
cache. The repo does not hard-code `rustc-wrapper`; use environment variables
so fresh checkouts and cross builds never fail because `sccache` is missing.

Windows on this workstation:

```powershell
$env:CARGO_HOME = "E:\DevData\cargo"
$env:RUSTUP_HOME = "E:\DevData\rustup"
cargo install sccache --locked --root "E:\DevData\cargo-tools"
$env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
$env:SCCACHE_DIR = "E:\DevData\sccache"
$env:RUSTC_WRAPPER = "sccache"
sccache --show-stats
```

Linux/macOS:

```bash
cargo install sccache --locked
export RUSTC_WRAPPER=sccache
sccache --show-stats
```

Rollback:

```powershell
Remove-Item Env:\RUSTC_WRAPPER -ErrorAction SilentlyContinue
```

Task 2 validation on Windows confirmed `sccache` is invoked with
`RUSTC_WRAPPER=sccache`. With `SCCACHE_DIR=E:\DevData\sccache`, stats reported
`Cache location Local disk: "E:\\DevData\\sccache"`. The Rust build still stops
at the pre-existing `numkong` C compile blocker described above.

## 2. Faster linker

Linking is the bottleneck for the gateway crate. `mold` on Linux and
`lld` on macOS cut link time from ~30s to ~3s.

### Linux

```bash
sudo apt-get install -y mold clang   # debian/ubuntu
# or
sudo dnf install -y mold clang       # fedora
```

Enable `mold` from your shell or a per-host Cargo config, for example by
setting `RUSTFLAGS="-C link-arg=-fuse-ld=mold"` for local native builds. Do not
commit host-specific linker requirements to the repo config.

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

## Build command choice

Use `cargo build` for edit/compile/test loops.

Use `make rust-build-fast` when you need release-like binaries for local
dogfooding and do not need GA packaging. It uses `release-thin`, which keeps
ThinLTO but raises `codegen-units` to improve build latency.

Use `make build` or `cargo build --release -p corlinman-gateway -p corlinman-cli`
for production release checks. This path remains unchanged.

Task 3 validation on Windows used `mingw32-make` because `make` is not on this
PowerShell `PATH`. `mingw32-make -n build` still expands to the unchanged
production release command, followed by `uv sync --frozen --no-dev` and
`pnpm -C ui build`. `mingw32-make -n rust-build-fast` expands to
`cargo build --profile release-thin -p corlinman-gateway -p corlinman-cli`.
The actual `mingw32-make rust-build-fast` invocation reaches Cargo and then
stops at the same pre-existing `numkong v7.6.0` MSVC C compile blocker:
`include\numkong/cast/serial.h(884): error C2059: syntax error: "if"`.

## `release-check` profile

`release-check` is for local release-like compile checks when link time is the
bottleneck. It disables LTO, raises codegen units, keeps incremental builds on,
and keeps symbols. It is not a production packaging profile.

```powershell
cargo build --profile release-check -p corlinman-gateway -p corlinman-cli
```

Use this only for local validation that the two primary binaries still compile
in an optimized profile. Use `release` for GA artifacts and `release-thin` for
dogfood binaries.

Task 4 validation on Windows confirmed Cargo accepts the profile and writes
artifacts under `target\release-check\`, but the build does not complete on
this host. It stops at the pre-existing `numkong v7.6.0` MSVC C compile blocker
and also reports `Could not find protoc` from `corlinman-proto` because
`protoc` is not currently on `PATH` or `PROTOC`. No `release-check` binaries
were produced, so the `target\release-check\corlinman*.exe --help` smoke checks
could not run.

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
Keep target linker setup in the build image, CI runner, or per-host Cargo
config unless it is available to every fresh checkout.

## Troubleshooting

- **`error: linker 'clang' not found`** — install clang via your
  package manager or remove the local linker override from your shell
  environment or per-host Cargo config. Native cargo defaults still work.
- **`-fuse-ld=mold: command not found`** — install mold; or drop
  the rustflags line under the affected `[target.*]` section.
- **sccache reports 0% hit rate** — first warm-up; or you switched
  `RUSTFLAGS` mid-cache-window (any new flag invalidates the
  cache key).
