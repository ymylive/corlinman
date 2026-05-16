# Rust Build Speed Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce corlinman's Rust build latency without changing runtime behavior or weakening the existing release path.

**Architecture:** Treat build speed as build-system work, not product-code work. First capture reproducible timing and dependency data, then add opt-in local acceleration, align docs/scripts with the actual configuration, and finally tune CI/release build paths behind explicit profiles. Keep the GA `release` profile semantically stable unless a later task proves parity with tests and smoke checks.

**Tech Stack:** Rust 1.95.0, Cargo workspace, `.cargo/config.toml`, Cargo profiles, GitHub Actions, PowerShell, optional `sccache`, optional fast linker (`mold`/`lld` where supported), optional analysis tools (`cargo-bloat`, `cargo-llvm-lines`, `hyperfine`).

---

## Safety Rules

- Do not change Rust application logic in `rust/crates/**/src/**`.
- Do not make `sccache`, `mold`, `lld`, `cargo-bloat`, `cargo-llvm-lines`, or `hyperfine` required for a fresh checkout.
- Do not enable `panic = "abort"` for the existing `release` profile in this plan. It changes panic behavior and can affect observability and recovery.
- Do not use nightly-only `build-std` or `no_std` for this workspace. The project is a multi-crate gateway/CLI/service workspace on stable Rust; those techniques from the reference articles are size-specialized and too invasive here.
- Do not change `release` from the current GA profile until the validation task proves `cargo nextest`, smoke commands, and binary packaging still pass.
- Keep tool installs and caches off `C:` on this machine. Use `E:\DevData` for Cargo homes/caches and `E:\Programs` for installed tools when a tool supports explicit paths.

## Reference Material Applied

- `https://www.cnblogs.com/RioTian/p/19012013`: use Cargo profile knobs (`opt-level`, `lto`, `codegen-units`, `strip`) but avoid blindly applying size-first options that slow full builds.
- `https://blog.csdn.net/gitblog_00349/article/details/153716227`: adopt measurement-first workflow (`hyperfine`, `cargo-bloat`, `cargo-llvm-lines`) and prefer `thin` LTO / higher `codegen-units` for large projects when build time matters.
- `https://blog.crwen.top/2024/06/19/rust-volumn-optimize/`: keep `strip` and size analysis as release/package concerns, and treat `panic = "abort"` as behavior-affecting unless separately approved.

## Current Repo Findings

- Workspace root: `E:\DevData\Repos\corlinman`.
- Rust workspace members: `rust/crates/*`.
- Toolchain pin: `rust-toolchain.toml` uses Rust `1.95.0`.
- Current `Cargo.toml` profiles:
  - `release`: `opt-level = 3`, `lto = "thin"`, `codegen-units = 1`, `strip = "symbols"`.
  - `release-thin`: inherits `release`, sets `codegen-units = 16`, keeps `lto = "thin"`.
  - `dev`: `opt-level = 0`, `debug = true`, `incremental = true`, `split-debuginfo = "unpacked"`.
- Current `.cargo/config.toml` only contains comments. It does not actually set `rustc-wrapper`, linker, target directories, or target-specific rustflags.
- Current `docs/dev/build-fast.md` says some linker wiring already exists in `.cargo/config.toml`; that is inaccurate and must be corrected.
- On this host at plan time:
  - `cargo 1.95.0` and `rustc 1.95.0` are available.
  - `sccache`, `mold`, and `clang` are not installed.
- Current `Makefile build` uses `cargo build --release -p corlinman-gateway -p corlinman-cli`; this uses the slow GA profile, not the existing faster `release-thin` dogfood profile.

## File Structure

- Modify: `.cargo/config.toml`
  - Responsibility: keep default builds portable; document opt-in env vars; optionally add safe `[env]` defaults that redirect caches/target dirs off `C:` without requiring external tools.
- Modify: `Cargo.toml`
  - Responsibility: keep existing `release` stable; add one new opt-in profile for faster local release checks if measurement supports it.
