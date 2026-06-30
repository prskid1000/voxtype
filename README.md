# VoxType

Local voice dictation overlay for Windows, written in **pure Python +
PySide6**. Press a hotkey, speak, release — cleaned text appears at
your cursor in any app. No cloud, no telemetry, no account.

STT and TTS both run **in-process via PyTorch through a single
generic backend per modality.** Paste any HuggingFace repo id (or
local path) into the model field; the backend reads the model's
`config.json`, auto-detects which architectural family it belongs
to, picks the right loader, and shows you only the knobs that family
honours. One install covers virtually every open-source speech
model on HuggingFace.

An embedded aiohttp server exposes both engines on a single
OpenAI-compatible port (default `:6600`) so external clients can
call `/v1/audio/transcriptions` and `/v1/audio/speech`.

**Default models** (~3.3 GB total disk):
- **STT**: `openai/whisper-large-v3` — 99 languages, ~3 GB
- **TTS**: `hexgrad/Kokoro-82M` — 54 voices in 9 language families, ~327 MB

Sibling project of [telecode](https://github.com/prskid1000/telecode).
LLM transcript cleanup is routed through telecode's dual-protocol proxy
at `http://127.0.0.1:1235`.

---

## Supported model families

VoxType ships **one generic STT backend and one generic TTS backend**.
At load time, the backend sniffs the model's `config.json`
(`model_type` / `architectures` / `pipeline_tag`) and dispatches to
the right handler. The settings UI only shows the knobs the detected
family actually honours.

### STT families

| Family | HF `model_type` | Loader class | Knobs the UI exposes | Example repos |
|---|---|---|---|---|
| **Whisper** (default) | `whisper` | `WhisperForConditionalGeneration` | language · task (transcribe/translate) · beams · temperature · repetition penalty · initial-prompt · dtype · attention · torch.compile | `openai/whisper-{tiny,base,small,medium,large-v3}`, `openai/whisper-large-v3-turbo`, `distil-whisper/distil-large-v3` |
| **Wav2Vec2 / HuBERT / WavLM** | `wav2vec2`, `hubert`, `wavlm`, `unispeech`, `unispeech_sat` | `AutoModelForCTC` | dtype · attention · torch.compile | `facebook/wav2vec2-large-960h-lv60-self`, `facebook/hubert-large-ls960-ft` |
| **MMS** | `wav2vec2` (with adapter) | `Wav2Vec2ForCTC` + `load_adapter(<ISO-639-3>)` | language (1107 langs, auto-mapped to MMS adapter) · dtype · attention · torch.compile | `facebook/mms-1b-all`, `facebook/mms-1b-fl102` |
| **SeamlessM4T v1 / v2** | `seamless_m4t`, `seamless_m4t_v2` | `SeamlessM4Tv2ForSpeechToText` | language · task · beams · `tgt_lang` override · dtype · attention | `facebook/seamless-m4t-v2-large`, `facebook/hf-seamless-m4t-medium` |
| **Moonshine** | `moonshine` | `AutoModelForSpeechSeq2Seq` | beams · dtype · attention · torch.compile | `UsefulSensors/moonshine-{tiny,base}` |
| **Speech2Text** | `speech_to_text` | `Speech2TextForConditionalGeneration` | language · beams · dtype · attention | `facebook/s2t-small-librispeech-asr` |
| **SpeechT5 ASR** | `speecht5` + `ForSpeechToText` arch | `transformers.pipeline` fallback | dtype · attention | `microsoft/speecht5_asr` |
| **Voxtral** *(new)* | `voxtral` | `VoxtralForConditionalGeneration` (prompted ASR) | language · task · temperature · prompt · bf16 · attention | `mistralai/Voxtral-Mini-3B-2507`, `mistralai/Voxtral-Small-24B-2507` |
| **Granite-Speech** *(new)* | `granite_speech` | `GraniteSpeechForConditionalGeneration` (prompted ASR / AST) | language · task · prompt · bf16 · attention | `ibm-granite/granite-speech-3.3-{2b,8b}` |
| **Phi-4-Multimodal** *(new)* | `phi4_multimodal` | `Phi4MultimodalForCausalLM` (prompted) | prompt · temperature · bf16 · attention | `microsoft/Phi-4-multimodal-instruct` |
| **Qwen2-Audio** | `qwen2_audio` | `Qwen2AudioForConditionalGeneration` (prompted) | prompt · temperature · top_p · bf16 · attention | `Qwen/Qwen2-Audio-7B-Instruct` |
| **VibeVoice ASR** *(new)* | `vibevoice_*` | `transformers.pipeline` fallback | language · bf16 · attention | `microsoft/VibeVoice-*-ASR` |
| **Generic** (catch-all) | any with `pipeline_tag=automatic-speech-recognition` | `transformers.pipeline("automatic-speech-recognition")` | dtype · chunk-length · attention | anything else HF registers as ASR |

### TTS families

| Family | HF `model_type` | Loader | Voice catalog | Extra knobs | Example repos |
|---|---|---|---|---|---|
| **Kokoro** (default) | (custom) | `kokoro.KPipeline` | 54 voices, 9 langs (static) | speed · voice_blend · stream · attention · torch.compile | `hexgrad/Kokoro-82M` |
| **VITS / MMS-TTS** | `vits` | `VitsModel` + `AutoTokenizer` | one implicit voice per repo (~1107 langs total) | speed · noise_scale · noise_scale_duration · seed · attention | `facebook/mms-tts-{eng,spa,fra,hin,deu,cmn,…}` |
| **SpeechT5 TTS** | `speecht5` + `ForTextToSpeech` arch | `SpeechT5ForTextToSpeech` + HifiGAN | 4 default x-vectors + any `dataset:row` | speaker_embedding · speed · attention | `microsoft/speecht5_tts` |
| **Bark** | `bark` | `BarkModel` + `AutoProcessor` | 11 preset speakers (en/de/es/fr/hi/ja/zh) | semantic_temperature · coarse_temperature · min_eos_p · seed · attention | `suno/bark`, `suno/bark-small` |
| **Parler-TTS** | (custom) | `ParlerTTSForConditionalGeneration` | 5 style presets + free-text style | style · temperature · max_new_tokens · speed · attention | `parler-tts/parler-tts-{mini,large}-v1` |
| **XTTS / Coqui** | (custom) | Coqui `TTS` if installed | reference clip → cloned voice | reference_audio · language · temperature · top_p · top_k · repetition_penalty · length_penalty · speed | `coqui/XTTS-v2` |
| **Orpheus** *(new)* | (Llama backbone + SNAC vocoder) | `orpheus_tts.OrpheusModel` | 8 named speakers | temperature · top_p · emotion_tags · seed | `canopylabs/orpheus-3b-0.1-ft` |
| **CSM (Sesame)** *(new)* | `csm` | `CsmForConditionalGeneration` | conversational | temperature · seed · attention | `sesame/csm-1b` |
| **Higgs-Audio v2** *(new)* | (custom) | `AutoModelForCausalLM` (trust_remote_code) | zero-shot from reference clip | temperature · reference_audio · seed · attention | `bosonai/higgs-audio-v2-generation-3B-base` |
| **VibeVoice** *(new)* | `vibevoice_*` | `transformers.pipeline` (trust_remote_code) | multi-speaker | temperature · attention | `microsoft/VibeVoice-1.5B` |
| **Qwen3-TTS** | (custom) | `transformers.pipeline` fallback | model-defined | temperature · top_p · speed | community Qwen-TTS mirrors |
| **Generic** | any with `pipeline_tag=text-to-speech` | `transformers.pipeline("text-to-speech")` | one default | torch.compile · attention | anything else HF registers as TTS |

### Universal settings (every family honours these)

| Setting | Type | What it does |
|---|---|---|
| **Device** | enum | `cpu` / `cuda`. Falls back to CPU when CUDA unavailable. |
| **Precision** | enum | `auto` / `fp16` / `bf16` / `fp32`. `auto` = fp16 on GPU, fp32 on CPU. |
| **Attention** | enum | `auto` / `sdpa` / `flash_attention_2` / `eager`. Pick `flash_attention_2` for ~1.5–2× speedup on Ampere+ with fp16/bf16 (install via `setup.ps1 -FlashAttn $true`). |
| **Language** | enum | Decoder hint for multilingual STT families. Hidden for English-only families. |
| **Idle Unload** | int | Seconds of idleness before the engine frees GPU memory. `0` = never. |
| **torch.compile** | bool | JIT compile the model (~20–40% steady-state speedup, ~30 s first call). |
| **Warm Up On Load** | bool | Run a dummy inference at load time so the first real call is fast. |
| **TTS Speed** | float | 0.5–2.0× synthesis rate. |
| **TTS Seed** | int | RNG seed for sampling families (VITS / Bark / Parler / Orpheus / Higgs). `-1` = random. |

The model field accepts **any HuggingFace repo id or local model
path**. Type it; the family pill next to the field updates instantly
from the repo id (no network). Click **Detect** to verify against the
HF API. Click **Load** to actually pull weights and run inference.

---

## Quick start

```powershell
git clone https://github.com/prskid1000/voxtype.git "$env:USERPROFILE\.voxtype"
cd "$env:USERPROFILE\.voxtype"
.\setup.ps1
```

`setup.ps1` will:

1. Verify **Python 3.10+**, **git**, **ffmpeg** (optional), GPU support
2. Create `voxtype-venv/` and install:
   - `torch` (CUDA 13 nightly wheel if `-GpuSupport`, CPU wheel otherwise)
   - `transformers`, `accelerate`, `sentencepiece`, `datasets` (covers
     every HF family in the tables above; `accelerate` enables
     direct-to-GPU weight loading for faster cold starts)
   - `kokoro` (the one TTS family that uses a non-HF loader)
   - PySide6 / pynput / sounddevice / soundfile / aiohttp / numpy /
     pywin32 / Pillow / mss
3. Pre-download the default STT + TTS models into the HuggingFace
   cache. Re-runs cost nothing.
4. Register a scheduled task `VoxType` that launches
   `pythonw.exe -m voxtype` at logon (no console window)
5. Seed `voxtype/data/settings.json` with defaults
6. Start VoxType immediately

Optional flags:

| Flag | Default | What it does |
|---|---|---|
| `-InstallDir <path>` | `~/.voxtype` | Where the venv + scheduled task land. |
| `-GpuSupport $true\|$false` | `$true` | Install the CUDA wheel of torch (vs. CPU-only). |
| `-CudaVersion cu130\|cu124\|cpu` | `cu130` | Which torch wheel index. `cu130` = nightly, `cu124` = stable. |
| `-FlashAttn $true\|$false` | `$false` | Search community Windows-wheel repos ([mjun0812](https://github.com/mjun0812/flash-attention-prebuild-wheels), [GarfieldHuang](https://github.com/GarfieldHuang/flash-attention-windows-wheel), [jono0301](https://github.com/jono0301/flash-attention-windows-wheels)) for a Flash-Attention 2 wheel matching your torch + CUDA + Python and install it. Unlocks `Attention → flash_attention_2` in Settings (~1.5–2× faster Whisper / Voxtral / Seamless on Ampere+). Off by default because wheel coverage is narrow on `cu130` nightly torch — switch to `-CudaVersion cu124` for the widest match, or leave Attention on `auto` (sdpa is still fast). |

Re-running `setup.ps1` is idempotent at every phase.

Look for the tray icon (bottom-right). Press **Ctrl+Win**, speak, release.

### Setup options

```powershell
.\setup.ps1                              # full install (CUDA 13 nightly torch)
.\setup.ps1 -CudaVersion cu124           # CUDA 12.4 stable torch
.\setup.ps1 -GpuSupport $false           # CPU-only torch
.\setup.ps1 -InstallDir "D:\voxtype"     # custom location
```

### Optional extras

| If you want… | Install |
|---|---|
| Parler-TTS (style-prompt synthesis) | `pip install parler-tts` |
| espeak-ng phonemizer fallback (some VITS langs, Bark) | `winget install eSpeak-NG.eSpeak-NG` |
| Non-WAV uploads to the HTTP server (mp3/ogg/m4a) | `winget install ffmpeg` |

When an optional dep is missing, the generic backend falls back to
`transformers.pipeline("text-to-speech")` so the UI still works — you
just lose family-specific knobs (e.g. Parler's style prompt).

---

## Picking a model

The model picker is now **just a text field + Browse + Detect + family
status pill**. There's no backend dropdown — there's only one backend
(the generic one) and it figures out the family automatically.

Recommended starting points (from `voxtype/resources/models.json`):

**STT (English-only / fastest):**
- `UsefulSensors/moonshine-tiny` — ~250 MB, real-time on CPU
- `facebook/wav2vec2-large-960h-lv60-self` — pure CTC, no language hint needed
- `distil-whisper/distil-medium.en` — ~750 MB

**STT (multilingual):**
- `openai/whisper-large-v3-turbo` — ~1.6 GB, multilingual, beam search
- `facebook/seamless-m4t-v2-large` — 100+ langs + translation
- `facebook/mms-1b-all` — 1107 languages (set Language to the target)

**TTS (English):**
- `hexgrad/Kokoro-82M` (default) — 54 voices, native streaming
- `suno/bark-small` — generative, 11 preset speakers
- `parler-tts/parler-tts-mini-v1` — style-prompt controllable

**TTS (multilingual / minority languages):**
- `facebook/mms-tts-<iso3>` — pick a language-specific repo
  (`mms-tts-eng`, `mms-tts-hin`, `mms-tts-cmn`, …)

---

## Prerequisites

| Dependency | Required for | Where to get it |
|---|---|---|
| **Windows 10/11** | Target OS | — |
| **Python 3.10–3.12** | Everything (kokoro pins <3.13) | https://python.org |
| **git** | Cloning the repo | https://git-scm.com |
| **ffmpeg** (optional) | Non-WAV audio uploads to the embedded server | `winget install ffmpeg` |
| **NVIDIA GPU + recent driver** | Optional — falls back to CPU. torch ships its own CUDA runtime. | https://nvidia.com/drivers |
| **espeak-ng** (recommended for non-English TTS) | Phonemizer fallback for some VITS / Bark voices | `winget install eSpeak-NG.eSpeak-NG` |
| **telecode** (optional) | LLM transcript cleanup | https://github.com/prskid1000/telecode |

Without telecode running, dictation still works — you just get raw
STT transcripts (no filler-word cleanup, no punctuation fixes).

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
    → stt.transcribe() — DIRECT call into stt_engine.STTEngine
                         (no HTTP — that's only for external clients)

if enhance_enabled:
    → pill = enhancing
    → if screen_context: capture active display + paint red cursor
      marker → JPEG base64
    → llm.enhance() — OpenAI-shape POST to telecode proxy (:1235)
    → LRU cache (50 entries) keyed on (transcript, screenshot fingerprint)

→ pill = typing
→ typer.type_text() — clipboard + Ctrl+V via PowerShell SendKeys
→ history.add() — append to data/history.json (last 500)
→ pill = idle
```

### Embedded HTTP server

Lives in `voxtype/server.py`, starts on port 6600 (configurable). Routes:

```
POST /v1/audio/transcriptions  →  STT (multipart upload)
POST /v1/audio/speech          →  TTS (JSON in, WAV out)
GET  /v1/models                →  engine list
GET  /health                   →  engine readiness snapshot
```

The `model` field is accepted but ignored (VoxType controls the
loaded model). The `voice` field on `/v1/audio/speech` IS honoured if
it matches the loaded backend's voice catalog — otherwise the
configured `tts_voice` default is used.

---

## Tray menu

```
⬡/⬢ STT     ▸ status + family + Load / Unload / Reload
⬡/⬢ TTS     ▸ status + family + Load / Unload / Reload
⬡/⬢ LLM     ▸ proxy model + Test Proxy Connection
⬢   Pill    ▸ Hide Pill / Show Pill + Reset Position
◳   OLED Guard ▸ Enable + 1 / 2 / 4 / 6 flashes per second
─
Open Settings Window   (default left-click)
─
Quit VoxType
```

Settings sections:

- **Dictation** — hotkey mode, live **Rebind** button, auto-stop on
  silence, VAD, append mode, save history, **Voice Activation** (start
  words — see below), **Recording Sounds**
  (enable + start/stop/done cues — bundled 1-second WAVs ship in
  `voxtype/resources/sounds/`; Browse to override with any
  wav/flac/ogg/mp3)
- **Services** — three cards:
  - **OpenAI HTTP Server** — enable + port for the embedded server
  - **STT** — model field with auto-family detection, language,
    device, dtype, warmup, torch.compile, plus an **Advanced
    (per-family)** section that shows/hides knobs based on the
    detected family (Task and Initial Prompt for Whisper, none for
    Wav2Vec2, …).
  - **TTS** — model field with auto-family detection, voice picker
    (populated from the detected family's static catalog), speed,
    stream, plus per-family Advanced (Style for Parler, Speaker
    Embedding for SpeechT5, Temperature for Bark, …).
- **LLM** — enhance on/off, screen context, proxy URL + model, Test
  Proxy Connection
- **Display** — **OLED Burn-In Guard**: flash a fullscreen black frame a
  few times per second to give OLED pixels a brief rest. Off by default.
  You set one knob — black flashes per second (1 / 2 / 4 / 6) — and the
  per-flash duration is derived from the auto-detected refresh rate (one
  display frame, kept honest with a 1 ms multimedia-timer resolution so
  the flash isn't stretched by Windows' coarse default tick). A **Flash
  Darkness** slider trades visibility for protection: 100% = full black
  (pixels fully off), lower = a gentle translucent dim — try ~40-60% if a
  full black flash is distracting. The frame is click-through and never
  steals focus. Primary display only. Mild, panel-dependent flicker — not
  a guaranteed burn-in cure.
- **History** — saved transcripts with 📋 Raw / 📋 Final copy icons
- **Logs** — live-tailing `voxtype.log` / `voxtype.log.prev`

Every toggle writes through to `data/settings.json` atomically.
Per-family options live in the free-form `stt_opts` / `tts_opts`
dicts inside `settings.json`, so adding a new family option never
requires touching the AppSettings dataclass.

---

## Settings shape

Top-level fields are universal across all families. Family-specific
knobs go in the opts bags.

```python
@dataclass
class AppSettings:
    # ── Universal STT (every family honours these) ──────────────────
    stt_model_path:    str = "openai/whisper-large-v3"
    stt_device:        str = "cpu"           # cpu | cuda
    stt_language:      str = "en"            # multilingual families
    stt_dtype:         str = "auto"          # auto|fp32|fp16|bf16
    stt_warmup:        bool = True
    stt_torch_compile: bool = False
    stt_idle_unload_sec: int = 300

    # Per-family opts (rendered dynamically). Examples:
    #   {"task": "translate", "num_beams": 5}     for Whisper / Seamless
    #   {}                                         for Wav2Vec2 / MMS
    stt_opts: dict = field(default_factory=dict)

    # ── Universal TTS ──────────────────────────────────────────────
    tts_model_path:    str = "hexgrad/Kokoro-82M"
    tts_device:        str = "cpu"
    tts_voice:         str = "af_heart"
    tts_speed:         float = 1.0
    tts_warmup:        bool = True
    tts_torch_compile: bool = False
    tts_stream:        bool = False

    # Per-family opts. Examples:
    #   {"style": "A calm female voice"}          for Parler
    #   {"speaker_embedding": "Matthijs/cmu-arctic-xvectors:7306"}  for SpeechT5
    #   {"temperature": 0.7}                       for Bark
    tts_opts: dict = field(default_factory=dict)

    # ── OLED burn-in guard ─────────────────────────────────────────
    oled_guard_enabled:   bool = False
    oled_flashes_per_sec: int = 2            # 1 | 2 | 4 | 6
    oled_flash_opacity:   float = 1.0        # 1.0 = full black, lower = dim
```

Old settings files from before the schema change auto-migrate on
load (`stt_task` → `stt_opts.task`, `tts_speaker` → `tts_voice`,
etc.). No manual editing required.

---

## Data layout

```
voxtype/data/
  settings.json      # AppSettings — auto-created on first run
  history.json       # last 500 transcripts (if save_history=true)
  voxtype.log        # current session
  voxtype.log.prev   # previous session (rotated on restart)
```

Override with `VOXTYPE_DATA_DIR=C:\some\other\path` if you want
storage outside the repo. `voxtype/data/` is gitignored.

---

## LLM enhancement

```json
{
  "enhance_enabled": true,
  "screen_context":  true,
  "proxy_url":       "http://127.0.0.1:1235",
  "proxy_model":     "qwen3.5-35b"
}
```

If the request fails, the **original STT transcript** is returned
unchanged — dictation keeps working when the LLM is unreachable.

---

## Hotkey

Defaults to **Ctrl + Win** (hold). Use the **Rebind** button in
Settings → Dictation to capture a new combo.

`hotkey_mode` can be `"hold"` (dictate while held) or `"toggle"`
(tap to start, tap to stop).

---

## Voice activation ("start words")

Hands-free dictation: instead of pressing the hotkey, say a **start
word** and VoxType begins recording. Off by default — enable it in
**Settings → Dictation → Voice Activation**. The hotkey keeps working
alongside it.

How it works: while enabled, VoxType keeps the mic open and the STT
model warm, segments your speech into short utterances, transcribes
each, and starts a dictation when an utterance matches one of your
configured **Start Words** (comma-separated, e.g. `computer, hey vox`).
Voice-triggered captures always **auto-stop on silence** (there's no
hotkey to release). Say the start word, pause briefly, then dictate.

| Setting | Default | Meaning |
|---------|---------|---------|
| `voice_activation_enabled` | `false` | Master on/off for the start-word listener. |
| `voice_start_words` | `"computer"` | Comma-separated trigger phrases. |
| `voice_match_contains` | `false` | `false` = utterance must START with a start word; `true` = match anywhere in the utterance. |
| `voice_max_phrase_sec` | `2.5` | Utterances longer than this are ignored when scanning (wake phrases are short). |

Trade-off: because detection reuses your STT model, the mic and model
stay active while listening (the GPU context is freed again once you
disable voice activation and STT idle-unloads). Every nearby short
utterance gets transcribed to check for the start word, so pick a
distinctive phrase to keep false triggers low.

---

## Testing

Stdlib unittest, no extra deps:

```powershell
.\voxtype-venv\Scripts\python.exe -m unittest discover tests
```

The test suite covers family detection (15+ STT/TTS repo-id cases),
settings migration, `config.patch` dotted writes, registry
resolution, alias catalog integrity, engine option filtering, and
the per-family option-spec contents.

---

## Uninstall

```powershell
.\uninstall.ps1
```

---

## Known limitations

- **Windows-only.** Typer uses PowerShell SendKeys; screen capture
  uses Win32 `GetCursorPos`.
- **No live mic device picker.** sounddevice picks the system default.
- **TTS isn't wired into the dictation pipeline** — it's served via
  the HTTP endpoint for external clients. Speak-back is not part of
  the hotkey flow.
- **CUDA 13 torch wheels are nightly.** Use `-CudaVersion cu124`
  for the stable channel.
- **Optional family deps are not auto-installed.** `parler-tts` is
  the main one — install manually if you want Parler's style-prompt
  knob; otherwise it falls through to the pipeline fallback.
