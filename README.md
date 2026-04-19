# VoxType

Local voice dictation overlay for Windows, written in **pure Python +
PySide6**. Press a hotkey, speak, release — cleaned text appears at
your cursor in any app. No cloud, no telemetry, no account.

Sibling project of [telecode](https://github.com/prskid1000/telecode).
LLM transcript cleanup is routed through telecode's dual-protocol proxy
at `http://127.0.0.1:1235`, so any model telecode serves (llama.cpp,
Qwen-VL, etc.) becomes a dictation backend automatically. There is no
direct LM Studio dependency any more.

---

## Quick start

```powershell
git clone https://github.com/prskid1000/voicemode-windows.git "$env:USERPROFILE\.voicemode-windows"
cd "$env:USERPROFILE\.voicemode-windows"
.\setup.ps1
```

`setup.ps1` will:

1. Verify **Python 3.10+**, **git**, **ffmpeg** (optional), GPU support
2. Create `voxtype-venv/` and `pip install -r voxtype/requirements.txt`
   (PySide6, pynput, sounddevice, numpy, Pillow, mss, aiohttp, …)
3. Create `stt-venv/` and `pip install faster-whisper-server` (~390 MB)
4. Unless `-SkipKokoro`, clone Kokoro-FastAPI, create `tts-venv/`,
   install PyTorch + CUDA wheels (~5 GB), download the Kokoro model
5. Register a single scheduled task `VoxType-Dictation` that launches
   `pythonw.exe -m voxtype` at logon (no console window)
6. Seed `voxtype/data/settings.json` with your chosen Whisper model
7. Start VoxType immediately

Look for the tray icon (bottom-right). Press **Ctrl+Win**, speak,
release.

### Setup options

```powershell
.\setup.ps1                                           # full install
.\setup.ps1 -SkipKokoro                               # no TTS — saves ~5 GB
.\setup.ps1 -GpuSupport $false                        # CPU-only PyTorch
.\setup.ps1 -WhisperModel "Systran/faster-whisper-medium"
.\setup.ps1 -InstallDir "D:\voxtype"                  # custom location
```

Re-running `setup.ps1` is idempotent: venvs, clones, and model
downloads are skipped when the artifacts already exist. A re-run on a
fully-installed machine takes ~10 s.

---

## Prerequisites

| Dependency | Required for | Where to get it |
|---|---|---|
| **Windows 10/11** | Target OS | — |
| **Python 3.10+** | Everything (VoxType UI, Whisper, Kokoro) | https://python.org |
| **git** | Clone Kokoro-FastAPI | https://git-scm.com |
| **ffmpeg** (optional) | Some audio codecs Whisper might receive | `winget install ffmpeg` |
| **NVIDIA GPU + CUDA driver** | Strongly recommended for Kokoro; Whisper works on CPU | https://nvidia.com/drivers |
| **telecode** (optional) | LLM transcript cleanup | https://github.com/prskid1000/telecode |

Without telecode running, dictation still works — you just get raw
Whisper transcripts (no filler-word cleanup, no punctuation fixes).
Set `enhance_enabled = false` in settings to silence the "proxy
unreachable" warnings.

---

## How it works

```
Hotkey down (pynput)
    → recorder.start() — sounddevice opens a 16 kHz mono int16 PCM stream
    → pill = recording

Hotkey up
    → recorder.stop() → raw PCM buffer
    → VAD gate (RMS energy) — drop pure silence
    → pill = processing
    → stt.transcribe() — multipart POST to faster-whisper-server
                         /v1/audio/transcriptions

if enhance_enabled:
    → pill = enhancing
    → if screen_context: capture active display + paint red cursor
      marker → JPEG base64
    → llm.enhance() — OpenAI-shape POST to telecode proxy (:1235)
                      with JSON-schema response_format
    → 4-stage JSON recovery for malformed responses
    → LRU cache (50 entries) keyed on (transcript, screenshot fingerprint)

→ pill = typing
→ typer.type_text() — write to clipboard, send Ctrl+V via PowerShell
                      SendKeys, restore previous clipboard contents
→ history.add() — append to data/history.json (last 500)
→ pill = idle
```

All state transitions cross thread boundaries via Qt signals:

- **Main thread**: Qt event loop (tray, pill, settings window)
- **Worker thread**: dedicated asyncio loop for HTTP + subprocess work
- **Pynput thread**: raw keyboard input hook

Quit uses an `os._exit(0)` watchdog (5 s) so stuck pipelines or sticky
child processes can't prevent shutdown. Whisper and Kokoro are killed
with `taskkill /T` first, then `/F` if needed.

---

## Tray menu

```
⬡/⬢ Whisper ▸ status + port + Restart
⬡/⬢ Kokoro  ▸ status + port + Restart
⬡/⬢ LLM     ▸ proxy model + Test Proxy Connection
─
Open Settings Window   (default left-click)
─
Quit VoxType
```

The Settings window (left-click tray, or Open Settings Window) is a
frameless dark window with a sidebar:

- **Dictation** — hotkey mode, auto-stop on silence, VAD, append mode, save history
- **Services** — Whisper + Kokoro enabled / port / model / device with Restart buttons
- **LLM** — enhance on/off, screen context, proxy URL + model, Test Proxy Connection
- **About** — version, data-dir path, sidecar info

Every toggle writes through to `data/settings.json` atomically and
calls `config.reload()` — effective on the next request. Port changes
(Whisper, Kokoro) take a Restart click.

---

## Data layout

All user state lives under the repo:

```
voxtype/data/
  settings.json      # AppSettings — auto-created on first run
  history.json       # last 500 transcripts (if save_history=true)
  voxtype.log        # current session
  voxtype.log.prev   # previous session (rotated on restart)
```

Override with `VOXTYPE_DATA_DIR=C:\some\other\path` if you want storage
outside the repo. `voxtype/data/` is gitignored so your settings never
travel with a commit.

---

## LLM enhancement

`settings.json` fields:

```json
{
  "enhance_enabled": true,
  "screen_context":  true,
  "proxy_url":       "http://127.0.0.1:1235",
  "proxy_model":     "qwen3.5-35b"
}
```

`proxy_model` can be anything telecode's llamacpp registry recognises,
OR anything in `proxy.model_mapping` (e.g. `claude-opus-4-6` if you've
mapped it to a local model). VoxType sends OpenAI-shape
`/v1/chat/completions` with `response_format: json_schema` — the model
returns structured output and VoxType extracts the `output` field.

If the request fails, the **original Whisper transcript** is returned
unchanged — dictation keeps working even when the LLM is unreachable.

---

## Hotkey

Defaults to **Ctrl + Win** (hold). Edit `settings.json`:

```json
"hotkey": { "key1": "ctrl", "key2": "cmd", "label": "Ctrl + Win" }
```

Valid key names: `ctrl`, `shift`, `alt`, `cmd` (Win), `space`, `enter`,
`tab`, `esc`, `f1`–`f12`, single letters/digits. Set `key2` to `null`
for a single-key hotkey like `f9`.

`hotkey_mode` can be `"hold"` (dictate while held) or `"toggle"` (tap
to start, tap to stop).

---

## Uninstall

```powershell
.\uninstall.ps1
```

Removes the scheduled task, kills orphaned child processes, and
(interactively) offers to delete the install directory and repo-local
`voxtype/data/`.

---

## Known limitations

- **Windows-only.** Typer uses PowerShell SendKeys; screen capture
  uses Win32 `GetCursorPos`. A future cross-platform port would swap
  those out for `pyautogui.typewrite` + `mss` without the Win32 bits.
- **No rebind UI yet.** The hotkey is edited in `settings.json`. A
  "capture next 1–2 keys" flow exists in `hotkey.py` but isn't wired
  to a UI button yet.
- **No live mic device picker.** sounddevice picks the system default.
- **Kokoro is unused by VoxType itself** — it's kept as a managed
  sidecar for other tools that want TTS on `localhost:${port}`.
