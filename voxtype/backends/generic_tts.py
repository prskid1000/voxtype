"""Generic TTS backend — one backend, many model families.

Paste any HuggingFace repo id (or local path); this backend sniffs the
model's metadata, picks the right loader class, and exposes the right
voice list + per-family runtime options. Families covered:

  kokoro       → kokoro.KPipeline  (54 curated voices, 9 langs, streaming)
  vits         → VitsModel + AutoTokenizer  (incl. MMS-TTS, ~1107 langs)
  speecht5_tts → SpeechT5ForTextToSpeech  + speaker x-vector
  bark         → BarkModel + BarkProcessor  (preset voice list)
  parler       → ParlerTTSForConditionalGeneration  (style-prompt)
  xtts         → Coqui TTS (voice cloning) — only if `TTS` is installed
  generic_tts  → transformers.pipeline("text-to-speech")  — fallback

A handler whose optional dep is missing gracefully degrades to the
pipeline fallback so the UI still works.
"""
from __future__ import annotations

import gc
import io
import logging
import wave
from typing import Any, Iterator

import numpy as np

from voxtype.backends import family_detect as fd
from voxtype.backends.tts_base import (
    OptionSpec, TTSBackend, TTSLoadConfig, VoiceEntry,
)

log = logging.getLogger("voxtype.backends.generic_tts")


# ── Per-family handlers ──────────────────────────────────────────────


class _BaseTTSHandler:
    family: str = ""
    sample_rate: int = 24000

    def __init__(self) -> None:
        self._model: Any = None
        self._processor: Any = None
        self._torch_device: str = "cpu"
        self._attn_impl: str = "auto"

    def _resolve_device(self, cfg: TTSLoadConfig) -> bool:
        import torch
        on_cuda = cfg.device == "cuda" and torch.cuda.is_available()
        if cfg.device == "cuda" and not on_cuda:
            log.warning("%s: cuda requested but unavailable — using CPU", self.family)
        self._torch_device = "cuda" if on_cuda else "cpu"
        self._attn_impl = (cfg.attn_impl or "auto").lower()
        return on_cuda

    def _attn_kwargs(self) -> dict[str, Any]:
        if self._attn_impl in {"sdpa", "flash_attention_2", "eager"}:
            return {"attn_implementation": self._attn_impl}
        return {}

    @staticmethod
    def _local_first(loader, *args, **kwargs):
        """Load from the HF cache with no network first — skips the
        per-load ETag/HEAD request for an already-cached model. Falls
        back to an online load (may download) if not cached yet."""
        try:
            return loader(*args, local_files_only=True, **kwargs)
        except Exception:
            return loader(*args, **kwargs)

    def _load_model(self, loader, model_id, **extra):
        """Load an HF model straight onto the target device via accelerate
        `device_map` + `low_cpu_mem_usage`, skipping the CPU->GPU copy.
        Falls back to the plain load + `.to()` if device_map is
        unsupported for the arch."""
        kw = dict(self._attn_kwargs())
        kw.update(extra)
        kw["low_cpu_mem_usage"] = True
        if self._torch_device == "cuda":
            try:
                return self._local_first(
                    loader, model_id, device_map="cuda", **kw)
            except Exception as exc:
                log.warning("%s: device_map load failed (%s); plain load",
                            self.family, exc)
        return self._local_first(loader, model_id, **kw).to(self._torch_device)

    def _seed_rng(self, opts: dict[str, Any]) -> None:
        """Apply the per-call `seed` opt (or universal seed via engine).
        Called at the top of each synth() before sampling."""
        seed = opts.get("seed")
        if seed is None:
            return
        try:
            s = int(seed)
        except Exception:
            return
        if s < 0:
            return
        try:
            import torch
            torch.manual_seed(s)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(s)
        except Exception:
            pass

    def load(self, cfg: TTSLoadConfig) -> None:  # pragma: no cover
        raise NotImplementedError

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        raise NotImplementedError

    def voices(self) -> list[VoiceEntry]:
        return []

    def default_voice(self) -> str:
        return ""

    def unload(self) -> None:
        self._model = None
        self._processor = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        gc.collect()

    @staticmethod
    def _to_int16_bytes(arr: np.ndarray) -> bytes:
        arr = np.asarray(arr, dtype=np.float32).reshape(-1)
        np.clip(arr, -1.0, 1.0, out=arr)
        return (arr * 32767.0).astype(np.int16).tobytes()


