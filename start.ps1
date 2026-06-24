#requires -Version 5.1
<#
 .SYNOPSIS
   Suno Manager - One-click launcher (Windows / PowerShell)

 .DESCRIPTION
   Windows PowerShell equivalent of start.sh. Activates the conda
   environment and starts the uvicorn server.

 .PARAMETER Port
   Override the port from config.yaml.

 .PARAMETER NoReload
   Disable hot-reload (production).

 .EXAMPLE
   .\start.ps1
 .EXAMPLE
   .\start.ps1 -Port 9090
 .EXAMPLE
   .\start.ps1 -NoReload
#>

[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$EnvName   = 'suno-manager'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Info { param([string]$Msg) Write-Host "[INFO]  " -ForegroundColor Cyan   -NoNewline; Write-Host $Msg }
function Write-Err  { param([string]$Msg) Write-Host "[ERROR] " -ForegroundColor Red    -NoNewline; Write-Host $Msg }
function Write-Warn { param([string]$Msg) Write-Host "[WARN]  " -ForegroundColor Yellow -NoNewline; Write-Host $Msg }

# ── Locate conda.exe ─────────────────────────────────────────
function Find-Conda {
    $cmd = Get-Command conda -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:USERPROFILE 'miniconda3\Scripts\conda.exe'),
        (Join-Path $env:USERPROFILE 'anaconda3\Scripts\conda.exe'),
        (Join-Path $env:USERPROFILE 'miniforge3\Scripts\conda.exe'),
        (Join-Path $env:LOCALAPPDATA 'miniconda3\Scripts\conda.exe'),
        "$env:ProgramData\miniconda3\Scripts\conda.exe",
        "$env:ProgramData\Anaconda3\Scripts\conda.exe",
        "C:\Miniconda3\Scripts\conda.exe",
        "C:\Anaconda3\Scripts\conda.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

$Conda = Find-Conda
if (-not $Conda) {
    Write-Err "Conda not found. Run .\install.ps1 first."
    exit 1
}

# ── Check environment exists ─────────────────────────────────
$envExists = $false
foreach ($line in (& $Conda env list)) {
    if ($line -match "^\s*$([regex]::Escape($EnvName))\s") { $envExists = $true; break }
}
if (-not $envExists) {
    Write-Err "Conda environment '$EnvName' not found. Run .\install.ps1 first."
    exit 1
}

# ── Read port from config.yaml if not overridden ─────────────
$configPath = Join-Path $ScriptDir 'config.yaml'
if ($Port -le 0) {
    if (Test-Path $configPath) {
        $portStr = & $Conda run -n $EnvName --no-capture-output python -c "import yaml; print(yaml.safe_load(open('$($configPath -replace '\\','/')')).get('server', {}).get('port', 8080))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $portStr -match '^\d+$') { $Port = [int]$portStr } else { $Port = 8080 }
    }
    else {
        $Port = 8080
    }
}

# ── Check if port is already in use ──────────────────────────
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $existingPid = $listener.OwningProcess
    Write-Warn "Port $Port is already in use (PID: $existingPid)"
    $answer = Read-Host "Kill existing process and restart? [y/N]"
    if ($answer -match '^[Yy]$') {
        Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        Write-Info "Old process killed"
    }
    else {
        Write-Err "Aborted. Free port $Port or use -Port to specify another."
        exit 1
    }
}

# ── Start the server ─────────────────────────────────────────
$reloadLabel = if ($NoReload) { 'disabled' } else { '--reload' }

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "         Suno Manager - Starting              " -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Environment:  " -NoNewline; Write-Host $EnvName -ForegroundColor Cyan
Write-Host "  Server:       " -NoNewline; Write-Host "http://localhost:$Port" -ForegroundColor Cyan
Write-Host "  Swagger:      " -NoNewline; Write-Host "http://localhost:$Port/docs" -ForegroundColor Cyan
Write-Host "  Reload:       " -NoNewline; Write-Host $reloadLabel -ForegroundColor Cyan
Write-Host ""

Set-Location $ScriptDir

$uvicornArgs = @('app:app', '--host', '0.0.0.0', '--port', "$Port", '--log-level', 'info')
if (-not $NoReload) { $uvicornArgs += '--reload' }

& $Conda run -n $EnvName --no-capture-output uvicorn @uvicornArgs
exit $LASTEXITCODE
