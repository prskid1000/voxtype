# VoiceMode Windows

Local voice input/output for [Claude Code](https://claude.ai/claude-code) on Windows. Fully offline STT (Whisper) + TTS (Kokoro) with GPU acceleration.

Includes **VoxType** — a local Wispr Flow alternative that lets you dictate into any Windows app with a global hotkey.

## What it does

- **Speech-to-Text**: Local [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) with OpenAI-compatible API
- **Text-to-Speech**: Local [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) with GPU support
- **MCP Integration**: Patched [VoiceMode](https://github.com/mbailey/voicemode) MCP server for Windows
- **VoxType Dictation**: Electron overlay app — press hotkey, speak, text appears at cursor
- **No cloud APIs**: Everything runs locally, full privacy
- **Auto-start**: Task Scheduler integration for boot-time startup (hidden, no console window)

## Prerequisites

- Windows 10/11
- Python 3.10+ (3.12 recommended)
- Node.js 18+ (for VoxType)
- Git
- ffmpeg (in PATH)
- NVIDIA GPU (optional, for Kokoro TTS acceleration)
- [Claude Code](https://claude.ai/claude-code) installed
- [LM Studio](https://lmstudio.ai/) with any model loaded (for VoxType enhancement, optional)

## Quick Start

```powershell
git clone https://github.com/prskid1000/voicemode-windows.git
cd voicemode-windows
.\setup.ps1
```

Setup will:
1. Install VoiceMode MCP with Windows patches
2. Install Whisper STT + Kokoro TTS services
3. Build and install VoxType dictation app
4. Create scheduled tasks for all 3 services
5. Start everything immediately

## VoxType — Voice Dictation

Press your hotkey (default: **Ctrl+Win**), speak, release — text appears at your cursor in any app.

### Features

- **Instant recording** — mic stream is pre-warmed at app startup (`getUserMedia` called once), eliminating the 3-second delay that normally occurs on first recording
- **Hold or toggle mode** — "Hold to talk" records while you hold the hotkey; "Toggle" records on first press, stops on second press
- **Custom hotkey** — capture any two-key combo (e.g. Ctrl+Win, Alt+Space) by clicking "Hotkey" in tray and pressing your keys
- **Whisper model switching** — select Tiny/Base/Small/Medium/Large v3 from tray; VoxType rewrites the `start-whisper-stt.bat` file with the new model name, kills the running `faster-whisper-server` process, and restarts the `VoiceMode-Whisper-STT` scheduled task — both VoxType dictation and Claude Code MCP use the same Whisper server
- **Kokoro voice switching** — select from 15 curated voices; VoxType writes `VOICEMODE_VOICES=<voice_id>,alloy` to `~/.voicemode/voicemode.env` — the VoiceMode MCP server reads this file on each TTS call, so Claude Code's voice changes immediately without restart
- **LLM enhancement** — after Whisper transcribes, the raw text is sent to LM Studio (`localhost:1234`) with an 11-rule system prompt + 10 examples to clean up fillers ("um", "like"), fix punctuation, format numbers/currency, and handle self-corrections — uses temperature 0 for deterministic output; auto-detects which model is loaded via `/v1/models`
- **Model preload** — on startup, sends a dummy silent WAV to Whisper and a dummy "Hi" request to LM Studio in parallel, so both models are loaded and warm before first dictation; disable in tray to skip startup entirely for instant launch
- **Auto-unload after idle** — configurable timer (5/10/15/30/60 min) that unloads all three models after no dictation activity, freeing VRAM: unloads the LLM model (via LM Studio API), kills the Whisper process, and kills the Kokoro TTS process; both the Whisper and Kokoro scheduled tasks are restarted so servers stay ready without models loaded
- **Auto-stop on silence** — monitors RMS energy level during recording; stops after 2 seconds of continuous silence
- **VAD noise gate** — before sending audio to Whisper, checks if RMS energy exceeds threshold and duration is >0.3s; skips empty recordings to save Whisper processing time
- **Append mode** — when enabled, saves clipboard content before pasting and restores it after, effectively appending text at cursor position; when disabled, replaces current selection
- **Transcription history** — stores last 20 entries in `~/.voxtype/history.json` with timestamp, raw transcript, and enhanced version; click any entry in tray to copy to clipboard
- **Multi-monitor** — pill overlay follows your cursor to whichever display is active
- **Draggable pill** — drag to reposition anywhere on screen; position (`pillX`, `pillY`) persists in settings across restarts
- **Minimal UI** — 28px transparent orb with animated states, expands to pill shape only during recording; uses `enable-transparent-visuals` + `disable-gpu-compositing` Electron flags for Windows transparency

### Pill States

| State | Visual |
|-------|--------|
| Idle | Dark orb with breathing aurora glow |
| Recording | Red pill with pulsing dot + live waveform |
| Transcribing | Orb with amber spinner |
| Enhancing | Orb with indigo sparkle |
| Done | Orb with green checkmark |
| Error | Orb with red lightning bolt |

### Tray Menu

Right-click the VoxType tray icon for full settings:

| Setting | Type | What it does |
|---------|------|--------------|
| Hold to talk | Radio | Record audio while you hold the hotkey down. Release to stop and transcribe. |
| Toggle on/off | Radio | First press starts recording, second press stops. Good for longer dictation. |
| Hotkey | Click | Enters capture mode — press any two keys together to set as your hotkey. Shows current combo (e.g. "Ctrl + LWin"). |
| Whisper model | Submenu | Switches the STT model. Rewrites `start-whisper-stt.bat`, kills the running Whisper process, restarts the scheduled task. Affects both VoxType and Claude Code MCP (same Whisper server). |
| Kokoro voice | Submenu | Switches the TTS voice for Claude Code's VoiceMode MCP. Writes `VOICEMODE_VOICES=<id>,alloy` to `~/.voicemode/voicemode.env`. Takes effect on next MCP TTS call — no restart needed. |
| LLM enhance | Toggle | Sends raw Whisper transcript to LM Studio (localhost:1234) for cleanup. Removes filler words, fixes punctuation, formats numbers. Disable for raw Whisper output. |
| LLM model | Submenu | Select which LM Studio model to use. Shows all downloaded models with load state. Auto-selects smallest by parameter count. |
| → Auto-unload after | Submenu | Unload all models (Whisper + LLM + Kokoro) after idle time (Disabled/5/10/15/30/60 min). Frees VRAM when not dictating. Timer resets after each use. |
| → Preload on startup | Toggle | Warm up Whisper (silent WAV) + LLM (dummy request) at launch. Disable to skip LM Studio connection on startup. |
| → Refresh models | Click | Re-fetch model list from LM Studio. |
| Append mode | Toggle | ON: saves clipboard, pastes text, restores clipboard (appends at cursor). OFF: replaces current selection via Ctrl+V. |
| Auto-stop on silence | Toggle | Monitors mic RMS energy. Stops recording after 2 seconds of continuous silence. |
| Skip silence (VAD) | Toggle | Checks if recording has speech (RMS > threshold, duration > 0.3s). Skips empty recordings to avoid wasting Whisper processing time. |
| Save history | Toggle | Stores transcriptions in `~/.voxtype/history.json` (last 20 entries with timestamp, raw, and enhanced text). |
| History | Submenu | Shows last 10 entries with time. Click to copy enhanced text to clipboard. "Clear history" deletes all. |
| Show/Hide pill | Click | Toggles the floating overlay orb on/off. |
| Reset pill position | Click | Moves pill back to bottom-center of primary display. |
| Quit | Click | Exits VoxType app. |

**Whisper models** (selectable from tray):

| Model | Speed | Accuracy | VRAM |
|-------|-------|----------|------|
| Tiny | Fastest | Basic | ~1GB |
| Base | Fast | Good | ~1GB |
| Small | Balanced | Great | ~2GB |
| Medium | Slower | Better | ~5GB |
| Large v3 | Slowest | Best | ~10GB |

**Kokoro voices** (selectable from tray):

| Voice | Gender | Accent |
|-------|--------|--------|
| Sky, Heart, Bella, Nova, Sarah, Nicole, Jessica | Female | American |
| Adam, Michael, Eric, Liam | Male | American |
| Emma, Alice | Female | British |
| George, Daniel | Male | British |

## Auto-Start (Task Scheduler)

Setup creates three scheduled tasks automatically:

| Task | Service |
|------|---------|
| `VoiceMode-Whisper-STT` | Whisper STT server (port 6600) |
| `VoiceMode-Kokoro-TTS` | Kokoro TTS server (port 6500) |
| `VoxType-Dictation` | Dictation overlay app |

All tasks run hidden, auto-restart on crash, auto-restart on crash, runs hidden (Interactive logon).

```powershell
# Manual control
schtasks /run /tn VoiceMode-Whisper-STT
schtasks /run /tn VoiceMode-Kokoro-TTS
schtasks /run /tn VoxType-Dictation

# Stop
schtasks /end /tn VoiceMode-Whisper-STT
schtasks /end /tn VoiceMode-Kokoro-TTS
schtasks /end /tn VoxType-Dictation
```

## Usage in Claude Code

After setup and restarting Claude Code:

```
# Start a voice conversation (TTS + STT)
/mcp__voicemode__converse
```

### How it works

1. Claude speaks via **Kokoro TTS** (GPU-accelerated, ~1s generation)
2. Your mic records with **VAD silence detection** (auto-stops when you go quiet)
3. Audio transcribed via **local Whisper STT** (~0.5-1s)
4. Transcribed text returned to Claude as your response

### Modes

| Mode | How | Use case |
|------|-----|----------|
| Full conversation | `converse("Hello!", wait_for_response=true)` | Two-way voice chat |
| TTS only | `converse("Hello!", wait_for_response=false)` | Claude speaks, no mic |
| STT only | `converse("", skip_tts=true, wait_for_response=true)` | Mic only, no speech |

### Shared services

VoxType and Claude Code MCP share the same Whisper and Kokoro servers:
- Switching Whisper model in VoxType tray changes it for Claude Code too
- Switching Kokoro voice in VoxType tray changes Claude Code's TTS voice
- Both use `localhost:6600` (STT) and `localhost:6500` (TTS)

## Architecture

```
Claude Code                          Any Windows App
    |                                      ^
    v                                      |
VoiceMode MCP (patched)              VoxType (Electron)
    |                                 |          |
    +---> Kokoro TTS --> Speaker      |    LM Studio
    |     :6500                       |    :1234
    |                                 |
    +---> Mic --> Whisper STT <-------+
                  :6600
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `VOICEMODE_STT_BASE_URLS` | `http://127.0.0.1:6600/v1` | Whisper STT endpoint |
| `VOICEMODE_TTS_BASE_URLS` | `http://127.0.0.1:6500/v1` | Kokoro TTS endpoint |
| `VOICEMODE_VOICES` | `af_sky,alloy` | Kokoro voice (set via VoxType tray) |

## Troubleshooting

### VoxType: First words get cut off
This was fixed with mic pre-warming. If it still happens, check that the Electron app has microphone permissions in Windows Settings > Privacy > Microphone.

### VoxType: LLM rewrites my words
The enhancement prompt is designed to preserve your exact words. If it's still too aggressive, disable "LLM enhance" in the tray menu to get raw Whisper output.

### Services not starting
```powershell
netstat -ano | findstr "6500 6600"
```

### STT returns empty
Switch to a larger Whisper model via VoxType tray > Whisper model, or:
```powershell
.\setup.ps1 -WhisperModel "Systran/faster-whisper-medium"
```

## Uninstall

```powershell
.\uninstall.ps1
```

Removes all scheduled tasks, VoxType data, and optionally the install directory.

## Credits

- [VoiceMode](https://github.com/mbailey/voicemode) by Mike Bailey
- [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) by fedirz
- [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) by remsky
- [Claude Code](https://claude.ai/claude-code) by Anthropic
- Inspired by [Wispr Flow](https://wisprflow.ai/)

## License

MIT
