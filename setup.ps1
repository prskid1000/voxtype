#Requires -Version 5.1
<#
.SYNOPSIS
    VoxType Setup — local voice dictation overlay for Windows.
.DESCRIPTION
    Installs Whisper STT and Kokoro TTS as Python venvs inside the install
    directory, installs the VoxType UI Python deps (PySide6 + pynput + …),
    and registers a scheduled task `VoxType-Dictation` that auto-starts at
    logon. VoxType itself owns the Whisper and Kokoro child processes —
    no separate scheduled tasks, no wrapper scripts.

    The Electron UI has been replaced by a PySide6 UI that talks to
    telecode's dual-protocol proxy for LLM transcript cleanup. No Node.js
    is required any more.
.PARAMETER InstallDir
    Where everything lives. Defaults to ~/.voicemode-windows (the repo dir
    when this script is run from a clone).
.PARAMETER WhisperModel
    Initial Whisper model. VoxType can switch later from the tray.
.PARAMETER GpuSupport
    Install PyTorch with CUDA. Set to $false for CPU-only Kokoro.
.PARAMETER SkipKokoro
    Skip the Kokoro install (~3 GB of PyTorch + model). VoxType still works
    for dictation; Kokoro is optional.
#>
param(
    [string]$InstallDir   = "$env:USERPROFILE\.voicemode-windows",
    [string]$WhisperModel = "Systran/faster-whisper-small",
    [bool]  $GpuSupport   = $true,
    [switch]$SkipKokoro
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    [OK] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    [FAIL] $msg" -ForegroundColor Red; exit 1 }

Write-Host @"

  VoxType Setup
  Local voice dictation for Windows (Python / PySide6)
  =========================================

"@ -ForegroundColor Magenta

# ─── Prerequisites ──────────────────────────────────────────────────

Step "Checking prerequisites"

# Find a working Python 3.10+
$pythonExe = $null
$candidates = @()
foreach ($name in @("python3.exe", "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
}
if (Get-Command py.exe -ErrorAction SilentlyContinue) { $candidates += "py.exe" }
$pyenvRoot = "$env:USERPROFILE\.pyenv\pyenv-win\versions"
if (Test-Path $pyenvRoot) {
    Get-ChildItem $pyenvRoot -Directory | Sort-Object Name -Descending | ForEach-Object {
        $p = Join-Path $_.FullName "python.exe"
        if (Test-Path $p) { $candidates += $p }
    }
}
foreach ($p in @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
)) { if (Test-Path $p) { $candidates += $p } }

foreach ($c in $candidates) {
    try {
        $ver = if ($c -eq "py.exe") { py -3 --version 2>&1 } else { & $c --version 2>&1 }
        if ($ver -match 'Python 3\.(1[0-9]|[2-9][0-9])') { $pythonExe = $c; break }
    } catch {}
}
if (-not $pythonExe) { Fail "Python 3.10+ not found. Install from https://python.org" }
Ok "Python: $(& $pythonExe --version 2>&1) ($pythonExe)"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git not found. Install from https://git-scm.com"
}
Ok "git available"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Warn "ffmpeg not found — some audio features may degrade"
} else {
    Ok "ffmpeg available"
}

if ($GpuSupport) {
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $gpuInfo = nvidia-smi --query-gpu=name --format=csv,noheader 2>&1 | Select-Object -First 1
        Ok "GPU: $gpuInfo"
    } else {
        Warn "nvidia-smi not found — falling back to CPU"
        $GpuSupport = $false
    }
}

# ─── Install dir ─────────────────────────────────────────────────────

Step "Install directory: $InstallDir"
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Ok "Ready"

# ─── VoxType UI venv (PySide6 + sounddevice + pynput + …) ────────────

Step "Installing VoxType UI"
$voxVenv    = Join-Path $InstallDir "voxtype-venv"
$voxPython  = Join-Path $voxVenv "Scripts\python.exe"
$voxTypeDir = Join-Path $InstallDir "voxtype"

if (-not (Test-Path "$voxTypeDir\__main__.py")) {
    Fail "voxtype/ not found at $voxTypeDir — run setup.ps1 from the repo root"
}

if (-not (Test-Path $voxPython)) {
    & $pythonExe -m venv $voxVenv
}

Write-Host "    pip install (PySide6, pynput, sounddevice, numpy, Pillow, mss, aiohttp)..." -ForegroundColor DarkGray
& $voxPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null
& "$voxVenv\Scripts\pip.exe" install -r "$voxTypeDir\requirements.txt" --quiet 2>&1 | Out-Null

if (-not (Test-Path "$voxVenv\Lib\site-packages\PySide6")) { Fail "VoxType UI pip install failed" }
Ok "VoxType UI deps installed"

# ─── Whisper STT venv ────────────────────────────────────────────────

Step "Installing Whisper STT"
$sttVenv    = Join-Path $InstallDir "stt-venv"
$whisperExe = Join-Path $sttVenv "Scripts\faster-whisper-server.exe"
$apiFile    = Join-Path $sttVenv "Lib\site-packages\faster_whisper_server\api.py"

if (-not (Test-Path "$sttVenv\Scripts\python.exe")) {
    & $pythonExe -m venv $sttVenv
}

Write-Host "    pip install (skips download if up-to-date)..." -ForegroundColor DarkGray
& "$sttVenv\Scripts\python.exe" -m pip install --upgrade pip --quiet 2>&1 | Out-Null
& "$sttVenv\Scripts\pip.exe" install faster-whisper-server --quiet 2>&1 | Out-Null

