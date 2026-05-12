#Requires -Version 5.1
<#
.SYNOPSIS
    VoxType Setup — local voice dictation overlay for Windows.
.DESCRIPTION
    Installs the VoxType UI Python deps (PySide6 + pynput + sherpa-onnx +
    huggingface_hub + …) into a single venv, and registers a scheduled
    task `VoxType` that auto-starts at logon. STT and TTS both run
    in-process via sherpa-onnx (ONNX Runtime) — no separate child
    processes, no extra venvs.

    External clients (telecode, MCP tools) reach VoxType through the
    embedded OpenAI-compatible HTTP server on port 6600 (configurable).
    LLM transcript cleanup is still routed through telecode's proxy.
.PARAMETER InstallDir
    Where everything lives. Defaults to ~/.voxtype.
.PARAMETER GpuSupport
    Swap CPU `onnxruntime` for `onnxruntime-gpu` so device='cuda' works
    for both STT and TTS. Set to $false for CPU-only.
#>
param(
    [string]$InstallDir   = "$env:USERPROFILE\.voxtype",
    [bool]  $GpuSupport   = $true
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

# ─── VoxType venv (single venv, all in-process via ONNX Runtime) ─────

Step "Installing VoxType (single venv — UI + STT + TTS all in-process)"
$voxVenv    = Join-Path $InstallDir "voxtype-venv"
$voxPython  = Join-Path $voxVenv "Scripts\python.exe"
$voxTypeDir = Join-Path $InstallDir "voxtype"

if (-not (Test-Path "$voxTypeDir\__main__.py")) {
    Fail "voxtype/ not found at $voxTypeDir — run setup.ps1 from the repo root"
}

if (-not (Test-Path $voxPython)) {
    & $pythonExe -m venv $voxVenv
}

Write-Host "    pip install core deps (PySide6, pynput, sounddevice, aiohttp, sherpa-onnx, huggingface_hub)..." -ForegroundColor DarkGray
& $voxPython -m pip install --upgrade pip --no-cache-dir --quiet 2>&1 | Out-Null
& "$voxVenv\Scripts\pip.exe" install -r "$voxTypeDir\requirements.txt" --no-cache-dir --quiet 2>&1 | Out-Null

if (-not (Test-Path "$voxVenv\Lib\site-packages\PySide6")) { Fail "VoxType UI pip install failed" }
if (-not (Test-Path "$voxVenv\Lib\site-packages\sherpa_onnx")) { Fail "sherpa-onnx install failed" }
Ok "Core deps installed (UI + STT + TTS via sherpa-onnx)"

# GPU: sherpa-onnx uses ONNX Runtime under the hood for both engines.
# Swap the CPU `onnxruntime` wheel (pulled in transitively) for
# `onnxruntime-gpu` so device='cuda' actually lands on the GPU. Falls
# back to CPU automatically at runtime if CUDA isn't usable.
if ($GpuSupport) {
    Write-Host "    pip install onnxruntime-gpu (replaces CPU onnxruntime for GPU inference)..." -ForegroundColor DarkGray
    & "$voxVenv\Scripts\pip.exe" uninstall -y onnxruntime --quiet 2>&1 | Out-Null
    & "$voxVenv\Scripts\pip.exe" install onnxruntime-gpu --no-cache-dir --quiet 2>&1 | Out-Null
    if (Test-Path "$voxVenv\Lib\site-packages\onnxruntime") {
        Ok "onnxruntime-gpu installed (STT + TTS will use CUDA when device='cuda')"
    } else {
        Warn "onnxruntime-gpu install failed — falling back to CPU inference"
    }
}

# ─── Pre-download default models (idempotent) ────────────────────────
#
# huggingface_hub.snapshot_download() uses the HF cache (default
# ~/.cache/huggingface/hub) and skips files that already exist — so
# this step is safe to re-run, only the missing pieces are fetched.
#
# Pulled now so the first dictation isn't blocked on a multi-GB
# download. Errors are non-fatal: if the user has no network at
# install time, the engines just download lazily on first use.

Step "Pre-downloading default models"

$stt_default = "csukuangfj/sherpa-onnx-whisper-turbo"
$tts_default = "csukuangfj/kokoro-multi-lang-v1_1"

Write-Host "    Fetching $stt_default (~1.6 GB) — skipped if already cached..." -ForegroundColor DarkGray
$rc_stt = & $voxPython -c @"
import sys
try:
    from huggingface_hub import snapshot_download
    p = snapshot_download(repo_id='$stt_default')
    print('STT cached at', p)
except Exception as e:
    print('STT download skipped:', e, file=sys.stderr)
    sys.exit(1)
"@ 2>&1
if ($LASTEXITCODE -eq 0) {
    Ok "STT default cached ($stt_default)"
} else {
    Warn "STT model pre-download failed (will download lazily on first use): $rc_stt"
}

Write-Host "    Fetching $tts_default (~395 MB) — skipped if already cached..." -ForegroundColor DarkGray
$rc_tts = & $voxPython -c @"
import sys
try:
    from huggingface_hub import snapshot_download
    p = snapshot_download(repo_id='$tts_default')
    print('TTS cached at', p)
except Exception as e:
    print('TTS download skipped:', e, file=sys.stderr)
    sys.exit(1)
"@ 2>&1
if ($LASTEXITCODE -eq 0) {
    Ok "TTS default cached ($tts_default)"
} else {
    Warn "TTS model pre-download failed (will download lazily on first use): $rc_tts"
}

# ─── Scheduled task ──────────────────────────────────────────────────

Step "Registering scheduled task: VoxType"

# pythonw.exe is the no-console GUI binary (ships with every Python install)
# so the task runs fully hidden. STT + TTS now run in-process — no child
# processes to launch.
$pythonwExe = $voxPython -replace 'python\.exe$','pythonw.exe'
if (-not (Test-Path $pythonwExe)) {
    Warn "pythonw.exe not found next to $voxPython — falling back to python.exe"
    $pythonwExe = $voxPython
}

$taskName = 'VoxType'
$username = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Tear down any existing task (idempotent install)
Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# Tear down legacy tasks left over from previous install layouts
foreach ($legacy in @('VoxType-Dictation', 'VoiceMode-Whisper-STT', 'VoiceMode-Kokoro-TTS')) {
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

# ─── Seed settings.json with defaults ────────────────────────────────

$dataDir      = Join-Path $voxTypeDir "data"
$settingsFile = Join-Path $dataDir    "settings.json"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
if (-not (Test-Path $settingsFile)) {
    & $voxPython -c @"
from voxtype.config import load, save
save(load())
"@ 2>&1 | Out-Null
    Ok "Seeded settings.json (set STT/TTS model paths from the settings window)"
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

  STT and TTS run in-process via ONNX Runtime. Both are OFF until
  you point them at a model in Settings > Services:
    - STT model: HuggingFace repo ID (auto-downloads) or local
      sherpa-onnx model directory.
    - TTS model: HuggingFace repo ID or local .onnx file
      (e.g. a Piper voice from rhasspy/piper-voices).

  External clients reach VoxType via the embedded HTTP server on
  http://127.0.0.1:6600 (OpenAI-compatible: /v1/audio/transcriptions
  and /v1/audio/speech).

  LLM transcript cleanup is routed through telecode's proxy at
  http://127.0.0.1:1235. Make sure telecode is running for the
  enhance step to work.

  Settings, history, and logs live in:
    $voxTypeDir\data\

"@ -ForegroundColor Green