- Modify: `Makefile`
  - Responsibility: expose explicit fast Rust build target without changing the existing `build` target's production semantics.
- Modify: `scripts/build-release.sh`
  - Responsibility: keep release packaging compatible while making profile choice visible and validated.
- Modify: `docs/dev/build-fast.md`
  - Responsibility: replace stale claims with exact Windows/Linux/macOS setup steps, measurements, rollback instructions, and safety notes.
- Create: `scripts/rust-build-baseline.ps1`
  - Responsibility: gather repeatable build timing and binary size metrics on Windows/PowerShell without deleting the normal `target/` directory.
- Create: `scripts/rust-build-baseline.sh`
  - Responsibility: gather the same metrics on Linux/macOS/GitHub Actions.
- Modify: `.github/workflows/ci.yml`
  - Responsibility: optionally add Rust cache and build-profile checks only after local validation; do not make optional local tools required.
- Modify: `docs/ci-status.md`
  - Responsibility: document CI build-speed changes and any skipped validation.

---

### Task 1: Capture Rust Build Baseline

**Files:**
- Create: `scripts/rust-build-baseline.ps1`
- Create: `scripts/rust-build-baseline.sh`
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Create the PowerShell baseline script**

Create `scripts/rust-build-baseline.ps1` with this content:

```powershell
param(
    [string]$Profile = "dev",
    [string[]]$Packages = @("corlinman-gateway", "corlinman-cli"),
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$TargetDir = Join-Path $RepoRoot ".target-baseline"
$Env:CARGO_TARGET_DIR = $TargetDir
$Env:CARGO_HOME = if ($Env:CARGO_HOME) { $Env:CARGO_HOME } else { "E:\DevData\cargo" }
$Env:RUSTUP_HOME = if ($Env:RUSTUP_HOME) { $Env:RUSTUP_HOME } else { "E:\DevData\rustup" }

Set-Location $RepoRoot

if ($Clean -and (Test-Path $TargetDir)) {
    Remove-Item -LiteralPath $TargetDir -Recurse -Force
}

$profileArgs = @()
if ($Profile -eq "release") {
    $profileArgs += "--release"
} elseif ($Profile -ne "dev") {
    $profileArgs += @("--profile", $Profile)
}

$packageArgs = @()
foreach ($package in $Packages) {
    $packageArgs += @("-p", $package)
}

$commandText = "cargo build $($profileArgs -join ' ') $($packageArgs -join ' ')".Trim()
Write-Host "Repo: $RepoRoot"
Write-Host "Target: $TargetDir"
Write-Host "Command: $commandText"

$timer = [System.Diagnostics.Stopwatch]::StartNew()
& cargo build @profileArgs @packageArgs
$exitCode = $LASTEXITCODE
$timer.Stop()

if ($exitCode -ne 0) {
    exit $exitCode
}

$profileDir = if ($Profile -eq "dev") { "debug" } elseif ($Profile -eq "release") { "release" } else { $Profile }
$binaryNames = @("corlinman-gateway.exe", "corlinman.exe", "corlinman-gateway", "corlinman")
$sizes = foreach ($name in $binaryNames) {
    $path = Join-Path (Join-Path $TargetDir $profileDir) $name
    if (Test-Path $path) {
        $item = Get-Item $path
        [PSCustomObject]@{
            Binary = $name
            Bytes = $item.Length
            MiB = [Math]::Round($item.Length / 1MB, 2)
        }
    }
}

Write-Host ("ElapsedSeconds: {0:N2}" -f $timer.Elapsed.TotalSeconds)
if ($sizes) {
    $sizes | Format-Table -AutoSize
}

if (Get-Command sccache -ErrorAction SilentlyContinue) {
    sccache --show-stats
}
```

- [ ] **Step 2: Create the Bash baseline script**

Create `scripts/rust-build-baseline.sh` with this content:

