# VoiceMode Windows - Project Guide

## Overview

Windows setup automation for VoiceMode MCP (local voice for Claude Code) + **VoxType** dictation overlay app.
Patches the Linux/macOS-only VoiceMode package to work on Windows with local Whisper STT and Kokoro TTS.
VoxType is an Electron-based Wispr Flow alternative — press a hotkey to dictate into any Windows app.

## Project Structure

```
voicemode-windows/
├── setup.ps1                     # Main installer (entry point)
├── configure-claude.ps1          # Adds MCP server to Claude Code via CLI
├── create-scheduled-tasks.ps1    # Creates Task Scheduler entries for STT+TTS
├── uninstall.ps1                 # Clean uninstall (including VoxType)
├── patches/
│   ├── apply-patches.ps1         # Wrapper that calls the Python patcher
│   └── apply-patches.py          # All 5 Windows patches (reliable LF matching)
├── voxtype/                      # VoxType dictation overlay (Electron app)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json / tsconfig.node.json
│   ├── electron-builder.json
│   ├── create-scheduled-task.ps1 # Auto-start VoxType at login
│   ├── start-voxtype.vbs         # No-console launcher (VBS → electron.exe direct)
│   ├── start-voxtype.bat         # Manual launcher (console visible)
│   ├── src/
│   │   ├── main/                 # Electron main process
│   │   │   ├── index.ts          # App entry, window, IPC, pipeline
│   │   │   ├── hotkey.ts         # uiohook-napi: custom two-key combos
│   │   │   ├── stt.ts            # POST audio to Whisper (localhost:6600)
│   │   │   ├── llm.ts            # POST transcript to LM Studio for enhancement
│   │   │   ├── typer.ts          # Clipboard + Ctrl+V via PowerShell
│   │   │   ├── tray.ts           # System tray icon + full settings menu
│   │   │   ├── preload.ts        # contextBridge for renderer IPC
│   │   │   ├── vad.ts            # Energy-based voice activity detection
│   │   │   ├── history.ts        # Transcription history (~/.voxtype/history.json)
│   │   │   ├── whisper-model.ts  # Whisper model switcher (rewrites bat, restarts task)
│   │   │   └── kokoro-voice.ts   # Kokoro voice selector (writes ~/.voicemode/voicemode.env)
│   │   ├── renderer/             # React UI
│   │   │   ├── index.html
│   │   │   ├── main.tsx
│   │   │   ├── App.tsx           # Audio capture, mic pre-warming, silence detection
│   │   │   ├── components/
│   │   │   │   ├── Pill.tsx      # Liquid-mercury orb overlay (6 animated states)
│   │   │   │   └── Settings.tsx  # Settings panel
│   │   │   └── styles/
│   │   │       └── globals.css   # Tailwind + custom animations
│   │   └── shared/
│   │       └── types.ts          # Shared types & IPC channel names
│   └── resources/
│       ├── icon.svg              # App icon (SVG source)
│       ├── icon.png              # Tray icon (64x64 PNG)
│       └── gen_icon.py           # Icon generator script
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
├── voxtype/                      # VoxType app (copied from repo at setup)
│   ├── dist/                     # Built Electron app
│   ├── node_modules/             # Runtime dependencies
│   ├── resources/                # Icons
│   ├── start-voxtype.vbs         # VBS launcher (no console)
│   ├── start-voxtype.bat         # Manual launcher
│   └── create-scheduled-task.ps1 # Task creator
├── start-whisper-stt.bat         # Whisper startup (port 6600)
├── start-kokoro-tts.bat          # Kokoro startup (port 6500)
└── voicemode.env                 # Env vars reference

~/.voxtype/
└── history.json                  # Transcription history (last 20 entries)

~/.voicemode/
└── voicemode.env                 # VoiceMode config (Kokoro voice selection)
```

## VoxType Features

### Core Pipeline
- **Hotkey** → **Record** → **Transcribe** → **Enhance** → **Type at cursor**
- Pre-warmed mic stream for instant recording (no 3s startup delay)
- Clipboard save/restore via PowerShell (works in every Windows app)

### Tray Menu Settings
| Setting | Description |
|---------|-------------|
| Hold to talk / Toggle | Recording mode |
| Hotkey | Custom two-key combo (click to capture) |
| Whisper model | Tiny/Base/Small/Medium/Large v3 (restarts service) |
| Kokoro voice | 15 featured voices (writes to voicemode.env) |
| LLM enhance | Toggle post-processing via LM Studio |
| LLM model → Auto-unload after | Unload all models (LLM + Whisper + Kokoro) after idle (Disabled/5/10/15/30/60 min) |
| LLM model → Preload on startup | Warm-up selected model at launch (sends dummy request) |
| Append mode | Append text after cursor vs replace selection |
| Auto-stop on silence | Stop recording after 2s silence |
| Skip silence (VAD) | Skip sending empty audio to Whisper |
| Save history | Store last 20 transcriptions |
| History | View/copy past transcriptions |
| Reset pill position | Snap pill back to bottom-center |

