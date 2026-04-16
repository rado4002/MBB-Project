<#
.SYNOPSIS
    MBB ya Kin — Resilience Test Runner (Windows / PowerShell)

.DESCRIPTION
    Starts the Docker stack with Toxiproxy, runs both resilience test suites,
    and prints a combined summary.

    Suite 1: test_resilience.py — blackout queue unit tests (no Docker required)
    Suite 2: test_chaos.py      — Docker chaos scenarios (Redis kill, PG kill,
                                  worker crash, slow 3G, payload size, drain)

.PARAMETER Scenarios
    Comma-separated chaos scenarios to run (default: all).
    Examples: "S1,S4,S5"  "S4"  "all"

.PARAMETER Jwt
    Bearer JWT token for authenticated endpoints.
    If omitted, unauthenticated requests are used (works when auth is optional in dev).

.PARAMETER Project
    Docker Compose project name (default: "bot" — folder name, lower-cased).
    Check with: docker compose ls

.PARAMETER SkipBuild
    Skip --build flag on docker compose up (use cached images).

.PARAMETER SkipStack
    Skip starting the Docker stack (use if already running).

.PARAMETER VerboseOutput
    Pass --verbose to test_chaos.py for detailed output.

.EXAMPLE
    # Full run (builds images, runs all scenarios):
    .\scripts\run_resilience_tests.ps1

.EXAMPLE
    # Slow-3G and payload checks only, no rebuild:
    .\scripts\run_resilience_tests.ps1 -Scenarios S4,S5 -SkipBuild

.EXAMPLE
    # Skip starting stack (already running), all scenarios:
    .\scripts\run_resilience_tests.ps1 -SkipStack -VerboseOutput
#>

[CmdletBinding()]
param(
    [string]$Scenarios  = "all",
    [string]$Jwt        = "",
    [string]$Project    = "bot",
    [switch]$SkipBuild,
    [switch]$SkipStack,
    [switch]$VerboseOutput
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Paths ─────────────────────────────────────────────────────────────────────

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$Backend    = Join-Path $RepoRoot "backend"
$ComposeBase        = Join-Path $RepoRoot "docker-compose.yml"
$ComposeDev         = Join-Path $RepoRoot "docker-compose.dev.yml"
$ComposeResilience  = Join-Path $RepoRoot "docker-compose.resilience.yml"

# ── Helper functions ──────────────────────────────────────────────────────────

function Write-Header {
    param([string]$Text)
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor Cyan
}

function Write-Step {
    param([string]$Text)
    Write-Host "`n>> $Text" -ForegroundColor Yellow
}

function Invoke-Cmd {
    param([string[]]$CmdArgs, [string]$WorkDir = $RepoRoot)
    Push-Location $WorkDir
    try {
        & $CmdArgs[0] $CmdArgs[1..($CmdArgs.Length - 1)]
        if ($LASTEXITCODE -ne 0) {
            $cmdStr = $CmdArgs -join ' '
            throw "Command exited with code ${LASTEXITCODE}: ${cmdStr}"
        }
    } finally {
        Pop-Location
    }
}

function Wait-ApiReady {
    param([int]$TimeoutSec = 60)
    Write-Host "  Waiting for API to be ready (up to ${TimeoutSec}s)..." -ForegroundColor Gray
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:8000/health" -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($r.StatusCode -eq 200) {
                Write-Host "  API ready ✓" -ForegroundColor Green
                return $true
            }
        } catch { }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Wait-ToxiproxyReady {
    param([int]$TimeoutSec = 30)
    Write-Host "  Waiting for Toxiproxy (up to ${TimeoutSec}s)..." -ForegroundColor Gray
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:8474/version" -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($r.StatusCode -eq 200) {
                Write-Host "  Toxiproxy ready ✓" -ForegroundColor Green
                return $true
            }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

# ── Validate prerequisites ────────────────────────────────────────────────────

Write-Header "MBB ya Kin — Resilience Test Runner"

Write-Step "Checking prerequisites..."

# Docker
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker not found in PATH. Install Docker Desktop."
    exit 1
}
Write-Host "  docker: OK" -ForegroundColor Green

# Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "python not found in PATH."
    exit 1
}
Write-Host "  python: OK" -ForegroundColor Green

# requests package
$reqCheck = & python -c "import requests, redis; print('OK')" 2>&1
if ($reqCheck -notmatch "OK") {
    Write-Step "Installing test dependencies (requests, redis)..."
    Invoke-Cmd @("pip", "install", "--quiet", "requests", "redis") -WorkDir $Backend
}
Write-Host "  requests + redis: OK" -ForegroundColor Green

# Compose files exist
foreach ($f in @($ComposeBase, $ComposeDev, $ComposeResilience)) {
    if (-not (Test-Path $f)) {
        Write-Error "Missing compose file: $f"
        exit 1
    }
}
Write-Host "  compose files: OK" -ForegroundColor Green

# ── Start Docker stack ────────────────────────────────────────────────────────

if (-not $SkipStack) {
    Write-Step "Starting Docker stack (base + dev + resilience)..."

    $buildFlag = if ($SkipBuild) { @() } else { @("--build") }

    Invoke-Cmd (
        @("docker", "compose",
          "-f", $ComposeBase,
          "-f", $ComposeDev,
          "-f", $ComposeResilience,
          "-p", $Project,
          "up", "-d") + $buildFlag
    ) -WorkDir $RepoRoot

    Write-Host "  Stack started ✓" -ForegroundColor Green
} else {
    Write-Host "  --SkipStack: assuming stack already running" -ForegroundColor Gray
}

# ── Wait for readiness ────────────────────────────────────────────────────────

Write-Step "Waiting for services..."

if (-not (Wait-ApiReady -TimeoutSec 90)) {
    Write-Error "API did not become ready within 90s. Check: docker compose logs api"
    exit 1
}

$toxiAvailable = Wait-ToxiproxyReady -TimeoutSec 30
if (-not $toxiAvailable) {
    Write-Warning "Toxiproxy not ready — S4 (slow 3G) will be skipped."
}

# ── Suite 1: Blackout Queue Unit Tests ───────────────────────────────────────

Write-Header "Suite 1: Blackout Queue Unit Tests (test_resilience.py)"

$suite1Exit = 0
Push-Location $Backend
try {
    & python tests/test_resilience.py
    $suite1Exit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($suite1Exit -eq 0) {
    Write-Host "`n  Suite 1: PASSED ✓" -ForegroundColor Green
} else {
    Write-Host "`n  Suite 1: FAILED ✗" -ForegroundColor Red
}

# ── Suite 2: Docker Chaos Tests ───────────────────────────────────────────────

Write-Header "Suite 2: Docker Chaos Tests (test_chaos.py)"

$chaosArgs = @(
    "tests/test_chaos.py",
    "--scenarios", $Scenarios,
    "--project", $Project
)
if ($Jwt)           { $chaosArgs += @("--jwt", $Jwt) }
if ($VerboseOutput) { $chaosArgs += "--verbose" }

$suite2Exit = 0
Push-Location $Backend
try {
    & python @chaosArgs
    $suite2Exit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($suite2Exit -eq 0) {
    Write-Host "`n  Suite 2: PASSED ✓" -ForegroundColor Green
} else {
    Write-Host "`n  Suite 2: FAILED ✗" -ForegroundColor Red
}

# ── Final summary ─────────────────────────────────────────────────────────────

Write-Header "Combined Result"

$overallExit = if (($suite1Exit -eq 0) -and ($suite2Exit -eq 0)) { 0 } else { 1 }

if ($overallExit -eq 0) {
    Write-Host "  ALL SUITES PASSED — system is Kinshasa-resilient ✓`n" -ForegroundColor Green
} else {
    Write-Host "  SOME CHECKS FAILED — review output above`n" -ForegroundColor Red
    Write-Host "  Useful debug commands:" -ForegroundColor Yellow
    Write-Host "    docker compose -p $Project logs api --tail=50"
    Write-Host "    docker compose -p $Project logs celery_worker --tail=50"
    Write-Host "    docker compose -p $Project ps"
}

exit $overallExit
