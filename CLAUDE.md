# VoiceMode Windows - Project Guide

## Overview

Windows setup automation for VoiceMode MCP (local voice for Claude Code).
Patches the Linux/macOS-only VoiceMode package to work on Windows with local Whisper STT and Kokoro TTS.

## Project Structure

```
voicemode-windows/
├── setup.ps1                     # Main installer (entry point)
├── configure-claude.ps1          # Adds MCP server to Claude Code via CLI
├── create-scheduled-tasks.ps1    # Creates Task Scheduler entries
├── uninstall.ps1                 # Clean uninstall
├── patches/
│   ├── apply-patches.ps1         # Wrapper that calls the Python patcher
│   └── apply-patches.py          # All 5 Windows patches (reliable LF matching)
├── README.md
├── CLAUDE.md                     # This file
├── LICENSE
└── .gitignore
```

## Install Directory Layout (created by setup.ps1)

```
~/.voicemode-windows/
├── mcp-venv/                     # VoiceMode MCP server (patched)
├── stt-venv/                     # faster-whisper-server
├── tts-venv/                     # Kokoro-FastAPI + PyTorch CUDA
├── Kokoro-FastAPI/               # Cloned repo + downloaded model
├── start-whisper-stt.bat         # Whisper startup (port 6600)
├── start-kokoro-tts.bat          # Kokoro startup (port 6500)
└── voicemode.env                 # Env vars reference
```

## Key Design Decisions

- **Python patcher, not PowerShell**: PowerShell here-strings use CRLF which silently fails on LF Python files. All patches are in `apply-patches.py`.
- **Separate venvs**: MCP, STT, TTS each get their own venv to avoid dependency conflicts.
- **PyTorch installed separately**: Kokoro's `pyproject.toml` uses `[tool.uv.sources]` for CUDA index which pip doesn't understand. We install torch with `--index-url` first, then Kokoro without extras.
- **`claude mcp add` via CLI**: Never parse `.claude.json` directly — it may have duplicate keys from case differences that break `ConvertFrom-Json`.
- **Two separate scheduled tasks**: A single task with two actions kills both if one crashes.
- **S4U logon type**: Runs whether user is logged on or not, no password stored.

## Windows Patches (in apply-patches.py)

1. **conch.py** — `fcntl` → `msvcrt` for file locking
2. **migration_helpers.py** — `os.uname()` → `platform.system()`
3. **model_install.py** — `os.uname()` → `platform.machine()`
4. **simple_failover.py** — `response_format: "text"` → `"json"`, remove `language="auto"`
5. **converse.py** — `scipy.signal.resample` → numpy decimation (fixes VAD freeze)

## Known Limitations

- **Push-to-talk not possible via MCP**: MCP server runs with piped stdin (JSON-RPC), so `msvcrt.kbhit()` can't detect terminal keypresses. A separate global hotkey service would be needed.
- **Conch lock**: The `~/.voicemode/conch` file can get stuck if the MCP process is killed without cleanup. Delete it manually if voice freezes.
- **faster-whisper-server**: Doesn't support `response_format=text` or `language=auto`. Patches handle this.

## Common Tasks

### Re-apply patches after voice-mode pip update
```powershell
python patches\apply-patches.py "$env:USERPROFILE\.voicemode-windows\mcp-venv"
```

### Test services
```powershell
curl http://127.0.0.1:6600/health  # Whisper
curl http://127.0.0.1:6500/health  # Kokoro
```

### Clear stuck conch lock
```bash
rm ~/.voicemode/conch
```

### Debug MCP server
```bash
PYTHONIOENCODING=utf-8 ~/.voicemode-windows/mcp-venv/Scripts/voice-mode.exe --debug
```

## Build / Test

No build step. To test setup from scratch:
```powershell
.\setup.ps1 -InstallDir "$env:USERPROFILE\.voicemode-test"
# Then clean up:
Remove-Item -Recurse "$env:USERPROFILE\.voicemode-test"
```

## Dependencies

| Component | Version | Source |
|-----------|---------|--------|
| voice-mode | 8.5.x | PyPI |
| faster-whisper-server | 0.0.2 | PyPI |
| Kokoro-FastAPI | 0.3.x | GitHub (remsky/Kokoro-FastAPI) |
| PyTorch | 2.8.x+cu129 | pytorch.org |
| webrtcvad | 2.0.10 | PyPI |