# ── Kokoro (uses the kokoro PyPI lib) ────────────────────────────────

_KOKORO_VOICES: dict[str, list[tuple[str, str, str]]] = {
    "American English": [
        ("af_alloy","F","Alloy"),("af_aoede","F","Aoede"),
        ("af_bella","F","Bella"),("af_heart","F","Heart"),
        ("af_jessica","F","Jessica"),("af_kore","F","Kore"),
        ("af_nicole","F","Nicole"),("af_nova","F","Nova"),
        ("af_river","F","River"),("af_sarah","F","Sarah"),
        ("af_sky","F","Sky"),
        ("am_adam","M","Adam"),("am_echo","M","Echo"),
        ("am_eric","M","Eric"),("am_fenrir","M","Fenrir"),
        ("am_liam","M","Liam"),("am_michael","M","Michael"),
        ("am_onyx","M","Onyx"),("am_puck","M","Puck"),
        ("am_santa","M","Santa"),
    ],
    "British English": [
        ("bf_alice","F","Alice"),("bf_emma","F","Emma"),
        ("bf_isabella","F","Isabella"),("bf_lily","F","Lily"),
        ("bm_daniel","M","Daniel"),("bm_fable","M","Fable"),
        ("bm_george","M","George"),("bm_lewis","M","Lewis"),
    ],
    "Spanish":   [("ef_dora","F","Dora"),("em_alex","M","Alex"),("em_santa","M","Santa")],
    "French":    [("ff_siwis","F","Siwis")],
    "Hindi":     [("hf_alpha","F","Alpha"),("hf_beta","F","Beta"),
                   ("hm_omega","M","Omega"),("hm_psi","M","Psi")],
    "Italian":   [("if_sara","F","Sara"),("im_nicola","M","Nicola")],
    "Japanese":  [("jf_alpha","F","Alpha"),("jf_gongitsune","F","Gongitsune"),
                   ("jf_nezumi","F","Nezumi"),("jf_tebukuro","F","Tebukuro"),
                   ("jm_kumo","M","Kumo")],
    "Brazilian Portuguese": [("pf_dora","F","Dora"),("pm_alex","M","Alex"),("pm_santa","M","Santa")],
    "Mandarin Chinese": [
        ("zf_xiaobei","F","Xiaobei"),("zf_xiaoni","F","Xiaoni"),
        ("zf_xiaoxiao","F","Xiaoxiao"),("zf_xiaoyi","F","Xiaoyi"),
        ("zm_yunjian","M","Yunjian"),("zm_yunxi","M","Yunxi"),
        ("zm_yunxia","M","Yunxia"),("zm_yunyang","M","Yunyang"),
    ],
}


class _KokoroHandler(_BaseTTSHandler):
    family = fd.TTS_KOKORO
    sample_rate = 24000

    def load(self, cfg: TTSLoadConfig) -> None:
        import torch
        from kokoro import KPipeline
        on_cuda = self._resolve_device(cfg)
        self._model = KPipeline(
            lang_code="a", repo_id=cfg.model_id, device=self._torch_device,
        )
        if cfg.torch_compile:
            try:
                inner = getattr(self._model, "model", None)
                if inner is not None:
                    self._model.model = torch.compile(inner, mode="reduce-overhead")
            except Exception as exc:
                log.warning("kokoro: torch.compile failed (%s)", exc)
        if cfg.warmup:
            try:
                for _ in self.synth("Voxtype ready.", "af_heart", {}):
                    pass
            except Exception as exc:
                log.warning("kokoro warmup failed: %s", exc)

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        import torch
        # Voice blend overrides the picked voice when provided.
        blend = str(opts.get("voice_blend") or "").strip()
        v: Any = blend if blend else (voice or "af_heart")
        speed = float(opts.get("speed") or 1.0)
        try:
            iterator = self._model(text, voice=v, speed=speed)
        except (TypeError, ValueError) as exc:
            log.warning("kokoro: voice spec %r rejected (%s); falling "
                        "back to default", v, exc)
            iterator = self._model(text, voice="af_heart", speed=speed)
        for _, _, audio in iterator:
            if audio is None:
                continue
            if isinstance(audio, torch.Tensor):
                arr = audio.detach().cpu().to(torch.float32).numpy()
            else:
                arr = np.asarray(audio, dtype=np.float32)
            yield self._to_int16_bytes(arr)

    def voices(self) -> list[VoiceEntry]:
        out: list[VoiceEntry] = []
        for lang, items in _KOKORO_VOICES.items():
            for vid, gender, name in items:
                out.append(VoiceEntry(vid, lang, gender, name))
        return out

    def default_voice(self) -> str:
        return "af_heart"


