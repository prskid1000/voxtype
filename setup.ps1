#Requires -Version 5.1
<#
.SYNOPSIS
    VoiceMode Windows Setup - Local voice (STT + TTS) for Claude Code
.DESCRIPTION
    Sets up VoiceMode MCP with local Whisper STT and Kokoro TTS on Windows.
    All processing is local - no cloud APIs, full privacy.
.PARAMETER InstallDir
    Installation directory (default: ~/.voicemode-windows)
.PARAMETER WhisperPort
    Port for Whisper STT server (default: 6600)
.PARAMETER KokoroPort
    Port for Kokoro TTS server (default: 6500)
.PARAMETER WhisperModel
    Whisper model to use (default: Systran/faster-whisper-small)
.PARAMETER GpuSupport
    Install GPU support for Kokoro TTS (default: true)
.PARAMETER SkipKokoro
    Skip Kokoro TTS installation (default: false)
.PARAMETER SkipWhisper
    Skip Whisper STT installation (default: false)
#>
param(
    [string]$InstallDir = "$env:USERPROFILE\.voicemode-windows",
    [int]$WhisperPort = 6600,
    [int]$KokoroPort = 6500,
    [string]$WhisperModel = "Systran/faster-whisper-small",
    [bool]$GpuSupport = $true,
    [switch]$SkipKokoro,
    [switch]$SkipWhisper
)

$ErrorActionPreference = "SilentlyContinue"

function Write-Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "    [FAIL] $msg" -ForegroundColor Red }

Write-Host @"

  VoiceMode Windows Setup
  Local STT (Whisper) + TTS (Kokoro) for Claude Code
  ====================================================

"@ -ForegroundColor Magenta

# --- Check prerequisites ---
Write-Step "Checking prerequisites"

# Python - find working python 3.10+ executable
$pythonExe = $null

# Search: PATH commands, py launcher, pyenv, common install locations
$searchPaths = @()
# Commands in PATH
foreach ($name in @("python3.exe", "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $searchPaths += $cmd.Source }
}
# py launcher
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) { $searchPaths += "py.exe" }
# pyenv-win
$pyenvRoot = "$env:USERPROFILE\.pyenv\pyenv-win\versions"
if (Test-Path $pyenvRoot) {
    Get-ChildItem $pyenvRoot -Directory | Sort-Object Name -Descending | ForEach-Object {
        $p = Join-Path $_.FullName "python.exe"
        if (Test-Path $p) { $searchPaths += $p }
    }
}
# Common install locations
foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                  "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
                  "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
                  "C:\Python312\python.exe", "C:\Python311\python.exe")) {
    if (Test-Path $p) { $searchPaths += $p }
}

foreach ($candidate in $searchPaths) {
    try {
        if ($candidate -eq "py.exe") {
            $ver = py -3 --version 2>&1 | Out-String
        } else {
            $ver = & $candidate --version 2>&1 | Out-String
        }
        if ($ver -match 'Python 3\.(1[0-9]|[2-9][0-9])') {
            $pythonExe = $candidate
            break
        }
    } catch {}
}
if (-not $pythonExe) {
    Write-Fail "Python 3.10+ not found. Install from https://python.org"
    exit 1
}
Write-Ok "Python: $(& $pythonExe --version 2>&1) ($pythonExe)"

# pip
& $pythonExe -m pip --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip not found"
    exit 1
}
Write-Ok "pip available"

# ffmpeg
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Warn "ffmpeg not found - some audio features may not work"
} else {
    Write-Ok "ffmpeg available"
}

# GPU check
if ($GpuSupport) {
    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($nvidiaSmi) {
        $gpuInfo = nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1
        Write-Ok "GPU: $gpuInfo"
    } else {
        Write-Warn "nvidia-smi not found - falling back to CPU"
        $GpuSupport = $false
    }
}

# --- Create installation directory ---
Write-Step "Creating installation directory: $InstallDir"
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Write-Ok "Directory ready"

# --- Create venv for VoiceMode MCP ---
Write-Step "Setting up VoiceMode MCP virtual environment"
$mcpVenv = Join-Path $InstallDir "mcp-venv"
if (-not (Test-Path "$mcpVenv\Scripts\python.exe")) {
    & $pythonExe -m venv $mcpVenv
    Write-Ok "Created venv: $mcpVenv"
} else {
    Write-Ok "Venv already exists: $mcpVenv"
}

& "$mcpVenv\Scripts\python.exe" -m pip install --upgrade pip --quiet 2>&1 | Out-Null
& "$mcpVenv\Scripts\pip.exe" install "setuptools<71" webrtcvad voice-mode --quiet 2>&1 | Out-Null
if (-not (Test-Path "$mcpVenv\Scripts\voice-mode.exe")) {
    Write-Fail "Failed to install voice-mode"
    exit 1
}
Write-Ok "VoiceMode MCP installed"

