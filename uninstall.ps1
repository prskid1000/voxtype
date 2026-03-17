#Requires -Version 5.1
<#
.SYNOPSIS
    Uninstall VoiceMode Windows setup
#>
param(
    [string]$InstallDir = "$env:USERPROFILE\.voicemode-windows"
)

Write-Host "`n  VoiceMode Windows Uninstaller" -ForegroundColor Cyan

# Remove scheduled tasks
try {
    Unregister-ScheduledTask -TaskName "VoiceMode-Whisper-STT" -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "VoiceMode-Kokoro-TTS" -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "VoxType-Dictation" -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "  [OK] Removed scheduled tasks" -ForegroundColor Green
} catch {
    Write-Host "  [WARN] Could not remove scheduled tasks (may need admin)" -ForegroundColor Yellow
}

# Remove Claude Code MCP config via CLI (safe — doesn't parse .claude.json)
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    claude mcp remove voicemode --scope user 2>&1 | Out-Null
    Write-Host "  [OK] Removed VoiceMode from Claude Code config" -ForegroundColor Green
} else {
    Write-Host "  [WARN] claude CLI not found — remove 'voicemode' MCP server manually" -ForegroundColor Yellow
}

# Remove VoxType data
$voxTypeData = Join-Path $env:USERPROFILE ".voxtype"
if (Test-Path $voxTypeData) {
    Remove-Item -Recurse -Force $voxTypeData -ErrorAction SilentlyContinue
    Write-Host "  [OK] Removed VoxType data" -ForegroundColor Green
}

# Remove installation directory
if (Test-Path $InstallDir) {
    $confirm = Read-Host "  Delete $InstallDir ? (y/N)"
    if ($confirm -eq 'y') {
        Remove-Item -Recurse -Force $InstallDir
        Write-Host "  [OK] Removed $InstallDir" -ForegroundColor Green
    }
}

Write-Host "`n  Uninstall complete. Restart Claude Code." -ForegroundColor Green