# ── VITS / MMS-TTS ───────────────────────────────────────────────────


class _VitsHandler(_BaseTTSHandler):
    family = fd.TTS_VITS
    sample_rate = 16000   # MMS-TTS is 16k; pure VITS varies — we read it post-load

    def load(self, cfg: TTSLoadConfig) -> None:
        from transformers import VitsModel, AutoTokenizer
        self._resolve_device(cfg)
        self._processor = self._local_first(AutoTokenizer.from_pretrained, cfg.model_id)
        self._model = self._load_model(VitsModel.from_pretrained, cfg.model_id)
        self._model.eval()
        # VITS exposes the SR via model.config.sampling_rate
        try:
            self.sample_rate = int(self._model.config.sampling_rate)
        except Exception:
            pass
        if cfg.warmup:
            try:
                for _ in self.synth("Hello.", "default", {}):
                    pass
            except Exception as exc:
                log.warning("vits warmup failed: %s", exc)

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        import torch
        self._seed_rng(opts)
        # Speed in VITS is the inverse of `speaking_rate`. Universal
        # `speed` is forwarded by the engine; we map it onto the model.
        speed = float(opts.get("speed") or 1.0)
        try:
            self._model.speaking_rate = speed
        except Exception:
            pass
        noise = opts.get("noise_scale")
        if noise is not None:
            try:
                self._model.noise_scale = float(noise)
            except Exception:
                pass
        ndur = opts.get("noise_scale_duration")
        if ndur is not None:
            try:
                self._model.noise_scale_duration = float(ndur)
            except Exception:
                pass
        inputs = self._processor(text=text, return_tensors="pt").to(self._torch_device)
        with torch.no_grad():
            out = self._model(**inputs).waveform
        arr = out[0].detach().cpu().to(torch.float32).numpy()
        yield self._to_int16_bytes(arr)

    def voices(self) -> list[VoiceEntry]:
        # Most VITS / MMS-TTS repos are single-speaker per language.
        # The "voice" is implicit in the model id, so we return one
        # entry labelled "default" and let users switch by changing the
        # model field.
        return [VoiceEntry("default", "model voice", "", "default")]

    def default_voice(self) -> str:
        return "default"


# ── SpeechT5 TTS ─────────────────────────────────────────────────────


