# VoxType — engineering notes

User-facing docs: [README.md](README.md).

## What this is

VoxType is a **pure-Python / PySide6** voice-dictation overlay for
Windows. Hold a hotkey, speak, release — the cleaned transcript is
pasted at the cursor. STT and TTS both run **in-process via ONNX
Runtime** — no separate sidecar subprocesses, no separate venvs. An
embedded aiohttp server exposes both on one OpenAI-compatible port
(`:6600` by default) so external clients (telecode, MCP tools) can
reach VoxType over standard HTTP.

The original Electron/React implementation (`voxtype/src/`) has been
deleted. All Node / npm / Electron / LM Studio references are gone.

LLM transcript cleanup is routed through
**telecode's dual-protocol proxy** (`http://127.0.0.1:1235`) — see
https://github.com/prskid1000/telecode. No direct LM Studio
integration, no `lms` CLI calls, no model-list fetching. Whichever
model telecode is supervising becomes VoxType's enhance backend.

## Project layout

```
voxtype/
├── setup.ps1                  # Idempotent installer: single venv + scheduled task
├── uninstall.ps1              # Reverse of setup
├── README.md                  # User-facing docs
├── CLAUDE.md                  # This file
├── LICENSE                    # MIT
├── .gitignore
└── voxtype/                   # The Python package
    ├── __init__.py
    ├── __main__.py            # `python -m voxtype` entry point
    ├── main.py                # Orchestrator (Qt loop + asyncio worker + pynput thread)
    ├── types.py               # AppSettings / HotkeyCombo / PillState
    ├── config.py              # JSON I/O (voxtype/data/settings.json, hot-reload)
    ├── debug_log.py           # Rotating logger
    │
    ├── audio.py               # sounddevice → 16 kHz mono int16 PCM
    ├── hotkey.py              # pynput keyboard listener
    ├── vad.py                 # numpy RMS energy gate on PCM
    ├── screen_capture.py      # mss + PIL, red cursor marker
    ├── typer.py               # Clipboard + Ctrl+V via PowerShell SendKeys
    ├── history.py             # Append-only JSON
    │
    ├── stt_engine.py          # In-process STT — sherpa-onnx + HF auto-download
    ├── tts_engine.py          # In-process TTS — sherpa-onnx Kokoro + HF auto-download
    ├── server.py              # Embedded aiohttp /v1/audio/* server
    ├── stt.py                 # Shim → delegates to stt_engine
    ├── llm.py                 # OpenAI-shape POST to telecode proxy
    ├── process.py             # Facade over engines for tray UI + Job Object utilities
    │
    ├── qt_theme.py            # Dark QSS
    ├── tray_menu.py           # QSystemTrayIcon + submenus (STT / TTS / LLM)
    ├── pill_window.py         # Frameless always-on-top status pill
    ├── settings_window.py     # Frameless sidebar settings window
    │
    ├── requirements.txt
    ├── resources/
    │   ├── icon.png
    │   └── system-prompt.md   # LLM cleanup instructions
    └── data/                  # User state — gitignored
        ├── settings.json
        ├── history.json
        ├── voxtype.log
        └── voxtype.log.prev
```

## Runtime architecture

```
Main thread          Qt event loop — widgets, tray, pill, signal delivery
voxtype-asyncio      asyncio loop — HTTP server, llm.enhance, engine load/unload
voxtype-stt          single-thread executor — sherpa-onnx inference
voxtype-tts          single-thread executor — ONNX synthesis
pynput thread        raw keyboard hook
```

Cross-thread handoff is Qt signals (pill state) or
`QTimer.singleShot(0, lambda: …)` (pulling async results back to the
Qt thread). Never call Qt widget methods directly from worker threads.

Dictation pipeline (`main.py:_pipeline`):

```
hotkey down  → recorder.start, pill = recording
hotkey up    → recorder.stop → PCM buffer → VAD gate
             → loop.submit(_pipeline(pcm, s))
               → stt.transcribe(pcm, language=s.stt_language)
                 ↳ direct call into stt_engine.STTEngine
                   (no HTTP — the server is for external clients only)
               → if enhance_enabled: await llm.enhance()
               → loop.run_in_executor(None, type_text, final)
               → history.add(...)
               → emit pill_state_req('idle', '')
```

