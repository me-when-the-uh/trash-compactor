<#
Trash-Compactor build script.

Builds the fast_walk Rust extension and the single-file PyInstaller
executable (dist\trash-compactor.exe). Run from the repo root:

    powershell -ExecutionPolicy Bypass -File build.ps1

Every step writes its full output to a log in %TEMP% (tc-pip.log,
tc-maturin.log, tc-pyinstaller.log, tc-build-verify.log).  A failing step
prints the error-looking lines from its log, the tail of the log, and the
log path.

Verification runs the frozen executable and requires fast_walk=True and
probe_candidate_files_parallel=True in its diagnostic line.

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

# Native commands (cargo, maturin, pip) write progress to stderr. Under
# ErrorActionPreference=Stop that raises NativeCommandError, and exit codes are
# checked explicitly instead.
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Fail { param([int]$Code, [string]$Text) Write-Host "ERROR: $Text" -ForegroundColor Red; exit $Code }

function Show-Log {
    param([string]$Path, [int]$Lines = 25)
    if (-not (Test-Path $Path)) { return }
    $content = @(Get-Content $Path -ErrorAction SilentlyContinue)
    if ($content.Count -eq 0) { return }
    $errors = @($content |
        Select-String -Pattern '^\s*(ERROR|CRITICAL|FATAL|Traceback)|^\s*error(\[|:)' |
        Select-Object -First 10)
    if ($errors.Count -gt 0) {
        Write-Host "--- error lines from $Path ---" -ForegroundColor DarkGray
        foreach ($match in $errors) { Write-Host $match.Line -ForegroundColor DarkGray }
    }
    $tail = @($content | Select-Object -Last $Lines)
    Write-Host "--- last $($tail.Count) lines of $Path ---" -ForegroundColor DarkGray
    foreach ($line in $tail) { Write-Host $line -ForegroundColor DarkGray }
    Write-Host "---" -ForegroundColor DarkGray
}

Write-Step "Locating Python 3.11+ (64-bit)"
$pythonPath = $null
$candidates = @("py -3.13", "py -3.12", "py -3.14", "py -3.11", "python")
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
$pipLog = Join-Path $env:TEMP "tc-pip.log"
& $pythonPath -m pip install --upgrade pip *> $pipLog
if ($LASTEXITCODE -ne 0) {
    Show-Log $pipLog
    Fail 4 "pip upgrade failed (exit $LASTEXITCODE). Full output: $pipLog"
}
& $pythonPath -m pip install -r requirements.txt *> $pipLog
if ($LASTEXITCODE -ne 0) {
    Show-Log $pipLog
    Fail 4 "pip install -r requirements.txt failed (exit $LASTEXITCODE). Full output: $pipLog"
}

Write-Step "Building fast_walk Rust extension"
$buildLog = Join-Path $env:TEMP "tc-maturin.log"
Push-Location fast_walk
& $pythonPath -m maturin build --release --interpreter $pythonPath *> $buildLog
$maturinExit = $LASTEXITCODE
Pop-Location
if ($maturinExit -ne 0) {
    Show-Log $buildLog 30
    Fail 3 "Rust extension build failed (exit $maturinExit). Ensure Visual Studio Build Tools with the MSVC C++ workload are installed, then re-run. Full output: $buildLog"
}

Write-Step "Installing fast_walk wheel"
$wheel = Get-ChildItem fast_walk\target\wheels\fast_walk-*.whl -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $wheel) {
    Fail 5 "No wheel in fast_walk\target\wheels after the maturin build. Full output: $buildLog"
}
Write-Host "   Using: $($wheel.Name)"
& $pythonPath -m pip install --force-reinstall $wheel.FullName *> $pipLog
if ($LASTEXITCODE -ne 0) {
    Show-Log $pipLog
    Fail 5 "Installing $($wheel.Name) failed (exit $LASTEXITCODE). Full output: $pipLog"
}

Write-Step "Stopping any running Trash-Compactor processes"
Get-Process -Name "trash-compactor" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

Write-Step "Building the portable executable (PyInstaller)"
$pyiLog = Join-Path $env:TEMP "tc-pyinstaller.log"
& $pythonPath -m PyInstaller --clean --noconfirm trash-compactor.spec *> $pyiLog
if ($LASTEXITCODE -ne 0) {
    Show-Log $pyiLog 30
    Fail 6 "PyInstaller build failed (exit $LASTEXITCODE). Full output: $pyiLog"
}
$exe = Join-Path $root "dist\trash-compactor.exe"
if (-not (Test-Path $exe)) {
    Show-Log $pyiLog 30
    Fail 6 "PyInstaller exited 0 but $exe is missing. Full output: $pyiLog"
}

if (-not $SkipVerify) {
    Write-Step "Verifying frozen executable"
    $tmp = Join-Path $env:TEMP ("tc-build-verify-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path (Join-Path $tmp "sub") -Force | Out-Null
    1..10 | ForEach-Object { Set-Content -Path (Join-Path $tmp "sub\f$_.txt") -Value ("compressible text content " * 100) }
    $verifyLog = Join-Path $env:TEMP "tc-build-verify.log"
    $env:TRASH_COMPACTOR_DIAGNOSTIC = "1"
    cmd /c "`"$exe`" -d -y `"$tmp`" < nul" *> $verifyLog
    $code = $LASTEXITCODE
    Remove-Item Env:TRASH_COMPACTOR_DIAGNOSTIC -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    $out = if (Test-Path $verifyLog) { Get-Content $verifyLog -Raw -ErrorAction SilentlyContinue } else { "" }
    $diagOk = ($out -match "fast_walk=True")
    $probeOk = ($out -match "probe_candidate_files_parallel=True")
    if ($code -ne 0 -or -not $diagOk -or -not $probeOk) {
        Show-Log $verifyLog 30
        Fail 7 "Verification failed (exit $code, fast_walk=True: $diagOk, probe_candidate_files_parallel=True: $probeOk). Full output: $verifyLog"
    }
    Write-Host "   Verification passed: fast_walk=True, probe_candidate_files_parallel=True, exit 0"
} else {
    Write-Host "   Skipping verification (-SkipVerify)"
}

Write-Host ""
Write-Host "Build complete: $exe" -ForegroundColor Green
exit 0
