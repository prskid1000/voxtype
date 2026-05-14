#Requires -Version 5.1
<#
.SYNOPSIS
    VoxType Setup — local voice dictation overlay for Windows.
.DESCRIPTION
    Installs the VoxType Python deps (PySide6 + pynput + torch +
    transformers + kokoro + …) into a single venv, and registers a
    scheduled task `VoxType` that auto-starts at logon. STT and TTS
    both run in-process via PyTorch — no separate child processes,
    no extra venvs.

    External clients (telecode, MCP tools) reach VoxType through the
    embedded OpenAI-compatible HTTP server on port 6600 (configurable).
    LLM transcript cleanup is still routed through telecode's proxy.
.PARAMETER InstallDir
    Where everything lives. Defaults to ~/.voxtype.
.PARAMETER GpuSupport
    Install torch with a CUDA wheel so STT + TTS run on GPU when
    device='cuda'. Set to $false for CPU-only.
.PARAMETER CudaVersion
    Which CUDA wheel index to use when -GpuSupport is on. Accepts
    "cu130" (CUDA 13, nightly), "cu124" (CUDA 12.4 stable, recommended
    if you don't have CUDA 13 installed), or "cpu". Default cu130.
.PARAMETER FlashAttn
    Attempt to install Flash-Attention 2 for ~1.5-2x speedup on
    Whisper / Voxtral / Seamless inference. Requires fp16/bf16 +
    Ampere+ (RTX 30xx / 40xx / 50xx / A100+).

    There are NO official PyPI Windows wheels. We sniff the venv's
    torch+CUDA+python triple and search the community Windows-wheel
    repos (mjun0812 / GarfieldHuang / jono0301) via the GitHub API
    for a matching prebuilt. Coverage is narrow and lags torch's
    nightly cu130 path — most users on the default `-CudaVersion cu130`
    won't find a match and will need to either:
      a) re-run with `-CudaVersion cu124` (much broader wheel coverage)
      b) pin torch to a stable version + build from source
      c) leave Settings -> Attention on 'auto' (sdpa is still fast)
    Default `$false`. Set `-FlashAttn $true` to opt in.
