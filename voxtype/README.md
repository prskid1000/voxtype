# VoxType (Python port)

Pure-Python / PySide6 rewrite of the Electron/React original. Same UX:
press a hotkey, speak, release — cleaned text lands at your cursor in
any app. Drop-in replacement for the `voxtype/` Electron app.

## What changed vs the TS original

- **Electron → PySide6.** No Chromium, no Node, no native hotkey module.
  About 5× faster to launch, ~200 MB less disk.
- **LM Studio removed.** Every LM Studio call (model listing,
  auto-start, auto-unload, preload) is gone. Transcript cleanup now goes
  through **telecode's dual-protocol proxy** at
  `http://127.0.0.1:1235/v1/chat/completions`. Model selection is a
  string in settings — telecode handles the actual loading and routing.
- **Whisper + Kokoro stay as child processes.** The supervisor in
  `services.py` spawns `faster-whisper-server.exe` and Kokoro's uvicorn,
  probes `/health`, auto-restarts with exponential backoff, and reaps
  via `taskkill /T` on quit — same lifecycle as the TS version.

## Run

```powershell
pip install -r voxtype/requirements.txt
python -m voxtype
```

Data dir: `%USERPROFILE%\.voxtype\`
  - `settings.json` — auto-created on first run with defaults
  - `history.json`  — last 500 transcripts (if `save_history` is on)
  - `voxtype.log`   — current session
  - `voxtype.log.prev` — previous session (kept through one restart)

## Files

```
voxtype/
  main.py              # Orchestrator: Qt loop + asyncio-on-a-thread
  audio.py             # sounddevice → raw 16 kHz mono int16 PCM
  hotkey.py            # pynput.keyboard.Listener (hold / toggle / capture)
  stt.py               # multipart POST to faster-whisper-server
  llm.py               # telecode proxy — structured JSON output, LRU cache
  typer.py             # clipboard + Ctrl+V via PowerShell SendKeys
  vad.py               # numpy RMS on PCM
  screen_capture.py    # mss + PIL, red cursor marker
  history.py           # append-only JSON
  services.py          # supervisor for Whisper + Kokoro sidecars
  tray_menu.py         # QSystemTrayIcon + submenus
  pill_window.py       # frameless always-on-top status pill
  settings_window.py   # sidebar + cards (Dictation / Services / LLM / About)
  qt_theme.py          # dark QSS (lifted from telecode)
  types.py             # AppSettings, HotkeyCombo, PillState
  config.py            # atomic JSON I/O + hot-reload
  debug_log.py         # rotating file logger
  kokoro_voice.py      # voice catalog + warmup ping
  whisper_model.py     # model catalog
  resources/
    icon.png
    system-prompt.md   # LLM cleanup instructions
```

## Pointing at a non-telecode proxy

Anything that speaks OpenAI `/v1/chat/completions` works. Edit
`settings.json`:

```json
{
  "proxy_url":   "http://127.0.0.1:8080",
  "proxy_model": "your-model-id"
}
```

`screen_context: true` sends a JPEG of the display under the cursor —
only useful if the target model handles vision. Turn it off for
text-only LLMs.
