# VoxType — engineering notes

User-facing docs: [README.md](README.md).

## What this is

VoxType is a **pure-Python / PySide6** voice-dictation overlay for
Windows. Hold a hotkey, speak, release — the cleaned transcript is
pasted at the cursor. The app owns the lifecycle of its sidecars
(faster-whisper-server, optionally Kokoro-FastAPI) — one process tree,
one scheduled task.

The original Electron/React implementation (`voxtype/src/`) has been
deleted. All Node / npm / Electron / LM Studio references are gone
from the tree. Grep for `electron`, `npm`, `lm.studio`, `voicemode` to
verify.

LLM transcript cleanup is routed through
**telecode's dual-protocol proxy** (`http://127.0.0.1:1235`) — see
https://github.com/prskid1000/telecode. No direct LM Studio
integration, no `lms` CLI calls, no model-list fetching. Whichever
model telecode is supervising (via `llamacpp.models` + `model_mapping`)
becomes VoxType's enhance backend.

## Project layout

```
voicemode-windows/
├── setup.ps1                  # Idempotent installer: venvs + scheduled task
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
    ├── debug_log.py           # Rotating logger (voxtype.log ↔ voxtype.log.prev)
    │
    ├── audio.py               # sounddevice → 16 kHz mono int16 PCM
    ├── hotkey.py              # pynput.keyboard.Listener (hold/toggle/capture, stale-key sweep)
    ├── vad.py                 # numpy RMS energy gate on PCM
    ├── screen_capture.py      # mss + PIL, red cursor marker (ctypes GetCursorPos)
    ├── typer.py               # Clipboard + Ctrl+V via PowerShell SendKeys
    ├── history.py             # Append-only JSON, last 500 entries
    │
    ├── stt.py                 # Multipart POST to faster-whisper-server
    ├── llm.py                 # OpenAI-shape POST to telecode proxy; JSON-schema; LRU cache
    ├── services.py            # Subprocess supervisor (Whisper + Kokoro, taskkill /T, auto-restart)
    ├── whisper_model.py       # Model catalog
    ├── kokoro_voice.py        # Voice catalog + warmup ping
    │
    ├── qt_theme.py            # Dark QSS (copy of telecode/tray/qt_theme.py)
    ├── tray_menu.py           # QSystemTrayIcon + submenus (Whisper / Kokoro / LLM)
    ├── pill_window.py         # Frameless always-on-top status pill
    ├── settings_window.py     # Frameless sidebar settings window
    │
    ├── requirements.txt
    ├── resources/
    │   ├── icon.png
    │   └── system-prompt.md   # LLM cleanup instructions (hot-read on every enhance)
    └── data/                  # User state — gitignored
        ├── settings.json
        ├── history.json
        ├── voxtype.log
        └── voxtype.log.prev
```

## Runtime architecture

Three threads cooperate:

| Thread | Owned by | Responsibility |
|---|---|---|
| Main | `QApplication.exec()` | Qt widgets, tray, pill, settings window, paint, signal delivery |
| `voxtype-asyncio` | `_AsyncLoopThread` in `main.py` | All HTTP (`stt.transcribe`, `llm.enhance`, `services._ping_once`) and subprocess management |
| pynput internal | `pynput.keyboard.Listener` | Raw keyboard hook + key-name normalisation |

Cross-thread handoff is **Qt signals** (pill state) or
`QTimer.singleShot(0, lambda: …)` (pulling async results back to the
Qt thread). Never call Qt widget methods directly from the worker
thread.

Pipeline (`main.py:_pipeline`):

```
hotkey down  (pynput thread)
  → QTimer.singleShot to Qt: recorder.start(), pill = recording
hotkey up    (pynput thread)
  → QTimer.singleShot to Qt: recorder.stop(), VAD gate
  → loop.submit(_pipeline(pcm, settings))
    → await stt.transcribe()
    → if enhance_enabled: await llm.enhance() (with optional screenshot)
    → await loop.run_in_executor(None, type_text, final)   # PowerShell blocking
    → history.add(...)
    → emit pill_state_req('idle', '')
```

Error path: `_flash_error(message)` → pill shows red for 2 s →
auto-transitions back to idle. The original Whisper transcript is
returned verbatim on any LLM failure so dictation keeps working.

## Settings & hot-reload

`voxtype/config.py` exposes `load()`, `save()`, `reload()`, `patch()`.
Every UI toggle calls `config.patch("path.to.key", value)` which:

1. Mutates the cached `AppSettings`
2. Atomically writes `data/settings.json` (tmp + `os.replace`)
3. Any subsequent `config.load()` sees the new value

**What takes effect immediately**: enhance toggle, VAD, append mode,
proxy URL/model, system prompt content (re-read on every
`llm.enhance` call).

**What needs a Restart click**: Whisper port/model/device, Kokoro
port/voice/device. Use the "Restart" button in the tray submenu or
the corresponding section of the settings window.

`VOXTYPE_DATA_DIR` env var overrides the default `voxtype/data/` path
if you want settings/history/logs somewhere else.

## Sidecar lifecycle (`services.py`)

Mirrors telecode's `llamacpp/supervisor.py` pattern.

- `start_whisper(cfg)` / `start_kokoro(cfg)` — spawn + `_drain` stdout
  into the log + probe `/health` up to 60 s
- `_watch_exit(m)` — auto-restart on unexpected exit with exponential
  backoff (1s, 2s, 4s, …, capped at 30s); suppressed when
  `m.stopping == True`