#>
param(
    [string]$InstallDir   = "$env:USERPROFILE\.voxtype",
    [bool]  $GpuSupport   = $true,
    [ValidateSet("cu130", "cu124", "cpu")]
    [string]$CudaVersion  = "cu130",
    [bool]  $FlashAttn    = $false
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

# ─── VoxType venv (single venv, both engines run in-process via torch)

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

& $voxPython -m pip install --upgrade pip --no-cache-dir --quiet 2>&1 | Out-Null

# ── torch first, with the right CUDA index ──────────────────────────
# torch ships its own bundled CUDA runtime, so installing the cu130
# wheel works on machines with CUDA 13 drivers without a separate
# toolkit install. CPU is the safe fallback when -GpuSupport is off.
$torchIndex = $null
if (-not $GpuSupport) {
    $torchIndex = "https://download.pytorch.org/whl/cpu"
    Write-Host "    pip install torch (CPU build)..." -ForegroundColor DarkGray
} elseif ($CudaVersion -eq "cu130") {
    # PyTorch nightly is the only channel currently shipping CUDA 13
    # wheels (as of mid-2026). Once stable wheels land, switch to the
    # /whl/cu130 URL.
    $torchIndex = "https://download.pytorch.org/whl/nightly/cu130"
    Write-Host "    pip install torch (CUDA 13 nightly)..." -ForegroundColor DarkGray
} elseif ($CudaVersion -eq "cu124") {
    $torchIndex = "https://download.pytorch.org/whl/cu124"
    Write-Host "    pip install torch (CUDA 12.4 stable)..." -ForegroundColor DarkGray
} else {
    $torchIndex = "https://download.pytorch.org/whl/cpu"
    Write-Host "    pip install torch (CPU)..." -ForegroundColor DarkGray
}

# Install torch + numpy together so torch's first import doesn't print
# the noisy "Failed to initialize NumPy" warning on the sanity check.
# numpy is in requirements.txt too — pip dedupes the second install.
$pipExtra = @()
if ($CudaVersion -eq "cu130" -and $GpuSupport) { $pipExtra += "--pre" }
& "$voxVenv\Scripts\pip.exe" install @pipExtra torch "numpy>=1.26" --index-url $torchIndex --extra-index-url https://pypi.org/simple --no-cache-dir --quiet 2>&1 | Out-Null

if (-not (Test-Path "$voxVenv\Lib\site-packages\torch")) {
    Fail "torch install failed (tried index $torchIndex)"
}
# stderr captured separately so any residual warnings don't pollute the cuda string.
$torchCudaCheck = & $voxPython -c "import torch; print(torch.version.cuda or 'cpu')" 2>$null
Ok "torch installed (cuda=$torchCudaCheck)"

# ── Remaining deps (PySide6, STT/TTS backends) ──────────────────────
# Currently shipped: whisper (transformers) + kokoro. Additional
# backends slot into voxtype/backends/ and are picked up by the
# registry — no setup.ps1 changes needed when you add one.
Write-Host "    pip install remaining deps (PySide6, transformers, kokoro, …)..." -ForegroundColor DarkGray
& "$voxVenv\Scripts\pip.exe" install -r "$voxTypeDir\requirements.txt" --no-cache-dir --quiet 2>&1 | Out-Null

if (-not (Test-Path "$voxVenv\Lib\site-packages\PySide6")) { Fail "VoxType UI pip install failed" }
if (-not (Test-Path "$voxVenv\Lib\site-packages\transformers")) { Fail "transformers install failed (whisper backend)" }
if (-not (Test-Path "$voxVenv\Lib\site-packages\kokoro")) { Fail "kokoro install failed (kokoro TTS backend)" }
Ok "Core deps installed (UI + STT via whisper + TTS via kokoro, both on torch)"

# ─── Flash-Attention 2 (Windows-aware installer, on by default) ──────
if ($FlashAttn -and -not $GpuSupport) {
    Warn "Flash-Attention 2 needs CUDA — skipping (you passed -GpuSupport `$false)."
}
elseif ($FlashAttn) {
    Step "Installing Flash-Attention 2 (pass -FlashAttn `$false to skip)"

    # Sniff the running venv's torch + CUDA + python triple so we can
    # pick a prebuilt wheel that actually links. PyPI's flash-attn has
    # no Windows wheels — we use lldacing's release repo instead.
    $probe = @"
import json, sys, torch
out = {
    "py":    f"cp{sys.version_info.major}{sys.version_info.minor}",
    "torch": torch.__version__.split("+")[0],
    "cuda":  (torch.version.cuda or "").replace(".", "") if torch.cuda.is_available() else "",
}
print(json.dumps(out))
"@
    $probeJson = & $voxPython -c $probe 2>$null
    $env_ok = $false
    try {
        $env = $probeJson | ConvertFrom-Json
        $env_ok = ($env.py -and $env.torch -and $env.cuda)
    } catch { $env_ok = $false }

    if (-not $env_ok) {
        Warn "Couldn't probe torch/CUDA versions — skipping flash-attn install. Make sure torch is installed first."
    } else {
        $py = $env.py; $tv = $env.torch; $cu = "cu" + $env.cuda
        Write-Host "    Detected: python=$py, torch=$tv, cuda=$cu" -ForegroundColor DarkGray
        Write-Host "    Searching community Windows-wheel repos via GitHub API..." -ForegroundColor DarkGray

        # Candidate repos that publish flash-attn Windows wheels.
        # mjun0812 has the broadest matrix; the cu130/sm_120 (Blackwell)
        # repos cover RTX 50-series + CUDA 13 specifically.
        $resolve = @"
import json, sys, urllib.request
PY, TORCH, CUDA = "$py", "$tv", "$cu"

REPOS = [
    "mjun0812/flash-attention-prebuild-wheels",
    "GarfieldHuang/flash-attention-windows-wheel",
    "jono0301/flash-attention-windows-wheels",
]

def fetch(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "voxtype-setup/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

needle_torch = TORCH.rsplit(".dev", 1)[0].rsplit("+", 1)[0]
needle_minor = needle_torch.rsplit(".", 1)[0]

def search(releases, strict):
    for rel in releases or []:
        for a in rel.get("assets", []):
            n = a.get("name", "")
            if not n.endswith("-win_amd64.whl"): continue
            if CUDA not in n: continue
            if f"-{PY}-" not in n: continue
            if strict and f"torch{needle_torch}" not in n: continue
            if (not strict) and f"torch{needle_minor}" not in n: continue
            return (n, a.get("browser_download_url"))
    return None

hit = None
for repo in REPOS:
    releases = fetch(f"https://api.github.com/repos/{repo}/releases?per_page=30")
    hit = search(releases, strict=True) or search(releases, strict=False)
    if hit:
        print(f"REPO {repo}", file=sys.stderr)
        break

if not hit:
    print("ERROR no matching wheel")
    sys.exit(2)
print(hit[1])
"@
        $wheelUrl = & $voxPython -c $resolve 2>&1
        if ($LASTEXITCODE -ne 0 -or $wheelUrl -match "ERROR") {
            Warn "No prebuilt Flash-Attn wheel matched python=$py torch=$tv $cu."
            Warn "Likely cause: torch nightly + cu130 has narrow wheel coverage on Windows."
            Warn "Options:"
            Warn "  1) Re-run setup with -CudaVersion cu124 (much broader Flash-Attn coverage)"
            Warn "  2) Pin torch to a stable version (2.5.1 / 2.6 / 2.7) and re-run -FlashAttn"
            Warn "  3) Build from source: requires CUDA toolkit + MSVC, ~30 min"
            Warn "     `& '$voxVenv\Scripts\pip.exe' install flash-attn --no-build-isolation`"
            Warn "  4) Leave Settings -> Attention on 'auto' (sdpa is already fast on Ampere+)"
            Warn "Browse the wheel repos manually if you want to download one yourself:"
            Warn "  https://github.com/mjun0812/flash-attention-prebuild-wheels/releases"
            Warn "  https://github.com/GarfieldHuang/flash-attention-windows-wheel/releases"
            Warn "  https://github.com/jono0301/flash-attention-windows-wheels/releases"
        } else {
            $wheelUrl = ($wheelUrl | Select-Object -Last 1).ToString().Trim()
            $wheelFile = Join-Path $env:TEMP (Split-Path -Leaf $wheelUrl)
            Write-Host "    Downloading $($wheelFile | Split-Path -Leaf)..." -ForegroundColor DarkGray
            try {
                Invoke-WebRequest -Uri $wheelUrl -OutFile $wheelFile -UseBasicParsing
                Write-Host "    pip install $($wheelFile | Split-Path -Leaf)..." -ForegroundColor DarkGray
                & "$voxVenv\Scripts\pip.exe" install $wheelFile --no-cache-dir --quiet 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0 -and (Test-Path "$voxVenv\Lib\site-packages\flash_attn")) {
                    Ok "flash-attn installed — Settings -> Attention -> 'flash_attention_2' is now functional"
                } else {
                    Warn "Wheel downloaded but pip install failed. Check the install log."
                }
            } catch {
                Warn "Download / install failed: $($_.Exception.Message)"
                Warn "Manual fallback: download from"
                Warn "  $wheelUrl"
                Warn "then run: & '$voxVenv\Scripts\pip.exe' install <wheel.whl>"
            } finally {
                if (Test-Path $wheelFile) { Remove-Item $wheelFile -Force -ErrorAction SilentlyContinue }
            }
        }
    }
}

# ─── Pre-download default models (idempotent) ────────────────────────
#
# STT: openai/whisper-base (~145 MB).
# TTS: hexgrad/Kokoro-82M  (~327 MB).
#
# snapshot_download skips files already in the HF cache, so re-runs
# are cheap. Errors are non-fatal: engines download lazily on first
# use if this step fails.

Step "Pre-downloading default models"

Write-Host "    Fetching STT default (openai/whisper-base, ~145 MB)..." -ForegroundColor DarkGray
$rc_stt = & $voxPython -c @"
import sys
try:
    from huggingface_hub import snapshot_download
    p = snapshot_download(repo_id='openai/whisper-base')
    print('STT cached at', p)
except Exception as e:
    print('STT download skipped:', e, file=sys.stderr)
    sys.exit(1)
"@ 2>&1
if ($LASTEXITCODE -eq 0) {
    Ok "STT default cached (whisper-base, ~145 MB)"
} else {
    Warn "STT model pre-download failed (will download lazily on first use): $rc_stt"
}

Write-Host "    Fetching TTS default (hexgrad/Kokoro-82M, ~327 MB)..." -ForegroundColor DarkGray
$rc_tts = & $voxPython -c @"
import sys
try:
    from huggingface_hub import snapshot_download
    p = snapshot_download(repo_id='hexgrad/Kokoro-82M')
    print('TTS cached at', p)
except Exception as e:
    print('TTS download skipped:', e, file=sys.stderr)
    sys.exit(1)
"@ 2>&1
if ($LASTEXITCODE -eq 0) {
    Ok "TTS default cached (Kokoro-82M, ~327 MB)"
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

  STT and TTS run in-process via PyTorch.
    - STT: openai/whisper-base by default (any HF Whisper repo works).
    - TTS: hexgrad/Kokoro-82M by default — 54 voices across 9 language
      families. Voice names are strings like af_heart, jm_kumo, etc.

  External clients reach VoxType via the embedded HTTP server on
  http://127.0.0.1:6600 (OpenAI-compatible: /v1/audio/transcriptions
  and /v1/audio/speech). The `model` / `voice` request fields are
  accepted but ignored — VoxType decides the model + voice via its
  own settings.

  LLM transcript cleanup is routed through telecode's proxy at
  http://127.0.0.1:1235. Make sure telecode is running for the
  enhance step to work.

  Settings, history, and logs live in:
    $voxTypeDir\data\

"@ -ForegroundColor Green
