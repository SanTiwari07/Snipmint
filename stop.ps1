<#
.SYNOPSIS
  Stops Snipmint and n8n. Nothing is deleted: clips, workflows and credentials stay.

.PARAMETER QuitDocker
  Also close Docker Desktop to free memory.
#>
[CmdletBinding()]
param([switch]$QuitDocker)

Set-Location -Path $PSScriptRoot

$files = @('-f', 'docker-compose.yml')
if (Test-Path 'docker-compose.n8n.yml') { $files += @('-f', 'docker-compose.n8n.yml') }

docker info *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host '==> Stopping Snipmint and n8n' -ForegroundColor Cyan
    & docker compose @files stop
} else {
    Write-Host 'Docker is not running, so nothing is running.'
}

if ($QuitDocker) {
    Write-Host '==> Closing Docker Desktop' -ForegroundColor Cyan
    Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue | Stop-Process
}
