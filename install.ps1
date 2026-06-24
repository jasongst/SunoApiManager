#requires -Version 5.1
<#
 .SYNOPSIS
   Suno Manager - One-click installer (Windows / PowerShell)

 .DESCRIPTION
   Windows PowerShell equivalent of install.sh. It will:
     1. Install Miniconda if conda is not found
     2. Create a conda environment "suno-manager" with Python 3.12
     3. Install ffmpeg (via conda-forge)
     4. Install Python dependencies from requirements.txt
     5. Install Patchright + Chromium (for CAPTCHA solving)
     6. Create a default config.yaml if missing
     7. Create required directories (downloads, uploads, logs)

 .EXAMPLE
   powershell -ExecutionPolicy Bypass -File .\install.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$EnvName       = 'suno-manager'
$PythonVersion = '3.12'
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path

# ─── Pretty output helpers ────────────────────────────────────
function Write-Info { param([string]$Msg) Write-Host "[INFO]  " -ForegroundColor Cyan   -NoNewline; Write-Host $Msg }
function Write-Ok   { param([string]$Msg) Write-Host "[OK]    " -ForegroundColor Green  -NoNewline; Write-Host $Msg }
function Write-Warn { param([string]$Msg) Write-Host "[WARN]  " -ForegroundColor Yellow -NoNewline; Write-Host $Msg }
function Write-Err  { param([string]$Msg) Write-Host "[ERROR] " -ForegroundColor Red    -NoNewline; Write-Host $Msg }

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "        Suno Manager - Installer (Windows)    " -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# ─── Locate conda.exe, searching PATH + common install dirs ───
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
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# ── Step 1: Check / Install Conda ─────────────────────────────
$Conda = Find-Conda

if ($Conda) {
    Write-Ok ("Conda found: " + (& $Conda --version))
}
else {
    Write-Info "Conda not found. Installing Miniconda..."

    # Miniconda only ships an x86_64 Windows installer; it runs under
    # emulation on ARM64 Windows just fine.
    $minicondaUrl = 'https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe'
    $installer    = Join-Path $env:TEMP 'miniconda_installer.exe'
    $installRoot  = Join-Path $env:USERPROFILE 'miniconda3'

    Write-Info "Downloading Miniconda from $minicondaUrl ..."
    # Use TLS 1.2 (required by Anaconda CDN on older Windows)
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $minicondaUrl -OutFile $installer -UseBasicParsing

    Write-Info "Installing Miniconda to $installRoot (silent) ..."
    # /S silent, /InstallationType=JustMe (no admin), /RegisterPython=0 (don't touch system Python)
    $args = "/InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S /D=$installRoot"
    $proc = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru
    Remove-Item $installer -Force -ErrorAction SilentlyContinue

    if ($proc.ExitCode -ne 0) {
        Write-Err "Miniconda installer exited with code $($proc.ExitCode)."
        exit 1
    }

    $Conda = Find-Conda
    if (-not $Conda) {
        Write-Err "Conda still not available after install. Restart your terminal and re-run this script."
        exit 1
    }
    Write-Ok "Miniconda installed successfully"
}

# ── Step 1b: Accept Anaconda channel Terms of Service ─────────
# Recent conda versions refuse to create environments from the default
# Anaconda channels until their ToS are accepted (CondaToSNonInteractiveError).
# Accept them up front; ignore failures on older conda builds that lack the
# `tos` subcommand.
Write-Info "Accepting Anaconda channel Terms of Service ..."
foreach ($channel in @(
    'https://repo.anaconda.com/pkgs/main',
    'https://repo.anaconda.com/pkgs/r',
    'https://repo.anaconda.com/pkgs/msys2'
)) {
    try { & $Conda tos accept --override-channels --channel $channel *> $null } catch { }
}

# ── Step 2: Create Conda Environment ──────────────────────────
$envList = & $Conda env list
$envExists = $false
foreach ($line in $envList) {
    # Match the env name as a whole word at the start of a line
    if ($line -match "^\s*$([regex]::Escape($EnvName))\s") { $envExists = $true; break }
}