- `stop_service(name)` — `taskkill /PID <pid> /T` (graceful), 3 s
  wait, then `/F` if still alive
- `stop_all()` — concurrent stop of every managed service

All spawned children carry `subprocess.CREATE_NO_WINDOW` so they don't
pop a console under `pythonw.exe`.

## LLM enhance (`llm.py`)

Zero LM Studio references. Sends to
`{proxy_url}/v1/chat/completions` with:

```json
{
  "model": "<proxy_model>",
  "messages": [
    {"role": "system", "content": "<system-prompt.md>"},
    {"role": "user",   "content": "<text + optional image_url>"}
  ],
  "temperature": 0,
  "max_tokens": 4096,
  "response_format": {
    "type": "json_schema",
    "json_schema": { ... output ... }
  }
}
```

- **LRU cache** (50 entries) keyed on `(transcript, screenshot_fingerprint)`
- **4-stage JSON recovery**: strict → largest `{…}` block → regex
  extract of `"output": "..."` → raw text
- **2-retry loop** with linear backoff
- **Sanity checks**: empty `output` → original; 3× length blow-up →
  original

`proxy_alive(proxy_url)` does a GET on `/v1/models` so the tray can
show live reachability.

## Hotkey (`hotkey.py`)

- Stores canonical key names as strings (`ctrl`, `cmd`, `f9`, etc.) —
  **not** numeric keycodes. Portable across keyboard layouts.
- Stale-key sweep every 2 s drops any key held >5 s (Windows Start
  menu sometimes eats the keyup for Meta/Win).
- Capture mode (`hotkey.capture(cb)`) waits for 1–2 keys after the
  user has released everything currently held, then fires `cb(combo)`.
  Wired to a UI button is a TODO.

## Tray + Settings UI

QSS is a verbatim copy of `telecode/tray/qt_theme.py` so the two apps
feel identical. Sidebar sections:

- **Dictation** — hotkey mode, hotkey label (read-only for now),
  auto-stop, VAD, append, save history
- **Services** — Whisper + Kokoro enable/port/model/voice/device +
  Restart buttons
- **LLM** — enhance / screen context toggles, proxy URL + model, Test
  Proxy Connection
- **About** — version, data dir, sidecar info

Tray submenus mirror the three upstream concerns (Whisper / Kokoro /
LLM) with a status line and a Restart / Test Proxy action each.

## Pill overlay (`pill_window.py`)

Frameless, always-on-top, tool window (hidden from taskbar), translucent
background, does-not-accept-focus. Dragging persists `pill_x` /
`pill_y` to settings. Six states → six drawings in `paintEvent`:

| state | visual |
|---|---|
| idle | hidden |
| recording | red filled dot + "Listening" |
| processing | amber spinning arc + "Transcribing" |
| enhancing | blue spinning arc + "Enhancing" |
| typing | green filled dot + "Typing" |
| error | red dot + message (dwells 2 s then → idle) |

## Quit behaviour

- `orchestrator.quit()` called by tray Quit or SIGINT
- Stops pynput listener
- Submits `services.stop_all()` to the worker loop, waits 6 s
- Hides tray, calls `app.quit()`
- Starts a 5-second `threading.Timer` that calls `os._exit(0)` — any
  stuck thread, async hang, or non-daemon child is force-killed

## Setup script (`setup.ps1`)

Phases are all idempotent — re-running on a fully-installed machine
takes ~10 s.

1. **Prereqs**: Python 3.10+ via pyenv or system install, git, ffmpeg
   (warn-only), NVIDIA GPU (warn-only for CPU fallback)
2. **VoxType venv**: `voxtype-venv/` + `pip install -r
   voxtype/requirements.txt`
3. **Whisper venv**: `stt-venv/` + `pip install faster-whisper-server`
   + patch `api.py` tomllib lookup
4. **Kokoro venv** (unless `-SkipKokoro`): clone repo, `tts-venv/`,
   install PyTorch (CUDA or CPU), pip install `-e .`, download model
5. **Scheduled task** `VoxType-Dictation`: runs
   `voxtype-venv\Scripts\pythonw.exe -m voxtype` at logon, hidden,
   with `RestartCount=3`
6. **Seed settings**: `data/settings.json` created with chosen
   `whisper_model`

No Node, no npm, no Electron, no `lms server start`. Grep the script
to confirm.

## Uninstall (`uninstall.ps1`)

- Unregisters `VoxType-Dictation` (and legacy tasks)
- Kills orphan `faster-whisper-server` / `uvicorn` / `pythonw` / `python`
  processes under `$InstallDir`
- Interactively offers to delete:
  - `voxtype/data/` (repo-local user data)
  - Legacy `%USERPROFILE%\.voxtype` (pre-repo-local layout)
  - Legacy `%USERPROFILE%\.voicemode` (old voice-mode MCP data)
  - `$InstallDir` itself (~3 GB of venvs + Kokoro model)

## Testing

No formal test suite yet. Manual smoke:

```powershell
# stop the running task first if any
Stop-ScheduledTask -TaskName VoxType-Dictation

# run in foreground so stderr is visible
.\voxtype-venv\Scripts\python.exe -m voxtype
```

Expected:
- Tray icon appears
- `data/voxtype.log` starts filling
- Hotkey press produces a red pill; release produces a transcript
- With `proxy_url` live, "Test Proxy" returns `● reachable`
- With telecode up and `enhance_enabled`, filler words are cleaned in
  the final paste