## In-process engines

Both `stt_engine.py` and `tts_engine.py` are singletons, both ONNX
Runtime backed, both with the same lifecycle API:

```python
from voxtype import stt_engine
eng = stt_engine.get_engine()
await eng.configure(settings)       # apply model_path / device / language
await eng.ensure_loaded()           # load the model (lazy on first request)
text = await eng.transcribe(pcm)    # inference in single-thread executor
await eng.unload()                  # release model, gc.collect
```

Both engines run on `sherpa-onnx` (a thin wrapper over `onnxruntime`).
One dependency, both engines. STT supports any sherpa-onnx Whisper /
Paraformer / Zipformer / SenseVoice export. TTS supports Kokoro / VITS-
Piper / Matcha-TTS exports. Defaults: Whisper Large V3 Turbo for STT
(`csukuangfj/sherpa-onnx-whisper-turbo`, multilingual) and Kokoro
multi-lang v1.1 for TTS (`csukuangfj/kokoro-multi-lang-v1_1`, 103
voices, English + Chinese).

Each engine:
- Accepts either a **local path** or a **HuggingFace repo ID** as the
  model setting. Repo IDs are auto-downloaded via `huggingface_hub.
  snapshot_download()` to the HF cache on first load and reused after.
- Holds a single `ThreadPoolExecutor(max_workers=1)` so concurrent
  inference can't OOM the GPU
- Tracks a `_loaded_key` tuple — `configure()` calls that change the
  key trigger an automatic unload so the next request rebuilds with
  the new settings
- Spawns an idle-watcher thread that calls `unload()` after
  `idle_unload_sec` of inactivity (0 = never unload)
- CPU/CUDA switching is the ONNX Runtime provider list. If
  `onnxruntime-gpu` isn't installed or CUDA init fails, ORT silently
  drops to the CPU provider — no manual fallback logic needed

`process.py` is now a thin facade — `get_status("whisper")` / 
`start_whisper(s)` / `restart_service("tts", s)` route through to the
engine singletons. The Job Object + kill_process_tree utilities are
kept for future subprocess additions but aren't wired to anything
right now.

## Embedded HTTP server (`server.py`)

aiohttp app, starts in `_boot_engines()` via `server.start(port=...)`.
Routes:

```
POST /v1/audio/transcriptions   →  stt_engine.transcribe (multipart)
POST /v1/audio/speech           →  tts_engine.synthesize (JSON in, WAV out)
GET  /v1/models                 →  engine list
GET  /health                    →  engine readiness snapshot
GET  /                          →  liveness probe
```

Decodes WAV directly; falls through to `soundfile` for other formats.
Resamples to 16 kHz mono int16 PCM before handing to the engine.
Single port — both routes live on the same server.