```bash
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
cargo build "${profile_args[@]}" "${package_args[@]}"
end="$(date +%s)"

profile_dir="$PROFILE"
if [[ "$PROFILE" == "dev" ]]; then
  profile_dir="debug"
elif [[ "$PROFILE" == "release" ]]; then
  profile_dir="release"
fi

echo "ElapsedSeconds: $((end - start))"
for binary in corlinman-gateway corlinman; do
  path="$CARGO_TARGET_DIR/$profile_dir/$binary"
  if [[ -f "$path" ]]; then
    ls -lh "$path"
  fi
done

if command -v sccache >/dev/null 2>&1; then
  sccache --show-stats
fi
```

- [ ] **Step 3: Make the Bash script executable**

Run:

```bash
chmod +x scripts/rust-build-baseline.sh
```

Expected: command exits with status `0`.

- [ ] **Step 4: Run a cold dev-profile baseline on Windows**

Run from `E:\DevData\Repos\corlinman`:

```powershell
.\scripts\rust-build-baseline.ps1 -Profile dev -Clean
```

Expected:
- `cargo build -p corlinman-gateway -p corlinman-cli` exits `0`.
- Output includes `ElapsedSeconds`.
- Output lists sizes for `corlinman-gateway.exe` and/or `corlinman.exe` when binaries are produced on Windows.

- [ ] **Step 5: Run a warm dev-profile baseline on Windows**

Run:

```powershell
.\scripts\rust-build-baseline.ps1 -Profile dev
```

Expected:
- Command exits `0`.
- `ElapsedSeconds` is lower than the cold run unless source files changed.

- [ ] **Step 6: Run a cold `release-thin` baseline**

Run:

```powershell
.\scripts\rust-build-baseline.ps1 -Profile release-thin -Clean
```

Expected:
- `cargo build --profile release-thin -p corlinman-gateway -p corlinman-cli` exits `0`.
- Output includes elapsed seconds and binary sizes.

- [ ] **Step 7: Document baseline results**

Append this section to `docs/dev/build-fast.md`, filling in the actual measured numbers from Steps 4-6:

```markdown
## Local baseline captured on 2026-05-15

Host: Windows, PowerShell, Rust 1.95.0

| Command | Clean target? | Elapsed seconds | Notes |
| --- | --- | ---: | --- |
| `.\scripts\rust-build-baseline.ps1 -Profile dev -Clean` | yes | paste the `ElapsedSeconds` value printed by the script | baseline dev cold build |
| `.\scripts\rust-build-baseline.ps1 -Profile dev` | no | paste the `ElapsedSeconds` value printed by the script | baseline dev warm build |
| `.\scripts\rust-build-baseline.ps1 -Profile release-thin -Clean` | yes | paste the `ElapsedSeconds` value printed by the script | faster release-quality dogfood build |
```

Before committing, replace each "paste the `ElapsedSeconds` value printed by the script" cell with the numeric value printed by the corresponding run.

- [ ] **Step 8: Commit the baseline tooling**

Run:

```bash
git add scripts/rust-build-baseline.ps1 scripts/rust-build-baseline.sh docs/dev/build-fast.md
git commit -m "chore(rust): add build baseline tooling"
```

Expected: commit succeeds and contains only the baseline scripts plus the measured documentation update.

---

### Task 2: Install Optional Local Build Cache Without Making It Required

**Files:**
- Modify: `.cargo/config.toml`
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Install `sccache` into `E:\DevData` on this Windows host**

Run:

```powershell
$env:CARGO_HOME = "E:\DevData\cargo"
$env:RUSTUP_HOME = "E:\DevData\rustup"
cargo install sccache --locked --root "E:\DevData\cargo-tools"
```

Expected:
- Command exits `0`.
- `E:\DevData\cargo-tools\bin\sccache.exe` exists.

- [ ] **Step 2: Verify `sccache` works without changing repo config**

Run:

```powershell
$env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
$env:RUSTC_WRAPPER = "sccache"
sccache --zero-stats
.\scripts\rust-build-baseline.ps1 -Profile dev -Clean
.\scripts\rust-build-baseline.ps1 -Profile dev
sccache --show-stats
```