# Patch faster-whisper-server's tomllib lookup (PyPI packaging quirk).
if (Test-Path $apiFile) {
    $content = Get-Content $apiFile -Raw
    if ($content -notmatch 'except FileNotFoundError') {
        $patched = $content -replace `
            '(with pyproject_path\.open\("rb"\) as f:\s+data = tomllib\.load\(f\)\s+return data\["project"\]\["version"\])', `
@"
try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
        return data["project"]["version"]
    except FileNotFoundError:
        return "0.0.2"
"@
        Set-Content $apiFile -Value $patched -NoNewline
        Ok "Patched faster-whisper-server version lookup"
    }
}

if (-not (Test-Path $whisperExe)) { Fail "Whisper install failed" }
Ok "Whisper ready (model downloads to ~/.cache/huggingface on first dictation)"

# ─── Kokoro TTS venv (optional) ──────────────────────────────────────

if (-not $SkipKokoro) {
    Step "Installing Kokoro TTS"
    $kokoroDir  = Join-Path $InstallDir "Kokoro-FastAPI"
    $ttsVenv    = Join-Path $InstallDir "tts-venv"
    $uvicornExe = Join-Path $ttsVenv "Scripts\uvicorn.exe"
    $modelPath  = Join-Path $kokoroDir "api\src\models\v1_0\kokoro-v1_0.pth"

    if (-not (Test-Path "$kokoroDir\pyproject.toml")) {
        if (Test-Path $kokoroDir) { Remove-Item -Recurse -Force $kokoroDir }
        git clone --depth 1 https://github.com/remsky/Kokoro-FastAPI.git $kokoroDir 2>&1 | Out-Null
        if (-not (Test-Path "$kokoroDir\pyproject.toml")) { Fail "Failed to clone Kokoro-FastAPI" }
    }

    if (-not (Test-Path "$ttsVenv\Scripts\python.exe")) {
        & $pythonExe -m venv $ttsVenv
    }

    Write-Host "    pip install (skips download if up-to-date — first run is multi-GB)..." -ForegroundColor DarkGray
    & "$ttsVenv\Scripts\python.exe" -m pip install --upgrade pip --quiet 2>&1 | Out-Null

    if ($GpuSupport) {
        & "$ttsVenv\Scripts\pip.exe" install torch --index-url https://download.pytorch.org/whl/cu129 --quiet 2>&1 | Out-Null
    } else {
        & "$ttsVenv\Scripts\pip.exe" install torch --index-url https://download.pytorch.org/whl/cpu --quiet 2>&1 | Out-Null
    }

    Push-Location $kokoroDir
    & "$ttsVenv\Scripts\pip.exe" install -e . --quiet 2>&1 | Out-Null
    Pop-Location

    if (-not (Test-Path $uvicornExe)) { Fail "Kokoro install failed" }

    if (-not (Test-Path $modelPath)) {
        Write-Host "    Downloading Kokoro model (313 MB)..." -ForegroundColor DarkGray
        & "$ttsVenv\Scripts\python.exe" "$kokoroDir\docker\scripts\download_model.py" `
            --output "$kokoroDir\api\src\models\v1_0" 2>&1 | Out-Null
        if (-not (Test-Path $modelPath)) { Fail "Failed to download Kokoro model" }
    }
    Ok "Kokoro ready (off by default — enable from VoxType tray)"
} else {
    Warn "Skipping Kokoro install (per -SkipKokoro)"
}

# ─── Scheduled task ──────────────────────────────────────────────────

Step "Registering scheduled task: VoxType-Dictation"

# pythonw.exe is the no-console GUI binary (ships with every Python install)
# so the task runs fully hidden. VoxType spawns Whisper/Kokoro as child
# processes itself.
$pythonwExe = $voxPython -replace 'python\.exe$','pythonw.exe'
if (-not (Test-Path $pythonwExe)) {
    Warn "pythonw.exe not found next to $voxPython — falling back to python.exe"
    $pythonwExe = $voxPython
}

$taskName = 'VoxType-Dictation'
$username = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Tear down any existing task (idempotent install)
Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# Tear down legacy tasks left over from the old multi-task layout
foreach ($legacy in @('VoiceMode-Whisper-STT', 'VoiceMode-Kokoro-TTS')) {
    if (Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $legacy -Confirm:$false
        Ok "Removed legacy task: $legacy"
    }
}

$action    = New-ScheduledTaskAction -Execute $pythonwExe -Argument "-m voxtype" -WorkingDirectory $InstallDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $username
$settings  = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
                -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 1) `
                -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $username -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Ok "Scheduled task registered (auto-start at logon)"

# ─── Seed settings.json with chosen Whisper model ────────────────────

$dataDir      = Join-Path $voxTypeDir "data"
$settingsFile = Join-Path $dataDir    "settings.json"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
if (-not (Test-Path $settingsFile)) {
    & $voxPython -c @"
import json, os
from pathlib import Path
import sys
sys.path.insert(0, r'$InstallDir')
from voxtype.config import load, save
s = load()
s.whisper_model = r'$WhisperModel'
save(s)
print('seeded', Path(r'$settingsFile'))
"@ 2>&1 | Out-Null
    Ok "Seeded settings.json (whisper model = $WhisperModel)"
}

# ─── Start now ───────────────────────────────────────────────────────

Step "Starting VoxType"
Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Ok "Running"

# ─── Done ────────────────────────────────────────────────────────────

Write-Host @"

  =========================================
  Setup complete!
  =========================================

  VoxType is running. Look for the tray icon (bottom-right).
  Press Ctrl+Win to dictate into any app.

  Whisper auto-starts with VoxType. Kokoro is OFF by default —
  enable it from tray > Services > Kokoro if you want TTS.

  LLM transcript cleanup is routed through telecode's proxy at
  http://127.0.0.1:1235. Make sure telecode is running for the
  enhance step to work.

  Settings, history, and logs live in:
    $voxTypeDir\data\

"@ -ForegroundColor Green