The dictation hot path inside VoxType calls the engines directly. The
server is purely for external consumers (telecode `voice/stt.py` +
`voice/tts.py`, the MCP `transcribe` / `speak` tools, anything else
that speaks OpenAI's audio API).

## Settings & hot-reload

`voxtype/config.py` exposes `load()`, `save()`, `reload()`, `patch()`.
Every UI toggle calls `config.patch("path.to.key", value)` which:

1. Mutates the cached `AppSettings`
2. Atomically writes `data/settings.json` (tmp + `os.replace`)
3. Any subsequent `config.load()` sees the new value

**Immediate effect**: enhance toggle, VAD, append mode, proxy
URL/model, system prompt content (re-read on every `llm.enhance`).

**Effect on next inference call**: stt_model_path, stt_device,
tts_model_path, tts_device. The engine's `configure()` notices the
key change and unloads so the next request rebuilds.

**Requires server restart**: server_port. The "Reload STT" / "Reload TTS"
buttons are the easiest way to apply changes immediately.

## LLM enhance (`llm.py`)

Unchanged. Posts to `{proxy_url}/v1/chat/completions` with
`response_format: json_schema`. LRU cache (50 entries), 4-stage JSON
recovery, 2-retry loop. On failure, returns the raw STT transcript
verbatim.

## Tray + Settings UI

Sidebar sections:
- **Dictation** — hotkey mode, Rebind button, auto-stop, VAD, append, save history
- **Services** — three cards (symmetric STT + TTS):
  - **OpenAI HTTP Server** (port, enabled)
  - **STT** (model field accepts HF repo or local path + Browse + Check, device, language, Reload)
  - **TTS** (model field accepts HF repo or local path + Browse + Check, device, speaker, length scale, Reload)
- **LLM** — enhance, screen context, proxy URL/model, Test Proxy
- **History** — saved transcripts with Copy buttons
- **Logs** — live tail

Tray submenus mirror the three upstream concerns (STT / TTS / LLM)
with status + Load / Unload / Reload actions.

## Setup script (`setup.ps1`)

Single venv. Idempotent — re-running on a fully-installed machine
takes ~10 s.

1. **Prereqs**: Python 3.10+, git, ffmpeg (warn-only), GPU detection
2. **Single venv**: `voxtype-venv/` + `pip install -r voxtype/requirements.txt`
   (PySide6, pynput, sounddevice, aiohttp, sherpa-onnx, huggingface_hub)
3. **GPU runtime** (if `-GpuSupport $true`): swap CPU `onnxruntime` for
   `onnxruntime-gpu` so both STT and TTS land on CUDA when `device='cuda'`
4. **Pre-download default models**: snapshot_download via huggingface_hub
   for `csukuangfj/sherpa-onnx-whisper-turbo` and
   `csukuangfj/kokoro-multi-lang-v1_1`. Idempotent — already-cached files
   skip. Network failure is non-fatal (engines download lazily on first
   use).
5. **Scheduled task** `VoxType`: runs `pythonw.exe -m voxtype` at logon
6. **Seed settings**: `data/settings.json` with AppSettings defaults.
   Both engines have built-in defaults (`csukuangfj/sherpa-onnx-whisper-turbo`
   for STT, `csukuangfj/kokoro-multi-lang-v1_1` for TTS) so dictation
   works out of the box — settings are only for overrides.

Parameters: `-GpuSupport`, `-InstallDir`.

## What changed in the in-process refactor

Removed:
- `stt-venv/`, `tts-venv/`, `Kokoro-FastAPI/` clone
- `voxtype/kokoro_voice.py` (voice catalog gone — the model file IS the voice)
- `voxtype/whisper_model.py` (model dropdown gone — free-text repo/path)
- All Kokoro settings (`kokoro_*` keys)
- All Whisper-prefixed settings (`whisper_*` → `stt_*`)
- Per-service port fields (`whisper_port`, `kokoro_port` → single `server_port`)
- Subprocess lifecycle in `process.py` — `_spawn_whisper`, `_spawn_kokoro`,
  `_wait_ready`, `_drain`, `_force_cpu_restart`, GPU-broken stdout sniffer
- HTTP client in `stt.py` (replaced by direct engine delegate)
- `faster-whisper` dependency (STT now via `sherpa-onnx`)
- `piper-tts` dependency (TTS now via `sherpa-onnx` Kokoro)

Added:
- `voxtype/stt_engine.py` — `STTEngine` singleton, sherpa-onnx + HF auto-download
- `voxtype/tts_engine.py` — `TTSEngine` singleton, onnxruntime + HF auto-download
- `voxtype/server.py` — embedded aiohttp `/v1/audio/*` server
- `stt_*` and `tts_*` settings — symmetric structure (enabled, auto_start,
  idle_unload_sec, model_path, device, language/speaker)
- `server_enabled`, `server_port` settings

## Testing

```powershell
Stop-ScheduledTask -TaskName VoxType
.\voxtype-venv\Scripts\python.exe -m voxtype
```

Smoke test:
- Tray icon appears
- `data/voxtype.log` starts filling
- First hotkey: pill goes red → amber (whisper loading) → green text
- `curl http://127.0.0.1:6600/health` returns engine status JSON
- `curl http://127.0.0.1:6600/v1/models` lists `whisper-1` + `tts-1`
- With telecode up: filler words cleaned in the final paste
