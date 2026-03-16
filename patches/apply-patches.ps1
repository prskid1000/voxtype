#Requires -Version 5.1
<#
.SYNOPSIS
    Apply Windows compatibility patches to VoiceMode
.DESCRIPTION
    Calls the Python patch script for reliable string replacement.
    PowerShell here-strings use CRLF which breaks Python file patching.
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$VenvPath
)

$patchScript = Join-Path $PSScriptRoot "apply-patches.py"
$pythonExe = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    Write-Host "    [FAIL] Python not found in venv: $VenvPath" -ForegroundColor Red
    exit 1
}

& $pythonExe $patchScript $VenvPath
