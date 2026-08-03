<#
Trash-Compactor build script.

Builds the fast_walk Rust extension and the single-file PyInstaller
executable (dist\trash-compactor.exe). Run from the repo root:

    powershell -ExecutionPolicy Bypass -File build.ps1

Exit codes:
  0  success
  1  Python 3.11+ (64-bit) not found
  2  Rust toolchain not found
  3  MSVC linker missing (VS Build Tools) or extension build failed
  4  Python dependency install failed
  5  fast_walk wheel build/install failed
  6  PyInstaller build failed
  7  frozen executable verification failed
#>
param([switch]$SkipVerify)

# Native commands (cargo, maturin, pip) write progress to stderr; under
# ErrorActionPreference=Stop that raises NativeCommandError.  We check
# $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Fail { param([int]$Code, [string]$Text) Write-Host "ERROR: $Text" -ForegroundColor Red; exit $Code }

Write-Step "Locating Python 3.11+ (64-bit)"
$pythonPath = $null
$candidates = @("python", "py -3.13", "py -3.12", "py -3.11")
$probe = @"
import sys, struct
ok = sys.version_info >= (3, 11) and struct.calcsize('P') == 8
venv = getattr(sys, 'base_prefix', sys.prefix) != sys.prefix
print(sys.executable if ok and not venv else '')
"@
foreach ($cand in $candidates) {
    $parts = $cand.Split(" ")
    if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
    $args = @()
    if ($parts.Length -gt 1) { $args += $parts[1] }
    $args += @("-c", $probe)
    $out = & $parts[0] @args 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { $pythonPath = $out.Trim(); break }
}
if (-not $pythonPath) {
    Fail 1 "Python 3.11+ (64-bit) was not found on PATH. Install from https://www.python.org/downloads/windows/ and re-run."
}
Write-Host "   Using: $pythonPath"

Write-Step "Locating Rust toolchain"
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    Fail 2 "cargo was not found on PATH. Install rustup (MSVC toolchain) from https://rustup.rs and re-run."
}
Write-Host "   cargo: $((Get-Command cargo).Source)"

Write-Step "Installing Python dependencies"
& $pythonPath -m pip install --upgrade pip 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 4 "pip upgrade failed." }
& $pythonPath -m pip install -r requirements.txt 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 4 "pip install failed; check requirements.txt." }

Write-Step "Building fast_walk Rust extension"
$buildLog = Join-Path $env:TEMP "tc-maturin.log"
Push-Location fast_walk
& $pythonPath -m maturin build --release --interpreter $pythonPath *> $buildLog
$maturinExit = $LASTEXITCODE
Pop-Location
if ($maturinExit -ne 0) {
    Get-Content $buildLog | Select-Object -Last 15
    Fail 3 "Rust extension build failed (exit $maturinExit). Ensure Visual Studio Build Tools with the MSVC C++ workload are installed, then re-run."
}

Write-Step "Installing fast_walk wheel"
$wheel = Get-ChildItem fast_walk\target\wheels\fast_walk-*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $wheel) { Fail 5 "No wheel produced by maturin." }
& $pythonPath -m pip install --force-reinstall $wheel.FullName 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 5 "Wheel install failed." }

Write-Step "Stopping any running Trash-Compactor processes"
Get-Process -Name "trash-compactor" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

Write-Step "Building single-file executable (PyInstaller)"
& $pythonPath -m PyInstaller --clean --noconfirm trash-compactor.spec 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 6 "PyInstaller build failed." }
$exe = Join-Path $root "dist\trash-compactor.exe"
if (-not (Test-Path $exe)) { Fail 6 "Executable not found at $exe." }

if (-not $SkipVerify) {
    Write-Step "Verifying frozen executable"
    $tmp = Join-Path $env:TEMP ("tc-build-verify-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path (Join-Path $tmp "sub") -Force | Out-Null
    1..10 | ForEach-Object { Set-Content -Path (Join-Path $tmp "sub\f$_.txt") -Value ("compressible text content " * 100) }
    $env:TRASH_COMPACTOR_DIAGNOSTIC = "1"
    $out = cmd /c "`"$exe`" -d -y `"$tmp`" < nul 2>&1"
    $code = $LASTEXITCODE
    Remove-Item Env:TRASH_COMPACTOR_DIAGNOSTIC -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    $diagOk = ($out -match "fast_walk=True")
    if ($code -ne 0 -or -not $diagOk) { Fail 7 "Verification failed (exit $code, fast_walk=True: $diagOk)." }
    Write-Host "   Verification passed: fast_walk=True, exit 0"
} else {
    Write-Host "   Skipping verification (-SkipVerify)"
}

Write-Host ""
Write-Host "Build complete: $exe" -ForegroundColor Green
exit 0