if ($envExists) {
    Write-Ok "Conda environment '$EnvName' already exists"
}
else {
    Write-Info "Creating conda environment '$EnvName' (Python $PythonVersion) ..."
    & $Conda create -n $EnvName "python=$PythonVersion" -y -q
    if ($LASTEXITCODE -ne 0) { Write-Err "Failed to create conda environment."; exit 1 }
    Write-Ok "Environment '$EnvName' created"
}

# Helper: run a command inside the env without persistent activation
function Invoke-InEnv {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CmdArgs)
    & $Conda run -n $EnvName --no-capture-output @CmdArgs
}

Write-Ok ("Environment ready: " + (Invoke-InEnv python --version))

# ── Step 3: Install ffmpeg via conda-forge ────────────────────
$ffmpegOk = $false
try {
    Invoke-InEnv ffmpeg -version *> $null
    if ($LASTEXITCODE -eq 0) { $ffmpegOk = $true }
} catch { $ffmpegOk = $false }

if ($ffmpegOk) {
    Write-Ok "ffmpeg already present in environment"
}
else {
    Write-Info "Installing ffmpeg via conda-forge ..."
    & $Conda install -n $EnvName -c conda-forge ffmpeg -y -q
    if ($LASTEXITCODE -ne 0) { Write-Err "Failed to install ffmpeg."; exit 1 }
    Write-Ok "ffmpeg installed"
}

# ── Step 4: Install Python Dependencies ───────────────────────
Write-Info "Installing Python dependencies ..."
$reqPath = Join-Path $ScriptDir 'requirements.txt'
if (Test-Path $reqPath) {
    Invoke-InEnv pip install -r $reqPath -q
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install failed."; exit 1 }
    Write-Ok "Python dependencies installed"
}
else {
    Write-Err "requirements.txt not found in $ScriptDir"
    exit 1
}

# ── Step 5: Install Patchright (optional, for CAPTCHA) ────────
Write-Info "Installing Patchright + Chromium (for CAPTCHA solving) ..."
Invoke-InEnv pip install patchright -q
Invoke-InEnv python -m patchright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Patchright Chromium install failed (non-critical - only needed for CAPTCHA)"
}
else {
    Write-Ok "Patchright installed"
}

# ── Step 6: Create Default Config ─────────────────────────────
$configPath = Join-Path $ScriptDir 'config.yaml'
if (-not (Test-Path $configPath)) {
    Write-Info "Creating default config.yaml ..."
    $config = @'
# Suno Manager Configuration

# Suno API connection
suno_api:
  # Suno session cookie — __client cookie value from browser
  cookie: ""

# Song generation settings
generation:
  default_model: "chirp-crow"
  min_duration_filter: 180
  polling_interval: 10
  auto_download: true
  auto_analyze_silence: true
  batch_size: 5                       # Songs per batch (to avoid rate limits)
  batch_delay: 30                     # Seconds to wait between batches

# Download settings
download:
  directory: "./downloads"
  format: "wav"

# Silence analysis settings
silence_analysis:
  threshold: -40
  min_length: 1000

# Server settings
server:
  host: "0.0.0.0"
  port: 8080
'@
    # Write UTF-8 without BOM (PyYAML-friendly)
    [System.IO.File]::WriteAllText($configPath, $config, (New-Object System.Text.UTF8Encoding($false)))
    Write-Warn "config.yaml created with empty cookie - you must set your Suno cookie before use!"
}
else {
    Write-Ok "config.yaml already exists"
}

# ── Step 7: Create Required Directories ───────────────────────
foreach ($d in @('downloads', 'uploads', 'logs')) {
    $dir = Join-Path $ScriptDir $d
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}
Write-Ok "Directories ready (downloads, uploads, logs)"

# ── Done ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "        Installation Complete!                " -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Environment:  " -NoNewline; Write-Host $EnvName -ForegroundColor Cyan
Write-Host "  Python:       " -NoNewline; Write-Host (Invoke-InEnv python --version) -ForegroundColor Cyan
Write-Host "  Project:      " -NoNewline; Write-Host $ScriptDir -ForegroundColor Cyan
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Yellow
Write-Host "    1. Edit " -NoNewline; Write-Host "config.yaml" -ForegroundColor Cyan -NoNewline; Write-Host " and set your Suno cookie"
Write-Host "    2. Run  " -NoNewline; Write-Host ".\start.ps1" -ForegroundColor Cyan -NoNewline; Write-Host " to start the server"
Write-Host ""