Expected:
- Both builds exit `0`.
- `sccache --show-stats` reports cacheable compile requests.
- The second build shows cache hits or a materially lower elapsed time.

- [ ] **Step 3: Replace stale `.cargo/config.toml` comments with exact opt-in instructions**

Replace `.cargo/config.toml` with:

```toml
# Cargo build acceleration knobs for corlinman.
#
# Keep this file portable. A fresh checkout must build even when optional
# tools such as sccache, mold, lld, or clang are not installed.
#
# Local Windows setup:
#   $env:CARGO_HOME = "E:\DevData\cargo"
#   $env:RUSTUP_HOME = "E:\DevData\rustup"
#   cargo install sccache --locked --root "E:\DevData\cargo-tools"
#   $env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
#   $env:RUSTC_WRAPPER = "sccache"
#
# Local Linux/macOS setup:
#   cargo install sccache --locked
#   export RUSTC_WRAPPER=sccache
#
# Faster linkers are intentionally configured through environment variables
# or per-host Cargo config, not this repository file, because missing linkers
# break first-time builds and cross-rs containers.
#
# See docs/dev/build-fast.md for measured commands and rollback steps.
```

- [ ] **Step 4: Update `docs/dev/build-fast.md` cache setup**

Replace the `sccache` section in `docs/dev/build-fast.md` with:

```markdown
## 1. Local build cache: `sccache`

`sccache` is optional. It caches `rustc` outputs by input hash, so it helps
most after the first build, across branch switches, and when CI restores the
cache. The repo does not hard-code `rustc-wrapper`; use an environment variable
so fresh checkouts and cross builds never fail because `sccache` is missing.

Windows on this workstation:

```powershell
$env:CARGO_HOME = "E:\DevData\cargo"
$env:RUSTUP_HOME = "E:\DevData\rustup"
cargo install sccache --locked --root "E:\DevData\cargo-tools"
$env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
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
```

- [ ] **Step 5: Run default build without `sccache`**

Run:

```powershell
Remove-Item Env:\RUSTC_WRAPPER -ErrorAction SilentlyContinue
.\scripts\rust-build-baseline.ps1 -Profile dev
```

Expected:
- Command exits `0`.
- Build does not require `sccache`.

- [ ] **Step 6: Run opt-in build with `sccache`**

Run:

```powershell
$env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
$env:RUSTC_WRAPPER = "sccache"
.\scripts\rust-build-baseline.ps1 -Profile dev
```

Expected:
- Command exits `0`.
- `sccache --show-stats` shows cache activity.

- [ ] **Step 7: Commit the cache documentation**

Run:

```bash
git add .cargo/config.toml docs/dev/build-fast.md
git commit -m "docs(rust): document opt-in sccache builds"
```

Expected: commit succeeds and no Rust source files are staged.

---

### Task 3: Add a Safe Fast Build Target

**Files:**
- Modify: `Makefile`
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Add a `rust-build-fast` target to `Makefile`**

Modify the `.PHONY` line:

```makefile
.PHONY: help dev build rust-build-fast test lint fmt proto docker ci clean
```

Add this help line under `help`:

```makefile
	@echo "  rust-build-fast  Rust dogfood build using the faster release-thin profile"
```

Add this target after `build`:

```makefile
rust-build-fast:
	cargo build --profile release-thin -p corlinman-gateway -p corlinman-cli
```

- [ ] **Step 2: Keep the existing production `build` target unchanged**

Verify `Makefile` still contains:

```makefile
build:
	cargo build --release -p corlinman-gateway -p corlinman-cli
	uv sync --frozen --no-dev
	pnpm -C ui build
```

Expected: the production build command remains `--release`.

- [ ] **Step 3: Document when to use each build command**

Append this to `docs/dev/build-fast.md`:

```markdown
## Build command choice

Use `cargo build` for edit/compile/test loops.

Use `make rust-build-fast` when you need release-like binaries for local
dogfooding and do not need GA packaging. It uses `release-thin`, which keeps
ThinLTO but raises `codegen-units` to improve build latency.

