#Requires -Version 5.1
<#
.SYNOPSIS
    Uninstall VoxType — kills services, removes the scheduled task,
    optionally removes the install directory + user data.
#>
param(
    [string]$InstallDir = "$env:USERPROFILE\.voicemode-windows"
)

function Ok($msg)   { Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }

Write-Host "`n  VoxType Uninstaller`n" -ForegroundColor Cyan

# 1. Stop + remove the VoxType scheduled task (cleanly stops child services)
$tasks = @(
    'VoxType-Dictation',
    # Legacy task names from the old multi-task layout
    'VoiceMode-Whisper-STT',
    'VoiceMode-Kokoro-TTS'
)
foreach ($t in $tasks) {
    $existing = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask    -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
        Ok "Removed task: $t"
    }
}

# 2. Kill orphaned child processes (Whisper + Kokoro + VoxType's own
#    pythonw, in case the task was killed without a chance to clean up)
foreach ($p in @('faster-whisper-server', 'uvicorn', 'pythonw', 'python')) {
    $procs = Get-Process -Name $p -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)
    }
    foreach ($proc in $procs) {
        try { $proc | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
    }
}
Ok "Killed any orphaned service processes under $InstallDir"

# 3. Repo-local user data (new layout: $InstallDir\voxtype\data)
$repoData = Join-Path $InstallDir "voxtype\data"
if (Test-Path $repoData) {
    $confirm = Read-Host "  Delete VoxType user data at $repoData (settings, history, logs)? (y/N)"
    if ($confirm -eq 'y') {
        Remove-Item -Recurse -Force $repoData -ErrorAction SilentlyContinue
        Ok "Removed $repoData"
    }
}

# 4. Legacy ~/.voxtype dir from the pre-repo-local layout
$legacyVoxtype = Join-Path $env:USERPROFILE ".voxtype"
if (Test-Path $legacyVoxtype) {
    $confirm = Read-Host "  Delete legacy VoxType data at $legacyVoxtype? (y/N)"
    if ($confirm -eq 'y') {
        Remove-Item -Recurse -Force $legacyVoxtype -ErrorAction SilentlyContinue
        Ok "Removed $legacyVoxtype"
    }
}

# 5. Legacy ~/.voicemode dir (only existed for the old voice-mode MCP)
$legacyVoicemode = Join-Path $env:USERPROFILE ".voicemode"
if (Test-Path $legacyVoicemode) {
    $confirm = Read-Host "  Delete legacy voice-mode MCP data at $legacyVoicemode? (y/N)"
    if ($confirm -eq 'y') {
        Remove-Item -Recurse -Force $legacyVoicemode -ErrorAction SilentlyContinue
        Ok "Removed $legacyVoicemode"
    }
}

# 6. Install directory (venvs + Kokoro repo + model + repo checkout)
if (Test-Path $InstallDir) {
    $confirm = Read-Host "  Delete install directory $InstallDir (~3 GB)? (y/N)"
    if ($confirm -eq 'y') {
        Remove-Item -Recurse -Force $InstallDir
        Ok "Removed $InstallDir"
    } else {
        Warn "Install directory kept — re-run setup.ps1 anytime to reinstall the task."
    }
}

Write-Host "`n  Uninstall complete.`n" -ForegroundColor Green
