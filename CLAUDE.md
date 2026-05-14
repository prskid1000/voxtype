# VoxType — engineering notes

User-facing docs: [README.md](README.md).

## What this is

VoxType is a **pure-Python / PySide6** voice-dictation overlay for
Windows. Hold a hotkey, speak, release — the cleaned transcript is
pasted at the cursor. STT and TTS both run **in-process via PyTorch**
— one ML backend, one venv. STT uses HuggingFace `transformers` (any
Whisper-family ONNX-exported repo or PyTorch repo works); TTS uses
the `kokoro` PyPI package wrapping Kokoro-82M. An embedded aiohttp
server exposes both on one OpenAI-compatible port (`:6600` by
default) so external clients reach VoxType over standard HTTP.

LLM transcript cleanup is routed through
**telecode's dual-protocol proxy** (`http://127.0.0.1:1235`).

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
    ├── stt_engine.py          # transformers.WhisperForConditionalGeneration + torch
    ├── tts_engine.py          # kokoro.KPipeline + torch
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
voxtype-stt          single-thread executor — torch STT inference
voxtype-tts          single-thread executor — torch TTS synthesis
pynput thread        raw keyboard hook
```

Cross-thread handoff is Qt signals (pill state) or
`QTimer.singleShot(0, lambda: …)` (pulling async results back to the
Qt thread).

## Pluggable engine backends

`stt_engine.py` and `tts_engine.py` are **thin orchestrators**. The
actual model code lives in `voxtype/backends/<name>.py` and implements
the `STTBackend` / `TTSBackend` ABC defined in
`backends/stt_base.py` / `backends/tts_base.py`.

The orchestrator owns:
- load / unload locking + idle-unload watcher
- status listeners + the `_key()` rebuild trigger
- the single-thread `ThreadPoolExecutor` for inference
- the async/sync bridge (sync chunk generator → async queue for streaming)

The backend owns:
- the actual library import (`transformers`, `faster_whisper`, `kokoro`, `piper`)
- model resolution rules (HF repo / local path / curated voice id)
- inference (sync, runs in the executor)
- the voice / language catalog (used by the UI to build pickers)
- capability flags via `supports("torch_compile" / "bf16" / "initial_prompt" / …)`

### Currently shipped backends

**STT** (registered in `backends/__init__.py`):
  - **`whisper`** — `transformers.WhisperForConditionalGeneration` +
    `AutoProcessor`. Broadest feature set (initial_prompt, torch.compile,
    bf16, translate-to-EN). Default: `openai/whisper-base`.

**TTS** (registered in `backends/__init__.py`):
  - **`kokoro`** — `kokoro.KPipeline`. 54 voices across 9 lang_codes.
    PyTorch, native per-sentence streaming. Default: `hexgrad/Kokoro-82M`.

The pluggable framework is in place; alternative backends
(faster-whisper, piper, parakeet, coqui, …) will be added when
there's a concrete need.

### Adding a backend

1. Create `voxtype/backends/<name>.py` subclassing the ABC.
2. Add one `_register_*("<name>", "voxtype.backends.<name>", "<Class>")`
   line in `backends/__init__.py`.
3. Append the optional dep to `voxtype/requirements.txt`.

Backends that fail to import (missing optional dep) are silently
skipped by the registry — the UI just doesn't list them.

### Engine API (unchanged by the backend split)

```python
eng = stt_engine.get_engine()
await eng.configure(settings)       # picks backend + applies all knobs
await eng.ensure_loaded()           # lazy on first request
text = await eng.transcribe(pcm)
await eng.unload()
```

Each engine:
- Accepts either a local path or a HF repo ID as the model setting.
  Repo IDs are auto-downloaded via `huggingface_hub` to
  `~/.cache/huggingface/hub` on first load.
- Holds a single `ThreadPoolExecutor(max_workers=1)` so concurrent
  inference can't OOM the GPU.
- Tracks a `_loaded_key` tuple — `configure()` calls that change the
  key trigger an automatic unload so the next request rebuilds.
- Spawns an idle-watcher thread that calls `unload()` after
  `idle_unload_sec` of inactivity.
- Resolves `device='cuda'` against `torch.cuda.is_available()` with
  silent CPU fallback + a warning log.

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

The `model` and `voice` request fields are **accepted but ignored** —
VoxType decides the model + voice via its own settings. External
clients only address VoxType by host + port.

Decodes WAV directly; falls through to `soundfile` for other formats.
Resamples to 16 kHz mono int16 PCM before handing to the engine.

## Settings & hot-reload

`voxtype/config.py` exposes `load()`, `save()`, `reload()`, `patch()`.
Every UI toggle calls `config.patch("path.to.key", value)` which:

1. Mutates the cached `AppSettings`
2. Atomically writes `data/settings.json` (tmp + `os.replace`)
3. Any subsequent `config.load()` sees the new value

**Forces a model rebuild** (engine `_key()` includes these):
stt_model_path, stt_device, stt_dtype, stt_torch_compile,
tts_model_path, tts_device, tts_torch_compile.
`configure()` notices the key change and unloads so the next request
rebuilds.

**Applied per-call, no rebuild**: stt_language, stt_task, stt_num_beams,
stt_initial_prompt, tts_speaker, tts_length_scale, tts_stream.

**Applied on next load only**: stt_warmup, tts_warmup.

**Settings → engine propagation**: `configure()` runs (a) at boot, (b)
before every local-hotkey STT press, (c) on tray Load/Reload, AND (d)
at the start of every `/v1/audio/transcriptions` and `/v1/audio/speech`
HTTP request. So changes made in the UI take effect on the next call
no matter how STT/TTS is invoked — no tray Reload required.

**Requires server restart**: server_port. Use the "Restart" button in
the Server card.

## Settings shape (key fields)

```python
@dataclass
class AppSettings:
    # Embedded HTTP server
    server_enabled: bool = True
    server_port: int = 6600

    # STT (pluggable; currently ships "whisper")
    stt_enabled: bool = True
    stt_auto_start: bool = True
    stt_idle_unload_sec: int = 300
    stt_backend: STTBackendName = "whisper"
    stt_model_path: str = "openai/whisper-base"
    stt_device: TorchDevice = "cpu"
    stt_language: str = "en"
    stt_task: STTTask = "transcribe"          # or "translate" → EN
    stt_dtype: TorchDtype = "auto"            # auto / fp32 / fp16 / bf16
    stt_num_beams: int = 1                    # 1 = greedy / fastest
    stt_initial_prompt: str = ""              # bias decoder with jargon
    stt_warmup: bool = True                   # dummy infer after load
    stt_torch_compile: bool = False           # +20-40% steady-state

    # TTS (pluggable; currently ships "kokoro")
    tts_enabled: bool = False
    tts_auto_start: bool = False
    tts_idle_unload_sec: int = 600
    tts_backend: TTSBackendName = "kokoro"
    tts_model_path: str = "hexgrad/Kokoro-82M"
    tts_device: TorchDevice = "cpu"
    tts_speaker: str = "af_heart"
    tts_length_scale: float = 1.0
    tts_warmup: bool = True
    tts_torch_compile: bool = False           # ~15% steady-state win
    tts_stream: bool = False                  # chunked WAV reply