class _SpeechT5Handler(_BaseTTSHandler):
    family = fd.TTS_SPEECHT5
    sample_rate = 16000

    def __init__(self) -> None:
        super().__init__()
        self._vocoder: Any = None
        self._embeddings: dict[str, Any] = {}   # cache

    def load(self, cfg: TTSLoadConfig) -> None:
        from transformers import (
            SpeechT5ForTextToSpeech, SpeechT5Processor, SpeechT5HifiGan,
        )
        self._resolve_device(cfg)
        self._processor = self._local_first(SpeechT5Processor.from_pretrained, cfg.model_id)
        self._model = self._load_model(
            SpeechT5ForTextToSpeech.from_pretrained, cfg.model_id)
        self._vocoder = self._load_model(
            SpeechT5HifiGan.from_pretrained, "microsoft/speecht5_hifigan")
        self._model.eval(); self._vocoder.eval()

    def _resolve_embedding(self, spec: str):
        """spec format: 'dataset_id:row_index' (e.g.
        'Matthijs/cmu-arctic-xvectors:7306'). Cached after first load."""
        import torch
        if spec in self._embeddings:
            return self._embeddings[spec]
        if ":" in spec:
            ds, idx = spec.rsplit(":", 1)
            try:
                from datasets import load_dataset
                emb_ds = load_dataset(ds, split="validation")
                v = torch.tensor(emb_ds[int(idx)]["xvector"]).unsqueeze(0)
                v = v.to(self._torch_device)
                self._embeddings[spec] = v
                return v
            except Exception as exc:
                log.warning("speecht5: embedding %r unavailable (%s); using zeros",
                            spec, exc)
        v = torch.zeros((1, 512), device=self._torch_device)
        self._embeddings[spec] = v
        return v

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        import torch
        emb_spec = str(opts.get("speaker_embedding")
                        or voice
                        or "Matthijs/cmu-arctic-xvectors:7306")
        speaker = self._resolve_embedding(emb_spec)
        inputs = self._processor(text=text, return_tensors="pt").to(self._torch_device)
        with torch.no_grad():
            speech = self._model.generate_speech(
                inputs["input_ids"], speaker, vocoder=self._vocoder,
            )
        arr = speech.detach().cpu().to(torch.float32).numpy()
        yield self._to_int16_bytes(arr)

    def voices(self) -> list[VoiceEntry]:
        # Built-in popular x-vector rows from cmu-arctic-xvectors.
        # Users can pass any dataset:row via the speaker_embedding opt.
        ds = "Matthijs/cmu-arctic-xvectors"
        return [
            VoiceEntry(f"{ds}:7306", "English", "F", "AWB-Female-7306"),
            VoiceEntry(f"{ds}:2271", "English", "M", "SLT-Male-2271"),
            VoiceEntry(f"{ds}:1726", "English", "M", "RMS-Male-1726"),
            VoiceEntry(f"{ds}:3457", "English", "F", "CLB-Female-3457"),
        ]

    def default_voice(self) -> str:
        return "Matthijs/cmu-arctic-xvectors:7306"

    def unload(self) -> None:
        self._vocoder = None
        self._embeddings.clear()
        super().unload()


# ── Bark ─────────────────────────────────────────────────────────────


_BARK_VOICES = [
    ("v2/en_speaker_0", "English", "M", "Speaker 0"),
    ("v2/en_speaker_1", "English", "M", "Speaker 1"),
    ("v2/en_speaker_3", "English", "M", "Speaker 3"),
    ("v2/en_speaker_6", "English", "F", "Speaker 6"),
    ("v2/en_speaker_9", "English", "F", "Speaker 9"),
    ("v2/de_speaker_3", "German",  "M", "Speaker 3"),
    ("v2/es_speaker_0", "Spanish", "M", "Speaker 0"),
    ("v2/fr_speaker_5", "French",  "F", "Speaker 5"),
    ("v2/hi_speaker_2", "Hindi",   "F", "Speaker 2"),
    ("v2/ja_speaker_0", "Japanese","F", "Speaker 0"),
    ("v2/zh_speaker_4", "Chinese", "F", "Speaker 4"),
]


class _BarkHandler(_BaseTTSHandler):
    family = fd.TTS_BARK
    sample_rate = 24000

    def load(self, cfg: TTSLoadConfig) -> None:
        from transformers import BarkModel, AutoProcessor
        self._resolve_device(cfg)
        self._processor = self._local_first(AutoProcessor.from_pretrained, cfg.model_id)
        self._model = self._load_model(BarkModel.from_pretrained, cfg.model_id)
        self._model.eval()
        try:
            self.sample_rate = int(self._model.generation_config.sample_rate)
        except Exception:
            pass

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        import torch
        self._seed_rng(opts)
        v = voice or "v2/en_speaker_6"
        inputs = self._processor(text, voice_preset=v).to(self._torch_device)
        gen: dict = {"do_sample": True}
        # Bark exposes three temperatures (semantic / coarse / fine).
        # The HF wrapper takes them as separate generate() kwargs.
        sem = opts.get("semantic_temperature")
        if sem is not None:
            try:
                gen["semantic_temperature"] = float(sem)
            except Exception:
                pass
        crs = opts.get("coarse_temperature")
        if crs is not None:
            try:
                gen["coarse_temperature"] = float(crs)
            except Exception:
                pass
        eos = opts.get("min_eos_p")
        if eos is not None:
            try:
                gen["min_eos_p"] = float(eos)
            except Exception:
                pass
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen)
        arr = out.detach().cpu().to(torch.float32).numpy().reshape(-1)
        yield self._to_int16_bytes(arr)

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry(vid, lang, g, name)
                for vid, lang, g, name in _BARK_VOICES]

    def default_voice(self) -> str:
        return "v2/en_speaker_6"