Use `make build` or `cargo build --release -p corlinman-gateway -p corlinman-cli`
for production release checks. This path remains unchanged.
```

- [ ] **Step 4: Verify fast target**

Run:

```powershell
make rust-build-fast
```

Expected:
- `cargo build --profile release-thin -p corlinman-gateway -p corlinman-cli` exits `0`.

- [ ] **Step 5: Verify production target still starts with GA release build**

Run:

```powershell
make -n build
```

Expected output includes:

```text
cargo build --release -p corlinman-gateway -p corlinman-cli
uv sync --frozen --no-dev
pnpm -C ui build
```

- [ ] **Step 6: Commit the fast target**

Run:

```bash
git add Makefile docs/dev/build-fast.md
git commit -m "chore(rust): add fast dogfood build target"
```

Expected: commit succeeds.

---

### Task 4: Add an Opt-In Faster Release-Check Profile

**Files:**
- Modify: `Cargo.toml`
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Add a `release-check` profile**

Add this block after `[profile.release-thin]` in `Cargo.toml`:

```toml
# Fast local release checks. This profile is intentionally opt-in:
# it prioritizes compile latency over final binary size/runtime.
# Do not use it for GA artifacts.
[profile.release-check]
inherits = "release"
debug = 1
lto = false
codegen-units = 256
incremental = true
strip = "none"
```

Rationale:
- `lto = false` reduces link-time optimization work.
- `codegen-units = 256` maximizes parallel codegen for local checks.
- `incremental = true` helps repeated release-like builds.
- `debug = 1` keeps enough symbols for useful local stack traces.
- `strip = "none"` avoids hiding local debug context.
- The existing `release` and `release-thin` profiles remain unchanged.

- [ ] **Step 2: Verify Cargo accepts the profile**

Run:

```powershell
cargo build --profile release-check -p corlinman-gateway -p corlinman-cli
```

Expected:
- Command exits `0`.
- Cargo writes artifacts under `target\release-check\`.

- [ ] **Step 3: Run smoke tests against the release-check build**

Run:

```powershell
.\target\release-check\corlinman.exe --help
.\target\release-check\corlinman-gateway.exe --help
```

Expected:
- Both commands exit `0`.
- Help text prints without panics.

- [ ] **Step 4: Document the profile**

Append this to `docs/dev/build-fast.md`:

```markdown
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
```

- [ ] **Step 5: Commit the profile**

Run:

```bash
git add Cargo.toml docs/dev/build-fast.md
git commit -m "chore(rust): add fast release-check profile"
```

Expected: commit succeeds and includes no `Cargo.lock` change.

---

### Task 5: Fix Release Script Profile Visibility

**Files:**
- Modify: `scripts/build-release.sh`
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Reject `release-check` in release packaging**

In `scripts/build-release.sh`, after parsing args and before building targets, add:

```bash
if [[ "$PROFILE" == "release-check" ]]; then
    echo "release-check is a local validation profile and must not be packaged" >&2
    exit 1
fi
```

Place it immediately after the block that sets default `TARGETS`.

- [ ] **Step 2: Verify normal release script still works in dry profile mode**

Run:

```powershell
bash scripts/build-release.sh --profile release-thin macos-aarch64
```

Expected on non-macOS:
- If the host cannot build `aarch64-apple-darwin`, the command may fail with a target/linker availability error after argument parsing.
- It must not fail because of the new `release-check` guard.

Expected on macOS:
- Command exits `0` and produces `dist/corlinman-*-aarch64-apple-darwin.tar.gz`.

- [ ] **Step 3: Verify `release-check` is blocked**

Run:

```powershell
bash scripts/build-release.sh --profile release-check macos-aarch64
```

Expected:
- Command exits non-zero.
- Output includes:

```text
release-check is a local validation profile and must not be packaged
```

- [ ] **Step 4: Document the packaging guard**

Append this to `docs/dev/build-fast.md`:

```markdown
`scripts/build-release.sh` rejects `--profile release-check` on purpose.
That profile is for local compile validation only and must not produce
operator-facing archives.
```

- [ ] **Step 5: Commit the script guard**

Run:

```bash
git add scripts/build-release.sh docs/dev/build-fast.md
git commit -m "chore(release): block local release-check packaging"
```

Expected: commit succeeds.

---

### Task 6: Tune CI Cache Without Requiring Local-Only Tools

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/ci-status.md`

