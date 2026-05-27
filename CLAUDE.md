# VoxType — engineering notes

User-facing docs (model tables, install, options): [README.md](README.md).
This file is the LLM-targeted summary of how the code is wired.

## What this is

Pure-Python / PySide6 voice-dictation overlay for Windows. Hold a
hotkey, speak, release — cleaned transcript pastes at the cursor.
STT and TTS run in a **single shared torch worker subprocess**
(`engine_worker`), one generic backend per modality, dispatching to
family handlers by sniffing HuggingFace `config.json`. The GUI process
never imports torch — it spawns the worker and talks to it over a
localhost socket (`engine_ipc` framing, `engine_host` manager). This
lets the worker **exit on idle to free the CUDA context** (see
"Engine worker" below). An embedded aiohttp server (default `:6600`)
exposes both engines on OpenAI-compatible endpoints. LLM transcript
cleanup is routed through telecode's proxy at `:1235`.

For the full list of supported STT/TTS families and per-family knobs,
see the tables in README.md.

## Project layout

```
voxtype/
├── setup.ps1 / uninstall.ps1     # Idempotent installer / reverse
├── README.md / CLAUDE.md
├── tests/                        # stdlib unittest (per-test sandboxed)
└── voxtype/
    ├── main.py                   # Qt loop + asyncio worker + pynput
    ├── types.py                  # AppSettings (+ stt_opts/tts_opts/sounds)
    ├── config.py                 # JSON I/O + dotted patch
    ├── audio.py / hotkey.py / vad.py / screen_capture.py / typer.py
    ├── history.py / debug_log.py
    ├── sounds.py                 # Fire-and-forget audio cues
    ├── stt_engine.py / tts_engine.py / stt.py   # IPC proxies (no torch)
    ├── engine_ipc.py             # Length-prefixed JSON+binary socket framing
    ├── engine_worker.py          # Torch subprocess: both backends + 2-stage idle
    ├── engine_host.py            # GUI-side worker manager (spawn/respawn/poll)
    ├── server.py                 # Embedded aiohttp /v1/audio/* server
    ├── llm.py                    # OpenAI-shape POST to telecode
    ├── process.py                # Facade over engines for tray UI
    ├── qt_theme.py / tray_menu.py / pill_window.py / settings_window.py
    ├── resources/                # icon.png, system-prompt.md, models.json
    ├── data/                     # User state (gitignored)
    └── backends/
        ├── __init__.py           # Registry — only `generic` is registered
        ├── stt_base.py           # STTBackend ABC + LoadConfig + OptionSpec
        ├── tts_base.py           # TTSBackend ABC + TTSLoadConfig + OptionSpec + VoiceEntry
        ├── shared.py             # Whisper 99-language table
        ├── family_detect.py      # Family detection + per-family options + voice catalogs
        ├── generic_stt.py        # All STT family handlers inline
        └── generic_tts.py        # All TTS family handlers inline
```

## Runtime architecture

```
GUI process (no torch)
  Main thread          Qt event loop — widgets, tray, pill, signal delivery
  voxtype-asyncio      asyncio loop — HTTP server, llm.enhance, IPC to worker
  engine-host-poller   polls worker status every 1.5s, caches it, fans out
  pynput thread        raw keyboard hook
engine_worker process (owns the CUDA context)
  accept loop + per-connection threads; one lock serializes torch work;
  worker-idle thread runs the two-stage idle monitor
```

Engine calls (`configure/ensure_loaded/transcribe/synthesize/unload`) are
async on the GUI side; they run the blocking socket round-trip in a
default executor and forward to the worker. `get_status()`/`idle_info()`
are sync (called from the Qt thread every second) and read ONLY the
poller's cached snapshot — never do blocking IPC there.

Cross-thread handoff uses Qt signals or `QTimer.singleShot(0, …)`.
**Engine status callbacks fire on the executor thread — never touch Qt
widgets from them directly.** The settings UI polls
`engine.get_backend().detected_family()` from a Qt-thread QTimer. The
Detect button uses a `QObject` + `Signal` bridge to marshal worker
results back into the GUI thread.

Pipeline state machine (PillState):
`idle → recording → processing → [enhancing →] typing → idle`. On
failure → `error` for 2 s → `idle`.