### Pill UI States
| State | Visual |
|-------|--------|
| Idle | Dark orb with aurora breathing glow |
| Recording | Expands to pill — red dot + live waveform + crimson glow |
| Processing | Orb with amber orbital dots spinner |
| Enhancing | Orb with indigo sparkle twinkle |
| Typing | Orb with green checkmark draw-in |
| Error | Orb with red lightning bolt jolt |

### LLM Enhancement
- Auto-detects loaded LM Studio model via `/v1/models`
- XML-structured system prompt with 11 rules + 10 long examples
- Handles: fillers, stutters, self-corrections, spoken punctuation, numbers, currency, lists, tech terms
- Temperature 0 for deterministic output

## Key Design Decisions

- **Python patcher, not PowerShell**: PowerShell here-strings use CRLF which silently fails on LF Python files.
- **Separate venvs**: MCP, STT, TTS each get their own venv to avoid dependency conflicts.
- **PyTorch installed separately**: Kokoro's `pyproject.toml` uses `[tool.uv.sources]` for CUDA index which pip doesn't understand.
- **`claude mcp add` via CLI**: Never parse `.claude.json` directly.
- **Three separate scheduled tasks**: VoiceMode-Whisper-STT, VoiceMode-Kokoro-TTS, VoxType-Dictation.
- **Interactive logon for all tasks**: All services use Interactive logon (runs when user is logged on). This allows VoxType to kill and restart Whisper/Kokoro processes when switching models. S4U was removed because its processes cannot be terminated by the user.
- **VBS launcher for VoxType**: `start-voxtype.vbs` launches `electron.exe` directly (GUI binary at `node_modules/electron/dist/electron.exe`), bypassing `electron.cmd` which spawns a console window. The VBS itself runs silently via `wscript.exe`.
- **Mic pre-warming**: getUserMedia() called once at app start, stream reused for instant recording (eliminates 3s mic startup delay).
- **Transparent Electron window**: `enable-transparent-visuals` + `disable-gpu-compositing` flags for Windows.
- **Preload sandboxing**: IPC constants inlined in preload.ts (can't import from shared modules in sandbox).
- **Whisper model switch**: Rewrites .bat file + kills `faster-whisper-server` process + restarts scheduled task.
- **Kokoro voice switch**: Writes to `~/.voicemode/voicemode.env` (VOICEMODE_VOICES var). No restart needed — picked up on next TTS call.

## Windows Patches (in apply-patches.py)

1. **conch.py** — `fcntl` → `msvcrt` for file locking
2. **migration_helpers.py** — `os.uname()` → `platform.system()`
3. **model_install.py** — `os.uname()` → `platform.machine()`
4. **simple_failover.py** — `response_format: "text"` → `"json"`, remove `language="auto"`
5. **converse.py** — `scipy.signal.resample` → numpy decimation (fixes VAD freeze)

## Known Limitations

- **Conch lock**: The `~/.voicemode/conch` file can get stuck if MCP process is killed. Delete manually if voice freezes.
- **faster-whisper-server**: Doesn't support `response_format=text` or `language=auto`. Patches handle this.
- **Small LLM hallucination**: 0.8B models may occasionally rewrite instead of clean up. Temperature 0 + examples mitigate this.
- **Whisper model download**: First use of a new model triggers a download (can take minutes for Large v3).

## Common Tasks

### Run VoxType in dev mode
```powershell
cd voxtype && npm run dev
```

### Build VoxType
```powershell
cd voxtype && npm run build:main && npx vite build
```

### Test services
```powershell
curl http://127.0.0.1:6600/health  # Whisper STT
curl http://127.0.0.1:6500/health  # Kokoro TTS
```

### Re-apply patches after voice-mode pip update
```powershell
python patches\apply-patches.py "$env:USERPROFILE\.voicemode-windows\mcp-venv"
```

### Clear stuck conch lock
```bash
rm ~/.voicemode/conch
```

## Dependencies

| Component | Version | Source |
|-----------|---------|--------|
| voice-mode | 8.5.x | PyPI |
| faster-whisper-server | 0.0.2 | PyPI |
| Kokoro-FastAPI | 0.3.x | GitHub (remsky/Kokoro-FastAPI) |
| PyTorch | 2.8.x+cu129 | pytorch.org |
| webrtcvad | 2.0.10 | PyPI |
| Electron | 35.x | npm |
| React | 19.x | npm |
| Tailwind CSS | 4.x | npm |
| uiohook-napi | 1.5.x | npm |