# ── Parler-TTS ───────────────────────────────────────────────────────


_PARLER_PRESETS = [
    ("calm-female", "A calm, warm female voice with a slow, deliberate pace."),
    ("calm-male",   "A calm, low-pitched male voice speaking at a moderate pace."),
    ("excited",     "An expressive, excited voice with high pitch and animated delivery."),
    ("news",        "A clear, authoritative voice in a news-anchor style."),
    ("narrator",    "A warm, engaging audiobook narrator with steady pacing."),
]


class _ParlerHandler(_BaseTTSHandler):
    family = fd.TTS_PARLER
    sample_rate = 44100

    def load(self, cfg: TTSLoadConfig) -> None:
        # parler-tts is a separate PyPI package. If missing, raise; the
        # dispatcher will retry with the pipeline fallback.
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer
        self._resolve_device(cfg)
        self._tokenizer = self._local_first(AutoTokenizer.from_pretrained, cfg.model_id)
        self._model = self._load_model(
            ParlerTTSForConditionalGeneration.from_pretrained, cfg.model_id)
        self._model.eval()
        try:
            self.sample_rate = int(self._model.config.sampling_rate)
        except Exception:
            pass

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        import torch
        self._seed_rng(opts)
        # Parler conditions on a *style description*. Voice id maps to
        # one of our presets; the per-call `style` opt overrides it.
        # The model receives it as `input_ids` (style) + `prompt_input_ids`
        # (text). Parler's own code names this argument `description`,
        # but the underlying generate() call uses `input_ids`.
        style = str(opts.get("style") or "")
        if not style:
            style = dict(_PARLER_PRESETS).get(voice or "calm-male",
                                               "A clear, neutral voice.")
        inputs = self._tokenizer(style, return_tensors="pt").to(self._torch_device)
        prompts = self._tokenizer(text, return_tensors="pt").to(self._torch_device)
        gen: dict[str, Any] = {
            "input_ids": inputs.input_ids,
            "prompt_input_ids": prompts.input_ids,
        }
        temp = opts.get("temperature")
        if temp is not None:
            try:
                t = float(temp)
                if t > 0.0:
                    gen["do_sample"] = True
                    gen["temperature"] = t
            except Exception:
                pass
        mnt = opts.get("max_new_tokens")
        if mnt is not None:
            try:
                gen["max_new_tokens"] = int(mnt)
            except Exception:
                pass
        with torch.no_grad():
            out = self._model.generate(**gen)
        arr = out.cpu().to(torch.float32).numpy().squeeze()
        yield self._to_int16_bytes(arr)

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry(k, "English", "", k.replace("-", " ").title())
                for k, _ in _PARLER_PRESETS]

    def default_voice(self) -> str:
        return "calm-male"


# ── XTTS (Coqui) — voice cloning ─────────────────────────────────────