**Engine worker + two-stage idle.** Why a subprocess at all:
`torch.cuda.empty_cache()` only returns the allocator's cached blocks;
the **CUDA context** (~300-600 MB of context + kernels + cuBLAS/cuDNN
workspaces) stays resident for the life of the process that first
touched CUDA. The only way to give it back is to exit that process. So
torch lives in `engine_worker` and the worker's idle monitor (every
2 s) is two-stage (modeled on docgraph's daemon):
  1. per-modality `*_idle_unload_sec` → drop that model's weights (the
     big VRAM chunk); reload lazily on the next request.
  2. `engine_idle_exit_sec` once BOTH models are unloaded → the worker
     **exits the process**, releasing the CUDA context. `engine_host`
     respawns it on the next `transcribe`/`synthesize` (lazy spawn).
The status poller uses `request(..., spawn=False)` so polling never
resurrects an idle-exited worker. The worker is bound to the
kill-on-close Job Object (`process.bind_to_lifetime_job`) so it dies
with the GUI; `process.stop_all()` also calls `engine_host.stop()`.
`engine.idle_info()` reports stage-1's `(idle_unload_sec, remaining)`
(from the cached status) for the "Live state" tile. The tile shows both
stages (mirroring telecode's docgraph tile): **Ready** + `Auto-unload in
Ns` bar (stage 1, weights) → **Warm** + `Release GPU in Ms` bar (stage 2;
worker alive, context cached, from the status `exit_remaining`) →
**Idle** "GPU fully released" (worker exited). Because the worker is
shared, a card shows "Warm" with no release bar while the OTHER modality
is still loaded (the context can't free until both unload).

**Log messages must stay ASCII-safe.** `debug_log` reconfigures the
stderr handler with `errors="backslashreplace"` because the Windows
console is cp1252 — a non-cp1252 glyph (`≥`, em-dash, `…`) in a log
line otherwise raises `UnicodeEncodeError` that escapes
`logging.handleError` and propagates out of the `log.*()` call,
killing the calling thread. This is exactly what silently killed the
idle watcher (its message contained `≥`/`—`) so auto-unload never
fired. Keep new log strings ASCII regardless. The worker logs to its
own `data/voxtype-worker.log` (separate file so the two processes don't
fight over `voxtype.log`).

## Generic backend dispatcher

`backends/generic_stt.py` and `backends/generic_tts.py` are the only
registered backends. Each is a thin dispatcher: `load_sync` calls
`fd.detect_*_family()`, picks the handler class, and falls back to
`_GenericPipelineHandler` if the family-specific loader throws
(missing optional dep, exotic arch). Family handlers are inline
classes inside the same file — no shared state across families.

## Family detection (`backends/family_detect.py`)

Three layers, fast → slow:
1. **Local config.json** for paths that exist on disk.
2. **Repo-id substring heuristic** — sync, ~0 ms. Used by the UI on
   every `textChanged` so the family pill + voice picker update
   without blocking on network.
3. **HuggingFace API** (`/api/models/<id>` + `/resolve/main/config.json`,
   3 s timeout) — triggered by the **Detect** button for verification.

Per-family metadata helpers consumed by the UI:
`stt_capabilities / tts_capabilities` (gate universal widgets),
`stt_runtime_options / tts_runtime_options` (list[OptionSpec] for the
Advanced section), `tts_voices_for_family` (static voice catalogs
for Kokoro / Bark / Parler / SpeechT5), `stt_family_label /
tts_family_label` (status pill text).

## Option-spec UI

Every per-family UI knob is an `OptionSpec(key, kind, label, default,
help, choices?, min?, max?, step?, rebuild?)` (defined in
`stt_base.py` / `tts_base.py`).
`settings_window._render_option(spec, "stt_opts"|"tts_opts")` maps
the spec to a Qt widget bound to `<bag>.<spec.key>` via
`config.patch()`. **Adding a new family option requires editing only
`family_detect.py`** — no UI code changes.

## Settings shape (`types.py`)

```python
@dataclass
class AppSettings:
    # Recording behaviour
    hotkey_mode: HotkeyMode = "hold"
    hotkey: HotkeyCombo = field(default_factory=HotkeyCombo)
    auto_stop_on_silence: bool = True
    silence_duration_sec: float = 1.5
    vad_enabled: bool = True
    append_mode: bool = False

    # Audio cues (empty path = built-in tone from voxtype.sounds)
    sounds_enabled: bool = True
    sound_start: str = ""
    sound_stop: str = ""
    sound_done: str = ""

    # Pill UI / HTTP server
    pill_x: int = -1; pill_y: int = -1; pill_hidden: bool = False
    server_enabled: bool = True; server_port: int = 6600

    # Universal STT (every family honours these)
    stt_enabled / stt_auto_start / stt_idle_unload_sec
    stt_model_path / stt_device / stt_language / stt_dtype
    stt_warmup / stt_torch_compile / stt_attn_impl
    stt_chunk_length_s / stt_stride_length_s
    stt_opts: dict   # family-specific (e.g. {"task": "translate", "num_beams": 5})

    # Universal TTS
    tts_enabled / tts_auto_start / tts_idle_unload_sec
    tts_model_path / tts_device / tts_voice / tts_speed
    tts_warmup / tts_torch_compile / tts_stream / tts_attn_impl / tts_seed
    tts_opts: dict   # family-specific (e.g. {"style": "...", "speaker_embedding": "..."})

    # LLM + history
    enhance_enabled / screen_context / proxy_url / proxy_model
    save_history
```

`AppSettings.from_json()` migrates legacy keys (`stt_task` →
`stt_opts.task`, `tts_speaker` → `tts_voice`, `tts_length_scale` →
`tts_speed`, legacy `stt_backend`/`tts_backend` → `"generic"`).

`config.patch("stt_opts.task", "translate")` dotted writes land in
the opts dict; flat keys still work for top-level fields.

## Audio cues (`sounds.py`)

`sounds.play(cue, custom_path="")` is fire-and-forget on a daemon
thread. Cues: `"start"` (on hotkey-down after recorder starts),
`"stop"` (on hotkey-up after VAD passes, before "processing"),
`"done"` (end of pipeline before final idle). Defaults are 1-second
WAVs bundled in `voxtype/resources/sounds/{start,stop,done}.wav`,
generated by `scripts/gen_sounds.py` (regenerate if you want to
tweak the chimes). Playback uses Windows' built-in
`winsound.PlaySound(SND_FILENAME | SND_ASYNC | SND_NODEFAULT)` for
WAVs — far more reliable than sounddevice for short cues when no
PortAudio output stream is already open. Non-WAV custom paths
fall back to `sounddevice` + `soundfile`. Failures are logged and
swallowed.

## Embedded HTTP server (`server.py`)

aiohttp app started in `_boot_engines()`. Routes:
```
POST /v1/audio/transcriptions   →  stt_engine.transcribe (multipart)
POST /v1/audio/speech           →  tts_engine.synthesize (JSON in, WAV out)
GET  /v1/models                 →  engine list
GET  /health                    →  engine readiness snapshot
GET  /                          →  liveness probe
```
The `model` field is accepted-but-ignored. The `voice` field on
`/v1/audio/speech` IS honoured if it matches the loaded backend's
catalog; otherwise the configured default is used.

## Tests

Stdlib `unittest`, per-test `VOXTYPE_DATA_DIR` sandbox so the real
`voxtype/data/settings.json` is never touched.

```powershell
.\voxtype-venv\Scripts\python.exe -m unittest discover tests
```

Coverage: family-detect heuristics, settings migration, dotted
`config.patch`, backend registry, `models.json` integrity, engine
opts filtering.

## Setup script (`setup.ps1`)

Single venv `voxtype-venv/`. Idempotent. Phases: prereq checks →
install torch (CUDA wheel index per `-CudaVersion`) → install
`voxtype/requirements.txt` → pre-download default models → register
`VoxType` scheduled task at logon → seed `data/settings.json`.

Flags: `-InstallDir`, `-GpuSupport`, `-CudaVersion cu130|cu124|cpu`,
`-FlashAttn`. See README.md for the full table.

## Dependencies

Core: `torch`, `transformers`, `sentencepiece`, `datasets`,
`huggingface_hub`, `kokoro` (the one TTS family with a non-HF
loader), `PySide6`, `pynput`, `sounddevice`, `soundfile`, `aiohttp`,
`numpy`, `Pillow`, `mss`, `pywin32`. Optional family deps
(`parler-tts`, `phonemizer`, `espeak-ng`) fall through to the
pipeline fallback when missing.

## Testing the running app

```powershell
Stop-ScheduledTask -TaskName VoxType
.\voxtype-venv\Scripts\python.exe -m voxtype
```

Smoke: tray icon appears · `data/voxtype.log` starts filling · first
hotkey: pill goes red → amber (loading) → green text · Settings →
Services cards show detected family · `curl
http://127.0.0.1:6600/health` returns engine status JSON.