# --- Apply Windows patches ---
Write-Step "Applying Windows compatibility patches"
$patchScript = Join-Path $PSScriptRoot "patches\apply-patches.ps1"
& $patchScript -VenvPath $mcpVenv
Write-Ok "Patches applied"

# --- Setup Whisper STT ---
if (-not $SkipWhisper) {
    Write-Step "Setting up Whisper STT service"
    $sttVenv = Join-Path $InstallDir "stt-venv"
    if (-not (Test-Path "$sttVenv\Scripts\python.exe")) {
        & $pythonExe -m venv $sttVenv
    }
    & "$sttVenv\Scripts\python.exe" -m pip install --upgrade pip --quiet 2>&1 | Out-Null
    & "$sttVenv\Scripts\pip.exe" install faster-whisper-server --quiet 2>&1 | Out-Null

    # Patch faster-whisper-server version lookup bug
    $apiFile = Join-Path $sttVenv "Lib\site-packages\faster_whisper_server\api.py"
    if (Test-Path $apiFile) {
        $content = Get-Content $apiFile -Raw
        if ($content -notmatch 'except FileNotFoundError') {
            $content = $content -replace '(with pyproject_path\.open\("rb"\) as f:\s+data = tomllib\.load\(f\)\s+return data\["project"\]\["version"\])',
                @"
try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
        return data["project"]["version"]
    except FileNotFoundError:
        return "0.0.2"
"@
            Set-Content $apiFile -Value $content -NoNewline
            Write-Ok "Patched faster-whisper-server version lookup"
        }
    }

    # Create startup script
    $whisperBat = Join-Path $InstallDir "start-whisper-stt.bat"
    @"
@echo off
title Whisper STT Server (port $WhisperPort)
"$sttVenv\Scripts\faster-whisper-server.exe" $WhisperModel --host 127.0.0.1 --port $WhisperPort
"@ | Set-Content $whisperBat
    Write-Ok "Whisper STT ready on port $WhisperPort"
}

# --- Setup Kokoro TTS ---
if (-not $SkipKokoro) {
    Write-Step "Setting up Kokoro TTS service"

    # Clone Kokoro-FastAPI
    $kokoroDir = Join-Path $InstallDir "Kokoro-FastAPI"
    if (-not (Test-Path "$kokoroDir\pyproject.toml")) {
        # Remove partial clone if exists
        if (Test-Path $kokoroDir) {
            Remove-Item -Recurse -Force $kokoroDir -ErrorAction SilentlyContinue
        }
        $ErrorActionPreference = "Continue"
        git clone --depth 1 https://github.com/remsky/Kokoro-FastAPI.git $kokoroDir 2>&1 | Out-Null
        $ErrorActionPreference = "SilentlyContinue"
        if (-not (Test-Path "$kokoroDir\pyproject.toml")) {
            Write-Fail "Failed to clone Kokoro-FastAPI"
            exit 1
        }
    }
    Write-Ok "Kokoro-FastAPI cloned"

    # Create venv and install
    $ttsVenv = Join-Path $InstallDir "tts-venv"
    if (-not (Test-Path "$ttsVenv\Scripts\python.exe")) {
        & $pythonExe -m venv $ttsVenv
    }
    & "$ttsVenv\Scripts\python.exe" -m pip install --upgrade pip --quiet 2>&1 | Out-Null

    # Install PyTorch first (pip can't resolve uv-specific index sources)
    if ($GpuSupport) {
        Write-Step "Installing PyTorch with CUDA support (this may take a while)..."
        & "$ttsVenv\Scripts\pip.exe" install torch --index-url https://download.pytorch.org/whl/cu129 --quiet 2>&1 | Out-Null
    } else {
        & "$ttsVenv\Scripts\pip.exe" install torch --index-url https://download.pytorch.org/whl/cpu --quiet 2>&1 | Out-Null
    }

    # Install Kokoro-FastAPI (without extras since torch is already installed)
    Push-Location $kokoroDir
    & "$ttsVenv\Scripts\pip.exe" install -e . --quiet 2>&1 | Out-Null
    Pop-Location

    if (-not (Test-Path "$ttsVenv\Scripts\uvicorn.exe")) {
        Write-Fail "Failed to install Kokoro-FastAPI"
        exit 1
    }
    Write-Ok "Kokoro TTS installed ($extra mode)"

    # Download model
    $modelPath = Join-Path $kokoroDir "api\src\models\v1_0\kokoro-v1_0.pth"
    if (-not (Test-Path $modelPath)) {
        Write-Step "Downloading Kokoro model (313MB)..."
        & "$ttsVenv\Scripts\python.exe" "$kokoroDir\docker\scripts\download_model.py" --output "$kokoroDir\api\src\models\v1_0" 2>&1 | Out-Null
        if (Test-Path $modelPath) {
            Write-Ok "Model downloaded"
        } else {
            Write-Fail "Failed to download Kokoro model"
            exit 1
        }
    } else {
        Write-Ok "Model already downloaded"
    }

    # Create startup script
    $kokoroBat = Join-Path $InstallDir "start-kokoro-tts.bat"
    $gpuFlag = if ($GpuSupport) { "true" } else { "false" }
    @"
@echo off
title Kokoro TTS Server (port $KokoroPort)
set PYTHONUTF8=1
set USE_GPU=$gpuFlag
set USE_ONNX=false
set PROJECT_ROOT=$kokoroDir
set PYTHONPATH=%PROJECT_ROOT%;%PROJECT_ROOT%\api
set MODEL_DIR=src\models
set VOICES_DIR=src\voices\v1_0
set WEB_PLAYER_PATH=%PROJECT_ROOT%\web
cd /d %PROJECT_ROOT%
"$ttsVenv\Scripts\uvicorn.exe" api.src.main:app --host 127.0.0.1 --port $KokoroPort
"@ | Set-Content $kokoroBat
    Write-Ok "Kokoro TTS ready on port $KokoroPort"
}