class _XTTSHandler(_BaseTTSHandler):
    family = fd.TTS_XTTS
    sample_rate = 24000

    def load(self, cfg: TTSLoadConfig) -> None:
        # Coqui's `TTS` PyPI package. Non-commercial license. If missing,
        # the dispatcher retries with the pipeline fallback (which will
        # fail too — XTTS isn't HF-pipeline-compatible).
        from TTS.api import TTS as _CoquiTTS
        self._resolve_device(cfg)
        self._model = _CoquiTTS(model_name=cfg.model_id, progress_bar=False)
        if self._torch_device == "cuda":
            try:
                self._model = self._model.to("cuda")
            except Exception:
                pass
        try:
            self.sample_rate = int(
                self._model.synthesizer.output_sample_rate)
        except Exception:
            pass

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        ref = str(opts.get("reference_audio") or "").strip()
        if not ref:
            raise RuntimeError(
                "XTTS requires a reference audio path "
                "(set tts_opts.reference_audio)")
        lang = str(opts.get("language") or "en")
        speed = float(opts.get("speed") or 1.0)
        kw: dict[str, Any] = {
            "text": text, "speaker_wav": ref, "language": lang,
            "speed": speed,
        }
        # XTTS exposes these on `tts()` as keyword args in Coqui ≥ 0.22.
        for key in ("temperature", "top_p", "top_k",
                     "repetition_penalty", "length_penalty"):
            if key in opts and opts[key] is not None:
                try:
                    kw[key] = (int(opts[key])
                                if key == "top_k"
                                else float(opts[key]))
                except Exception:
                    pass
        arr = self._model.tts(**kw)
        yield self._to_int16_bytes(np.asarray(arr, dtype=np.float32))

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry("clone", "any", "", "Clone from Reference WAV")]

    def default_voice(self) -> str:
        return "clone"


# ── Orpheus-TTS — Llama backbone + SNAC vocoder ──────────────────────


class _OrpheusHandler(_BaseTTSHandler):
    family = fd.TTS_ORPHEUS
    sample_rate = 24000

    def load(self, cfg: TTSLoadConfig) -> None:
        # Orpheus loads via standard transformers (Llama backbone)
        # plus a separate SNAC vocoder. The orpheus-speech PyPI package
        # ships a wrapper; we try it first, then fall back to a manual
        # AutoModelForCausalLM + snac setup.
        from orpheus_tts import OrpheusModel
        self._resolve_device(cfg)
        self._model = OrpheusModel(
            model_name=cfg.model_id,
            device=self._torch_device,
            dtype="bfloat16" if self._torch_device == "cuda" else "float32",
        )

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        self._seed_rng(opts)
        emo = str(opts.get("emotion_tags") or "").strip()
        if emo:
            tags = " ".join(f"<{t.strip().strip('<>')}>"
                             for t in emo.split(",") if t.strip())
            text = f"{tags} {text}"
        kw: dict[str, Any] = {
            "prompt": text,
            "voice": voice or "tara",
        }
        for key in ("temperature", "top_p"):
            if key in opts and opts[key] is not None:
                try:
                    kw[key] = float(opts[key])
                except Exception:
                    pass
        # OrpheusModel.generate_speech is a generator yielding PCM bytes
        # at 24 kHz int16. Pass through unchanged.
        for chunk in self._model.generate_speech(**kw):
            if isinstance(chunk, (bytes, bytearray)):
                yield bytes(chunk)
            else:
                yield self._to_int16_bytes(np.asarray(chunk, dtype=np.float32))

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry(*row) for row in [
            ("tara","English","F","Tara"),
            ("leah","English","F","Leah"),
            ("jess","English","F","Jess"),
            ("mia","English","F","Mia"),
            ("leo","English","M","Leo"),
            ("dan","English","M","Dan"),
            ("zac","English","M","Zac"),
            ("zoe","English","F","Zoe"),
        ]]

    def default_voice(self) -> str:
        return "tara"


# ── CSM (Sesame) — native transformers ───────────────────────────────


class _CSMHandler(_BaseTTSHandler):
    family = fd.TTS_CSM
    sample_rate = 24000

    def load(self, cfg: TTSLoadConfig) -> None:
        # CSM landed in transformers in mid-2025 as model_type="csm".
        import importlib
        tf = importlib.import_module("transformers")
        AutoProcessor = tf.AutoProcessor
        try:
            ModelCls = tf.CsmForConditionalGeneration
        except AttributeError:
            ModelCls = tf.AutoModel
            log.warning("csm: CsmForConditionalGeneration not in "
                        "transformers; falling back to AutoModel")
        self._resolve_device(cfg)
        self._processor = AutoProcessor.from_pretrained(
            cfg.model_id, trust_remote_code=True)
        self._model = self._load_model(
            ModelCls.from_pretrained, cfg.model_id, trust_remote_code=True)
        self._model.eval()
        try:
            self.sample_rate = int(self._model.config.audio_sampling_rate)
        except Exception:
            pass

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        import torch
        self._seed_rng(opts)
        inputs = self._processor(text=text, return_tensors="pt").to(
            self._torch_device)
        gen: dict[str, Any] = {}
        temp = opts.get("temperature")
        if temp is not None:
            try:
                t = float(temp)
                if t > 0.0:
                    gen["do_sample"] = True
                    gen["temperature"] = t
            except Exception:
                pass
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen)
        # CSM returns audio tokens decoded to PCM by the processor.
        try:
            audio = self._processor.batch_decode_audio(out)[0]
        except AttributeError:
            audio = out.cpu().to(torch.float32).numpy().reshape(-1)
        yield self._to_int16_bytes(np.asarray(audio, dtype=np.float32))

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry("conversational", "English", "",
                            "Conversational")]

    def default_voice(self) -> str:
        return "conversational"