```

## Tray + Settings UI

Sidebar sections:
- **Dictation** — hotkey, VAD, etc.
- **Services** — three cards, each with a footer row containing a
  live status pill and lifecycle buttons:
  - **OpenAI HTTP Server** — Start / Stop / Restart
  - **STT** — Load / Unload / Reload
  - **TTS** — Load / Unload / Reload
- **LLM** — proxy URL/model + Test Proxy
- **History** — saved transcripts
- **Logs** — live tail

Tray submenus mirror STT / TTS / LLM with status + Load / Unload / Reload.

## Setup script (`setup.ps1`)

Single venv. Idempotent. Parameters:

| Flag | Default | What it does |
|---|---|---|
| `-InstallDir <path>` | `~/.voxtype` | Where the venv + scheduled task land |
| `-GpuSupport $true\|$false` | `$true` | Install GPU torch wheel vs CPU wheel |
| `-CudaVersion cu130\|cu124\|cpu` | `cu130` | Which torch CUDA wheel index. `cu130` = nightly, `cu124` = stable |

Phases:

1. **Prereqs**: Python 3.10–3.12, git, ffmpeg (warn-only), GPU detection.
2. **Single venv**: `voxtype-venv/`. Installs `torch` first from the
   right `download.pytorch.org/whl/...` index (CUDA 13 nightly, CUDA
   12.4 stable, or CPU), then everything else from
   `voxtype/requirements.txt`.
3. **Pre-download default models**: `openai/whisper-base` (~145 MB)
   and `hexgrad/Kokoro-82M` (~327 MB) via
   `huggingface_hub.snapshot_download`. Idempotent — already-cached
   files skip. Network failure is non-fatal.
4. **Scheduled task** `VoxType`: runs `pythonw.exe -m voxtype` at logon.
5. **Seed settings**: `data/settings.json` with AppSettings defaults.

## Dependencies (see `voxtype/requirements.txt`)

- **torch** — single ML backend. Installed from the torch wheel
  index matching the requested CUDA version (or CPU). Bundles its
  own CUDA runtime — no separate CUDA toolkit install needed.
- **transformers** — STT (`WhisperForConditionalGeneration` + `AutoProcessor`).
- **kokoro** — TTS (`KPipeline`). Pulls in misaki (G2P) + uses
  system `espeak-ng` for non-English fallback.
- **huggingface_hub** — model auto-download.
- **PySide6**, **pynput**, **sounddevice**, **soundfile**, **aiohttp**,
  **numpy**, **Pillow**, **mss**, **pywin32**.

## Testing

```powershell
Stop-ScheduledTask -TaskName VoxType
.\voxtype-venv\Scripts\python.exe -m voxtype
```

Smoke test:
- Tray icon appears
- `data/voxtype.log` starts filling (look for `stt ready (device=cuda dtype=torch.float16)`)
- First hotkey: pill goes red → amber (loading) → green text
- `curl http://127.0.0.1:6600/health` returns engine status JSON
- `curl http://127.0.0.1:6600/v1/models` lists `whisper-1` + `tts-1`
- With telecode up: filler words cleaned in the final paste

## What changed in the PyTorch refactor

Removed:
- `sherpa-onnx`, `optimum`, `onnxruntime`, `onnxruntime-gpu` deps
- `stt_quant` setting (no quant variant juggling — transformers + torch
  handles dtype via `torch_dtype=torch.float16` on GPU).
- The `model` / `voice` request-field plumbing on the HTTP server.
  VoxType decides everything via its own settings.

Added:
- `torch` (bundles CUDA runtime — no toolkit install needed)
- `kokoro` + `misaki` (TTS pipeline + G2P)
- Voice name field (`af_heart`, `jm_kumo`, …) replacing the integer
  speaker index.
- `setup.ps1 -CudaVersion` flag picking the torch wheel channel.
