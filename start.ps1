<#
.SYNOPSIS
  Starts everything Snipmint needs in one go and opens the app.

.DESCRIPTION
  1. Starts Docker Desktop if it is not running and waits for it.
  2. Starts Snipmint (+ n8n and its database, if n8n is set up in .env).
  3. Waits until they answer, then opens Snipmint in your browser with the access token.

  n8n keeps its workflows, credentials and history between runs, so there is nothing to set up again.

.PARAMETER Build
  Rebuild the Snipmint image first. Use it after updating the code (git pull).

.PARAMETER NoBrowser
  Do not open the browser.

.PARAMETER NoN8n
  Start Snipmint only.

.EXAMPLE
  .\start.ps1
  .\start.ps1 -Build
#>
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$NoBrowser,
    [switch]$NoN8n
)

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

function Test-Docker {
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

function Test-Url($url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Wait-Until($what, [scriptblock]$check, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $check) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail 'Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/'
}

# ---- 1. Docker Desktop
Step 'Docker Desktop'
if (Test-Docker) {
    Ok 'already running'
} else {
    $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $exe)) { Fail "Docker Desktop is not running and was not found at $exe. Start it yourself, then run this again." }
    Write-Host '    starting it (this takes up to a minute or two)...'
    Start-Process -FilePath $exe | Out-Null
    if (-not (Wait-Until 'Docker' { Test-Docker } 240)) {
        Fail 'Docker did not become ready in 4 minutes. Open Docker Desktop, wait until it says "running", then run this again.'
    }
    Ok 'ready'
}

# ---- 2. Snipmint (+ n8n)
$files = @('-f', 'docker-compose.yml')
$useN8n = $false
if (-not $NoN8n -and (Test-Path 'docker-compose.n8n.yml') -and (Test-Path '.env')) {
    $env_text = Get-Content '.env' -Raw
    if ($env_text -match '(?m)^N8N_DB_PASSWORD=\S+' -and $env_text -match '(?m)^N8N_ENCRYPTION_KEY=\S+') { $useN8n = $true }
}
if ($useN8n) { $files += @('-f', 'docker-compose.n8n.yml') }

$what = 'Snipmint'
if ($useN8n) { $what = 'Snipmint + n8n' }
Step "Starting $what"
$upArgs = @('compose') + $files + @('up', '-d')
if ($Build) { $upArgs += '--build' }
& docker @upArgs
if ($LASTEXITCODE -ne 0) { Fail 'docker compose failed. Scroll up for the reason.' }

# ---- 3. Wait until they answer
Step 'Waiting for Snipmint'
if (-not (Wait-Until 'Snipmint' { Test-Url 'http://localhost:8000/api/health' } 180)) {
    Fail 'Snipmint did not answer on http://localhost:8000. See: docker compose logs snipmint'
}
Ok 'Snipmint is up'

if ($useN8n) {
    Step 'Waiting for n8n'
    if (Wait-Until 'n8n' { Test-Url 'http://localhost:5678/healthz' } 180) { Ok 'n8n is up' }
    else { Write-Host '    n8n is not answering yet. Snipmint still works; check: docker compose logs n8n' -ForegroundColor Yellow }
}

# ---- 4. Open the app with its access token
$token = $null
if (Test-Path '.env') {
    $m = Select-String -Path '.env' -Pattern '^SNIPMINT_TOKEN=(\S+)' | Select-Object -First 1
    if ($m) { $token = $m.Matches[0].Groups[1].Value }
}
if (-not $token) {
    $logs = (& docker compose @files logs --no-color snipmint 2>$null) -join "`n"
    $all = [regex]::Matches($logs, 'http://localhost:8000/\?token=([A-Za-z0-9_-]+)')
    if ($all.Count -gt 0) { $token = $all[$all.Count - 1].Groups[1].Value }
}

$url = 'http://localhost:8000/'
if ($token) { $url = "http://localhost:8000/?token=$token" }

Write-Host ''
Write-Host 'Everything is running.' -ForegroundColor Green
Write-Host '  Snipmint : http://localhost:8000'
if ($useN8n) { Write-Host '  n8n      : http://localhost:5678' }
Write-Host '  Stop it  : .\stop.ps1'
if (-not $NoBrowser) { Start-Process $url }