- [ ] **Step 1: Keep `Swatinem/rust-cache@v2` for clippy and nextest**

Verify `.github/workflows/ci.yml` still contains this block in both `rust-clippy` and `rust-test`:

```yaml
      - name: Cache cargo
        uses: Swatinem/rust-cache@v2
        with:
          shared-key: rust-ci
          cache-on-failure: true
```

Expected: both Rust jobs already cache Cargo artifacts.

- [ ] **Step 2: Add a non-gating release-check compile job only if CI latency needs it**

If PR feedback needs a release-like compile gate, add this job under `jobs`:

```yaml
  rust-release-check:
    name: rust-release-check
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4

      - name: Install protobuf-compiler
        run: sudo apt-get update && sudo apt-get install -y protobuf-compiler pkg-config libssl-dev

      - name: Install Rust (from rust-toolchain.toml)
        uses: dtolnay/rust-toolchain@master
        with:
          toolchain: stable

      - name: Cache cargo
        uses: Swatinem/rust-cache@v2
        with:
          shared-key: rust-release-check
          cache-on-failure: true

      - name: cargo build release-check
        run: cargo build --profile release-check -p corlinman-gateway -p corlinman-cli
```

Rationale:
- `continue-on-error: true` prevents a new performance-oriented check from blocking product work while it is being evaluated.
- No `sccache`, `mold`, or `lld` requirement is added.

- [ ] **Step 3: Document CI choice**

If the job is added, append this to `docs/ci-status.md`:

```markdown
### rust-release-check — advisory

Added as a non-gating compile-latency signal. It builds the two primary Rust
binaries with `--profile release-check`, reusing `Swatinem/rust-cache@v2`.
It does not replace the normal `rust-clippy`, `rust-test`, or release jobs,
and it does not produce operator artifacts.
```

If the job is not added, append this instead:

```markdown
### rust-release-check — not added

Skipped for now. Existing `rust-clippy` and `rust-test` jobs already use
`Swatinem/rust-cache@v2`, and local `release-check` measurement is sufficient
until CI timing shows a release-like compile gate is needed.
```

- [ ] **Step 4: Validate workflow syntax by running local dry checks**

Run:

```powershell
git diff -- .github/workflows/ci.yml docs/ci-status.md
```

Expected:
- Diff shows only the intended advisory job or the explicit skipped note.
- No required CI job is removed.

- [ ] **Step 5: Commit CI documentation or job**

If the job was added, run:

```bash
git add .github/workflows/ci.yml docs/ci-status.md
git commit -m "ci(rust): add advisory release-check build"
```

If only documentation was added, run:

```bash
git add docs/ci-status.md
git commit -m "docs(ci): document rust release-check decision"
```

Expected: commit succeeds.

---

### Task 7: Analyze Heavy Dependencies Without Changing Behavior

**Files:**
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Install analysis tools into `E:\DevData`**

Run:

```powershell
$env:CARGO_HOME = "E:\DevData\cargo"
$env:RUSTUP_HOME = "E:\DevData\rustup"
cargo install cargo-bloat cargo-llvm-lines --locked --root "E:\DevData\cargo-tools"
```

Expected:
- Command exits `0`.
- `E:\DevData\cargo-tools\bin\cargo-bloat.exe` exists.
- `E:\DevData\cargo-tools\bin\cargo-llvm-lines.exe` exists.

- [ ] **Step 2: Capture binary-size dependency data**

Run:

```powershell
$env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
cargo bloat --release --crates --bin corlinman-gateway
cargo bloat --release --crates --bin corlinman
```