# ── Higgs-Audio v2 — zero-shot cloning ───────────────────────────────


class _HiggsHandler(_BaseTTSHandler):
    family = fd.TTS_HIGGS
    sample_rate = 24000

    def load(self, cfg: TTSLoadConfig) -> None:
        from transformers import AutoModelForCausalLM, AutoProcessor
        self._resolve_device(cfg)
        self._processor = AutoProcessor.from_pretrained(
            cfg.model_id, trust_remote_code=True)
        self._model = self._load_model(
            AutoModelForCausalLM.from_pretrained, cfg.model_id, trust_remote_code=True)
        self._model.eval()

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        import torch
        self._seed_rng(opts)
        ref = str(opts.get("reference_audio") or "").strip()
        proc_kwargs: dict[str, Any] = {"text": text, "return_tensors": "pt"}
        if ref:
            proc_kwargs["reference_audio"] = ref
        inputs = self._processor(**proc_kwargs).to(self._torch_device)
        gen: dict[str, Any] = {}
        temp = opts.get("temperature")
        if temp is not None:
            try:
                t = float(temp)
                if t > 0.0:
                    gen["do_sample"] = True
                    gen["temperature"] = t
            except Exception:
                pass
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen)
        try:
            audio = self._processor.batch_decode_audio(out)[0]
        except AttributeError:
            audio = out.cpu().to(torch.float32).numpy().reshape(-1)
        yield self._to_int16_bytes(np.asarray(audio, dtype=np.float32))

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry("zero-shot", "Multi", "",
                            "Zero-shot (use Reference)")]

    def default_voice(self) -> str:
        return "zero-shot"


# ── VibeVoice (Microsoft) — long-form multi-speaker ──────────────────


class _VibeVoiceTTSHandler(_BaseTTSHandler):
    """Detection-only handler. Defers to the generic pipeline fallback
    since VibeVoice's transformers integration is recent and exposes
    a non-uniform API. If the future stabilises we'll add a dedicated
    path; for now `pipeline("text-to-speech")` works."""
    family = fd.TTS_VIBEVOICE
    sample_rate = 24000

    def load(self, cfg: TTSLoadConfig) -> None:
        from transformers import pipeline
        on_cuda = self._resolve_device(cfg)
        self._model = pipeline(
            "text-to-speech",
            model=cfg.model_id,
            device=0 if on_cuda else -1,
            trust_remote_code=True,
        )

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        self._seed_rng(opts)
        result = self._model(text)
        if isinstance(result, dict):
            arr = result.get("audio")
            sr = result.get("sampling_rate")
            if isinstance(sr, int):
                self.sample_rate = sr
            if arr is not None:
                yield self._to_int16_bytes(np.asarray(arr).reshape(-1))

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry("default", "Multi", "", "default")]

    def default_voice(self) -> str:
        return "default"


# ── Generic pipeline fallback ────────────────────────────────────────