# --- Configure Claude Code MCP ---
Write-Step "Configuring Claude Code MCP server"
$configScript = Join-Path $PSScriptRoot "configure-claude.ps1"
& $configScript -InstallDir $InstallDir -WhisperPort $WhisperPort -KokoroPort $KokoroPort
Write-Ok "Claude Code configured"

# --- Create scheduled tasks for STT + TTS ---
Write-Step "Creating scheduled tasks for voice services"
$taskScript = Join-Path $PSScriptRoot "create-scheduled-tasks.ps1"
& $taskScript -InstallDir $InstallDir
Write-Ok "Scheduled tasks created"

# --- Setup VoxType dictation app ---
Write-Step "Setting up VoxType dictation overlay"
$voxTypeSrc = Join-Path $PSScriptRoot "voxtype"
$voxTypeDest = Join-Path $InstallDir "voxtype"

if (Test-Path "$voxTypeSrc\package.json") {
    # Check for Node.js
    $nodeExe = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeExe) {
        Write-Ok "Node.js: $(node --version)"

        # Build in source directory
        Push-Location $voxTypeSrc
        Write-Host "    Installing npm dependencies..." -ForegroundColor DarkGray
        npm install --quiet 2>&1 | Out-Null
        if (Test-Path "node_modules\.bin\electron.cmd") {
            Write-Ok "Dependencies installed"

            Write-Host "    Building VoxType..." -ForegroundColor DarkGray
            npx tsc -p tsconfig.node.json 2>&1 | Out-Null
            npx vite build 2>&1 | Out-Null
            if (Test-Path "dist\main\main\index.js") {
                Write-Ok "VoxType built"
            } else {
                Write-Warn "VoxType build failed — skipping"
                Pop-Location
                break
            }
        } else {
            Write-Warn "npm install failed — skipping VoxType setup"
            Pop-Location
            break
        }
        Pop-Location

        # Copy built app to install directory
        Write-Step "Copying VoxType to $voxTypeDest"
        if (Test-Path $voxTypeDest) {
            Remove-Item -Recurse -Force $voxTypeDest -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Path $voxTypeDest -Force | Out-Null

        # Copy only what's needed to run (not source)
        $copyItems = @(
            "dist",
            "node_modules",
            "resources",
            "package.json",
            "start-voxtype.vbs",
            "start-voxtype.bat",
            "create-scheduled-task.ps1"
        )
        foreach ($item in $copyItems) {
            $src = Join-Path $voxTypeSrc $item
            $dst = Join-Path $voxTypeDest $item
            if (Test-Path $src) {
                if ((Get-Item $src).PSIsContainer) {
                    Copy-Item -Recurse -Force $src $dst
                } else {
                    Copy-Item -Force $src $dst
                }
            }
        }
        Write-Ok "VoxType copied to install directory"

        # Create scheduled task pointing to install directory
        & "$voxTypeDest\create-scheduled-task.ps1" -VoxTypePath $voxTypeDest
        Write-Ok "VoxType scheduled task created"
    } else {
        Write-Warn "Node.js not found — skipping VoxType setup. Install from https://nodejs.org"
    }
} else {
    Write-Warn "VoxType directory not found — skipping"
}

# --- Start all services now ---
Write-Step "Starting services"
$tasksToStart = @("VoiceMode-Whisper-STT", "VoiceMode-Kokoro-TTS", "VoxType-Dictation")
foreach ($task in $tasksToStart) {
    $existing = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    if ($existing) {
        Start-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
        Write-Ok "Started: $task"
    }
}

# --- Summary ---
Write-Host @"

  ====================================================
  Setup Complete!
  ====================================================

  Services (auto-start on login):
    Whisper STT  : 127.0.0.1:$WhisperPort
    Kokoro TTS   : 127.0.0.1:$KokoroPort
    VoxType      : Dictation overlay (Ctrl+Win)

  All services are running and will auto-start on login.

  VoxType: Press Ctrl+Win to dictate into any app.
  Right-click tray icon for settings.

  Then restart Claude Code and use voice!

"@ -ForegroundColor Green