Expected:
- Both commands exit `0`.
- Output lists crate-level size contribution.

- [ ] **Step 3: Capture compile-codegen hotspot data**

Run:

```powershell
$env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
cargo llvm-lines --release --bin corlinman-gateway
cargo llvm-lines --release --bin corlinman
```

Expected:
- Both commands exit `0`.
- Output lists functions/modules with large generated LLVM IR.

- [ ] **Step 4: Document findings without changing dependencies**

Append this section to `docs/dev/build-fast.md`:

```markdown
## Dependency and codegen observations

Captured with:

```powershell
cargo bloat --release --crates --bin corlinman-gateway
cargo bloat --release --crates --bin corlinman
cargo llvm-lines --release --bin corlinman-gateway
cargo llvm-lines --release --bin corlinman
```

Top size/codegen contributors:

| Binary | Tool | Contributor | Observation | Action |
| --- | --- | --- | --- | --- |
| corlinman-gateway | cargo-bloat | paste the top crate from output | paste its reported size/share | observe only |
| corlinman | cargo-bloat | paste the top crate from output | paste its reported size/share | observe only |
| corlinman-gateway | cargo-llvm-lines | paste the largest module or function from output | paste its reported line count/share | observe only |
| corlinman | cargo-llvm-lines | paste the largest module or function from output | paste its reported line count/share | observe only |
```

Before committing, replace the "paste ..." cells with measured output. Do not remove or feature-gate dependencies in this task.

- [ ] **Step 5: Commit analysis notes**

Run:

```bash
git add docs/dev/build-fast.md
git commit -m "docs(rust): record build hotspot analysis"
```

Expected: commit succeeds and no dependency files change.

---

### Task 8: Validate Existing Functionality

**Files:**
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Run Rust formatting check**

Run:

```powershell
cargo fmt --all -- --check
```

Expected:
- Command exits `0`.
- If it fails due to pre-existing formatting drift, record the failing files in `docs/dev/build-fast.md` and do not reformat unrelated code in this plan.

- [ ] **Step 2: Run Rust clippy**

Run:

```powershell
cargo clippy --workspace --all-targets -- -D warnings
```

Expected:
- Command exits `0`.
- If it fails due to pre-existing lint drift, record the exact lint names and files in `docs/dev/build-fast.md`.

- [ ] **Step 3: Run Rust tests**

Run:

```powershell
cargo nextest run --workspace
```

Expected:
- Command exits `0`.
- If `cargo-nextest` is missing, install it off `C:`:

```powershell
$env:CARGO_HOME = "E:\DevData\cargo"
$env:RUSTUP_HOME = "E:\DevData\rustup"
cargo install cargo-nextest --locked --root "E:\DevData\cargo-tools"
$env:Path = "E:\DevData\cargo-tools\bin;$env:Path"
cargo nextest run --workspace
```

- [ ] **Step 4: Run the two primary binary smoke checks**

Run:

```powershell
cargo build --profile release-thin -p corlinman-gateway -p corlinman-cli
.\target\release-thin\corlinman.exe --help
.\target\release-thin\corlinman-gateway.exe --help
```

Expected:
- Build exits `0`.
- Both help commands exit `0`.

- [ ] **Step 5: Run production release compile**

Run:

```powershell
cargo build --release -p corlinman-gateway -p corlinman-cli
```

Expected:
- Command exits `0`.
- This proves the unchanged GA release profile still builds.

- [ ] **Step 6: Record validation results**

Append this to `docs/dev/build-fast.md`:

```markdown
## Validation after build-speed changes

| Command | Result | Notes |
| --- | --- | --- |
| `cargo fmt --all -- --check` | write `pass` or `fail` from the run | paste the exact failure summary or `exits 0` |
| `cargo clippy --workspace --all-targets -- -D warnings` | write `pass` or `fail` from the run | paste the exact failure summary or `exits 0` |
| `cargo nextest run --workspace` | write `pass` or `fail` from the run | paste the exact failure summary or `exits 0` |
| `cargo build --profile release-thin -p corlinman-gateway -p corlinman-cli` | write `pass` or `fail` from the run | paste the exact failure summary or `exits 0` |
| `.\target\release-thin\corlinman.exe --help` | write `pass` or `fail` from the run | paste the exact failure summary or `exits 0` |
| `.\target\release-thin\corlinman-gateway.exe --help` | write `pass` or `fail` from the run | paste the exact failure summary or `exits 0` |
| `cargo build --release -p corlinman-gateway -p corlinman-cli` | write `pass` or `fail` from the run | paste the exact failure summary or `exits 0` |
```

Before committing, replace each instruction cell with the observed result and exact note from the corresponding command.

- [ ] **Step 7: Commit validation notes**

Run:

```bash
git add docs/dev/build-fast.md
git commit -m "docs(rust): record build optimization validation"
```

Expected: commit succeeds.

---

### Task 9: Final Review and Rollback Instructions

**Files:**
- Modify: `docs/dev/build-fast.md`

- [ ] **Step 1: Add rollback section**

Append this to `docs/dev/build-fast.md`:

```markdown
## Rollback

All build-speed changes in this plan are build-system only.

To disable `sccache` for the current PowerShell session:

```powershell
Remove-Item Env:\RUSTC_WRAPPER -ErrorAction SilentlyContinue
```

To stop using the local validation profile, switch back to:

```powershell
cargo build
cargo build --release -p corlinman-gateway -p corlinman-cli
```

To remove local baseline artifacts:

```powershell
Remove-Item -LiteralPath .target-baseline -Recurse -Force
```

Production release behavior remains controlled by the existing `release`
profile and `make build`.
```

- [ ] **Step 2: Inspect staged diff for behavior changes**

Run:

```powershell
git diff --stat
git diff -- rust/crates Cargo.toml .cargo/config.toml Makefile scripts docs .github/workflows
```

Expected:
- No changes under `rust/crates/**/src/**`.
- `Cargo.toml` changes are limited to adding `[profile.release-check]`.
- Existing `[profile.release]`, `[profile.release-thin]`, and `[profile.dev]` remain unchanged.

- [ ] **Step 3: Search for disallowed high-risk settings**

Run:

```powershell
rg -n 'panic\s*=\s*"abort"|build-std|no_std|lto\s*=\s*"fat"|opt-level\s*=\s*"z"' Cargo.toml .cargo docs scripts .github
```

Expected:
- Matches may appear in documentation as rejected or reference-only options.
- No production profile or release script uses these settings as part of this plan.

- [ ] **Step 4: Commit final docs**

Run:

```bash
git add docs/dev/build-fast.md
git commit -m "docs(rust): add build-speed rollback notes"
```

Expected: commit succeeds.

- [ ] **Step 5: Final summary command**

Run:

```powershell
git log --oneline -n 9
```

Expected:
- The latest commits correspond to Tasks 1-9.
- The summary can be pasted into a PR description.

---

## Not In This Plan

- No `panic = "abort"` change for the existing `release` profile.
- No nightly `build-std`.
- No `no_std`.
- No dependency removal or feature-gating without a separate behavior test plan.
- No hard-coded linker or `rustc-wrapper` in repo config.
- No changes to runtime Rust source files.

## Acceptance Criteria

- Fresh checkout still builds without `sccache`, `mold`, `lld`, `clang`, `cargo-bloat`, `cargo-llvm-lines`, or `hyperfine`.
- `cargo build` still uses the existing dev profile.
- `make build` still uses `cargo build --release -p corlinman-gateway -p corlinman-cli`.
- `make rust-build-fast` provides a documented faster dogfood path.
- `cargo build --profile release-check -p corlinman-gateway -p corlinman-cli` works as an opt-in local validation path.
- Existing Rust tests and primary binary smoke checks either pass or have documented pre-existing failures.
- No changes are made under `rust/crates/**/src/**`.
- Documentation contains exact measured baseline and validation results.