class _PipelineTTSHandler(_BaseTTSHandler):
    family = fd.TTS_GENERIC
    sample_rate = 16000

    def __init__(self) -> None:
        super().__init__()
        self._pipe: Any = None

    def load(self, cfg: TTSLoadConfig) -> None:
        from transformers import pipeline
        on_cuda = self._resolve_device(cfg)
        self._pipe = pipeline(
            "text-to-speech",
            model=cfg.model_id,
            device=0 if on_cuda else -1,
        )

    def synth(self, text: str, voice: str,
              opts: dict[str, Any]) -> Iterator[bytes]:
        result = self._pipe(text)
        # pipeline output is {"audio": np.ndarray, "sampling_rate": int}
        if isinstance(result, dict):
            arr = result.get("audio")
            sr = result.get("sampling_rate")
            if isinstance(sr, int):
                self.sample_rate = sr
            if arr is None:
                return
            yield self._to_int16_bytes(np.asarray(arr).reshape(-1))

    def voices(self) -> list[VoiceEntry]:
        return [VoiceEntry("default", "model voice", "", "default")]

    def default_voice(self) -> str:
        return "default"

    def unload(self) -> None:
        self._pipe = None
        super().unload()


# Family → handler class
_HANDLERS: dict[str, type[_BaseTTSHandler]] = {
    fd.TTS_KOKORO:    _KokoroHandler,
    fd.TTS_VITS:      _VitsHandler,
    fd.TTS_SPEECHT5:  _SpeechT5Handler,
    fd.TTS_BARK:      _BarkHandler,
    fd.TTS_PARLER:    _ParlerHandler,
    fd.TTS_XTTS:      _XTTSHandler,
    fd.TTS_ORPHEUS:   _OrpheusHandler,
    fd.TTS_CSM:       _CSMHandler,
    fd.TTS_HIGGS:     _HiggsHandler,
    fd.TTS_VIBEVOICE: _VibeVoiceTTSHandler,
    fd.TTS_QWEN_TTS:  _PipelineTTSHandler,
    fd.TTS_GENERIC:   _PipelineTTSHandler,
}


# ── Public backend ───────────────────────────────────────────────────


class GenericTTSBackend(TTSBackend):
    name = "generic"
    default_model = "hexgrad/Kokoro-82M"
    default_voice = "af_heart"
    priority = 0
    sample_rate = 24000   # default; updated post-load

    def __init__(self) -> None:
        self._handler: _BaseTTSHandler | None = None
        self._family: str = ""
        self._model_id: str = ""

    def detected_family(self) -> str:
        return self._family

    def supports(self, feature: str) -> bool:
        return feature in fd.tts_capabilities(self._family or fd.TTS_GENERIC)

    def voices(self) -> list[VoiceEntry]:
        return self._handler.voices() if self._handler else []

    def runtime_options(self) -> list[OptionSpec]:
        return fd.tts_runtime_options(self._family) if self._family else []

    def load_sync(self, cfg: TTSLoadConfig) -> None:
        self._model_id = cfg.model_id
        family = fd.detect_tts_family(cfg.model_id) or fd.TTS_GENERIC
        log.info("generic-tts: detected family=%s for model=%s",
                 family, cfg.model_id)
        cls = _HANDLERS.get(family, _PipelineTTSHandler)
        self._handler = cls()
        try:
            self._handler.load(cfg)
            self._family = family
        except Exception as exc:
            log.warning("generic-tts: %s loader failed (%s); falling back "
                        "to pipeline()", family, exc)
            self._handler = _PipelineTTSHandler()
            self._family = fd.TTS_GENERIC
            self._handler.load(cfg)
        self.sample_rate = self._handler.sample_rate
        # Carry the handler's default voice through to the engine.
        if not self.default_voice or self.default_voice == "af_heart":
            dv = self._handler.default_voice()
            if dv:
                self.default_voice = dv

    def unload_sync(self) -> None:
        if self._handler is not None:
            try:
                self._handler.unload()
            except Exception as exc:
                log.debug("generic-tts unload exc: %s", exc)
        self._handler = None
        self._family = ""

    def synth_chunks_sync(self, text: str, voice: str,
                          opts: dict[str, Any]) -> Iterator[bytes]:
        if self._handler is None:
            raise RuntimeError("generic-tts: not loaded")
        for chunk in self._handler.synth(text, voice, opts or {}):
            if chunk:
                yield chunk

    def runtime_info(self) -> dict:
        return {
            "family": self._family,
            "model_id": self._model_id,
            "device": getattr(self._handler, "_torch_device", "cpu")
                       if self._handler else "cpu",
            "sample_rate": self.sample_rate,
        }
