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

Write-Host ("ElapsedSeconds: {0:N2}" -f $timer.Elapsed.TotalSeconds)

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

if ($sizes) {
    $sizes | Format-Table -AutoSize
}

if (Get-Command sccache -ErrorAction SilentlyContinue) {
    sccache --show-stats
}
