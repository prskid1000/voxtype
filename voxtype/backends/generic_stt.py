"""Generic STT backend — one backend, many model families.

Paste any HuggingFace repo id (or local path); this backend sniffs the
model's `config.json`, picks the right transformers loader class, and
exposes the right per-family runtime options. Families covered:

  whisper        → WhisperForConditionalGeneration   (full knob set)
  wav2vec2       → Wav2Vec2ForCTC + AutoProcessor    (CTC, single lang)
  mms            → Wav2Vec2ForCTC + lang adapter     (1107 langs)
  seamless       → SeamlessM4Tv2 / SeamlessM4T S2T
  moonshine      → MoonshineForConditionalGeneration (English, fast)
  speech_to_text → Speech2TextForConditionalGeneration
  speecht5_asr   → SpeechT5ForSpeechToText
  parakeet       → NeMo TDT/RNNT via HF mirror
  qwen_audio     → Qwen2-Audio multimodal LLM
  generic_asr    → transformers.pipeline("automatic-speech-recognition")
                   — universal fallback covering everything else

Any family the user's transformers install doesn't know about falls
through to the generic pipeline, which itself can handle anything HF
registers as an ASR model.
"""
from __future__ import annotations

import gc
import logging
from typing import Any

import numpy as np

from voxtype.backends import family_detect as fd
from voxtype.backends.stt_base import LoadConfig, STTBackend, OptionSpec

log = logging.getLogger("voxtype.backends.generic_stt")


# ── Per-family handlers ──────────────────────────────────────────────
# Each handler owns its own model + processor pair. The dispatcher
# below picks the right one based on the detected family.


class _BaseHandler:
    family: str = ""

    def __init__(self) -> None:
        self._model: Any = None
        self._processor: Any = None
        self._torch_device: str = "cpu"
        self._torch_dtype: Any = None
        self._attn_impl: str = "auto"

    @staticmethod
    def _pick_dtype(pref: str, on_cuda: bool):
        import torch
        pref = (pref or "auto").lower()
        if pref == "auto":
            return torch.float16 if on_cuda else torch.float32
        if pref == "fp16":
            return torch.float16 if on_cuda else torch.float32
        if pref == "bf16":
            return torch.bfloat16
        return torch.float32

    def _resolve_device(self, cfg: LoadConfig) -> bool:
        import torch
        on_cuda = cfg.device == "cuda" and torch.cuda.is_available()
        if cfg.device == "cuda" and not on_cuda:
            log.warning("%s: cuda requested but unavailable — using CPU", self.family)
        self._torch_device = "cuda" if on_cuda else "cpu"
        self._torch_dtype = self._pick_dtype(cfg.dtype, on_cuda)
        self._attn_impl = (cfg.attn_impl or "auto").lower()
        return on_cuda

    def _from_pretrained_kwargs(self) -> dict[str, Any]:
        """Common kwargs for `from_pretrained` — dtype + attn impl.
        flash_attention_2 requires fp16/bf16; we downgrade silently
        on fp32 to spare users a cryptic transformers error."""
        import torch
        kw: dict[str, Any] = {"torch_dtype": self._torch_dtype}
        impl = self._attn_impl
        if impl in {"sdpa", "flash_attention_2", "eager"}:
            if (impl == "flash_attention_2"
                    and self._torch_dtype == torch.float32):
                log.warning("%s: flash_attention_2 requires fp16/bf16; "
                            "falling back to sdpa", self.family)
                impl = "sdpa"
            kw["attn_implementation"] = impl
        # "auto" → let transformers pick (sdpa on recent versions).
        return kw

    @staticmethod
    def _local_first(loader, *args, **kwargs):
        """Load from the HF cache with no network first — skips the
        per-load ETag/HEAD request transformers otherwise makes to check
        for updates, which is pure latency for an already-cached model.
        Falls back to an online load (may download) if the model isn't
        cached yet."""
        try:
            return loader(*args, local_files_only=True, **kwargs)
        except Exception:
            return loader(*args, **kwargs)

    def _load_model(self, loader, model_id, **extra):
        """Load an HF model straight onto the target device via accelerate
        `device_map` + `low_cpu_mem_usage`, skipping the CPU->GPU copy that
        a plain `from_pretrained(...).to(cuda)` incurs. Falls back to the
        plain load + `.to()` if device_map is unsupported for the arch."""
        kw = self._from_pretrained_kwargs()
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

    def load(self, cfg: LoadConfig) -> None:  # pragma: no cover — abstract
        raise NotImplementedError

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        raise NotImplementedError

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


class _WhisperHandler(_BaseHandler):
    family = fd.STT_WHISPER

    def load(self, cfg: LoadConfig) -> None:
        import torch
        from transformers import WhisperForConditionalGeneration, AutoProcessor
        self._resolve_device(cfg)
        self._processor = self._local_first(AutoProcessor.from_pretrained, cfg.model_id)
        self._model = self._load_model(
            WhisperForConditionalGeneration.from_pretrained, cfg.model_id)
        self._model.eval()
        if cfg.torch_compile:
            try:
                self._model = torch.compile(self._model, mode="reduce-overhead")
            except Exception as exc:
                log.warning("whisper: torch.compile failed (%s)", exc)
        if cfg.warmup:
            # Minimal forward pass: triggers lazy CUDA-kernel / cuDNN
            # autotuning (the real win) without a full 440-token decode.
            try:
                dummy = np.zeros(16000, dtype=np.float32)
                inputs = self._processor(
                    dummy, sampling_rate=16000, return_tensors="pt")
                feats = inputs.input_features.to(
                    self._torch_device, dtype=self._torch_dtype)
                with torch.no_grad():
                    self._model.generate(feats, max_new_tokens=1)
            except Exception as exc:
                log.warning("whisper warmup failed: %s", exc)

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        import torch
        inputs = self._processor(audio, sampling_rate=16000, return_tensors="pt")
        feats = inputs.input_features.to(self._torch_device, dtype=self._torch_dtype)
        beams = max(1, int(opts.get("num_beams") or 1))
        temp = float(opts.get("temperature") or 0.0)
        rep = float(opts.get("repetition_penalty") or 1.0)
        gen: dict = {
            "task": str(opts.get("task") or "transcribe"),
            "max_new_tokens": 440,
            "num_beams": beams,
        }
        # Sampling kicks in only when explicitly asked. Whisper's beam
        # search and sampling are mutually exclusive — if both num_beams>1
        # AND temperature>0 are set, prefer beams (the user's intent is
        # quality, not diversity).
        if temp > 0.0 and beams == 1:
            gen["do_sample"] = True
            gen["temperature"] = temp
        if rep > 1.0:
            gen["repetition_penalty"] = rep
        lang = str(opts.get("language") or "").lower()
        if lang and lang != "auto":
            gen["language"] = lang
        prompt = str(opts.get("initial_prompt") or "")
        if prompt:
            try:
                pids = self._processor.get_prompt_ids(
                    prompt, return_tensors="pt",
                ).to(self._torch_device)
                gen["prompt_ids"] = pids
            except Exception:
                pass
        with torch.no_grad():
            out = self._model.generate(feats, **gen)
        text = self._processor.batch_decode(out, skip_special_tokens=True)[0]
        return (text or "").strip()


class _Wav2Vec2Handler(_BaseHandler):
    """CTC family: Wav2Vec2, HuBERT, WavLM, UniSpeech."""
    family = fd.STT_WAV2VEC2

    def load(self, cfg: LoadConfig) -> None:
        import torch
        from transformers import AutoModelForCTC, AutoProcessor
        self._resolve_device(cfg)
        self._processor = self._local_first(AutoProcessor.from_pretrained, cfg.model_id)
        self._model = self._load_model(
            AutoModelForCTC.from_pretrained, cfg.model_id)
        self._model.eval()
        if cfg.torch_compile:
            try:
                self._model = torch.compile(self._model, mode="reduce-overhead")
            except Exception as exc:
                log.warning("ctc: torch.compile failed (%s)", exc)
        if cfg.warmup:
            try:
                self.transcribe(np.zeros(16000, dtype=np.float32), {})
            except Exception as exc:
                log.warning("ctc warmup failed: %s", exc)

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        import torch
        inputs = self._processor(
            audio, sampling_rate=16000, return_tensors="pt", padding=True,
        )
        input_values = inputs.input_values.to(self._torch_device,
                                               dtype=self._torch_dtype)
        with torch.no_grad():
            logits = self._model(input_values).logits
        ids = torch.argmax(logits, dim=-1)
        text = self._processor.batch_decode(ids)[0]
        return (text or "").strip()


class _MMSHandler(_Wav2Vec2Handler):
    """MMS = Wav2Vec2 with per-language adapter heads.

    Loading the right adapter requires `target_lang=<iso>` at
    from_pretrained() time AND calling `model.load_adapter(...)`.
    We rebuild the model when `language` changes — the engine's
    `_key()` doesn't include `language`, so we do it ourselves here."""
    family = fd.STT_MMS

    def __init__(self) -> None:
        super().__init__()
        self._loaded_lang = ""
        self._model_id = ""
        self._cfg: LoadConfig | None = None

    def load(self, cfg: LoadConfig) -> None:
        # Defer real load until we know the target language.
        self._cfg = cfg
        self._model_id = cfg.model_id
        self._resolve_device(cfg)
        # MMS adapter weights are tiny — initial load with default lang.
        self._ensure_lang("eng")

    def _ensure_lang(self, lang: str) -> None:
        from transformers import Wav2Vec2ForCTC, AutoProcessor
        if not lang or lang == "auto":
            lang = "eng"
        # MMS uses 3-letter ISO 639-3 codes. Convert common 2-letter codes.
        lang3 = _ISO2_TO_ISO3.get(lang, lang)
        if self._loaded_lang == lang3 and self._model is not None:
            return
        log.info("mms: switching adapter → %s", lang3)
        self._processor = AutoProcessor.from_pretrained(
            self._model_id, target_lang=lang3,
        )
        self._model = self._load_model(
            Wav2Vec2ForCTC.from_pretrained, self._model_id,
            target_lang=lang3, ignore_mismatched_sizes=True,
        )
        self._model.load_adapter(lang3)
        self._model.eval()
        self._loaded_lang = lang3

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        self._ensure_lang(str(opts.get("language") or "en").lower())
        return super().transcribe(audio, opts)


class _SeamlessHandler(_BaseHandler):
    family = fd.STT_SEAMLESS

    def load(self, cfg: LoadConfig) -> None:
        from transformers import AutoProcessor, SeamlessM4Tv2ForSpeechToText
        self._resolve_device(cfg)
        self._processor = self._local_first(AutoProcessor.from_pretrained, cfg.model_id)
        # Note: SeamlessM4Tv2 covers both v1 and v2 — single class.
        self._model = self._load_model(
            SeamlessM4Tv2ForSpeechToText.from_pretrained, cfg.model_id)
        self._model.eval()

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        import torch
        inputs = self._processor(audios=audio, sampling_rate=16000,
                                  return_tensors="pt")
        inputs = {k: v.to(self._torch_device,
                          dtype=self._torch_dtype if v.dtype.is_floating_point else v.dtype)
                  for k, v in inputs.items()}
        task = str(opts.get("task") or "transcribe")
        lang = str(opts.get("language") or "en").lower()
        # Explicit override beats derivation from task+language.
        override = str(opts.get("tgt_lang") or "").strip().lower()
        if override:
            tgt = override
        else:
            lang3 = _ISO2_TO_ISO3.get(lang, lang)
            tgt = "eng" if task == "translate" else lang3
        gen: dict = {
            "tgt_lang": tgt,
            "num_beams": max(1, int(opts.get("num_beams") or 5)),
        }
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen)
        text = self._processor.batch_decode(out, skip_special_tokens=True)[0]
        return (text or "").strip()


class _MoonshineHandler(_BaseHandler):
    family = fd.STT_MOONSHINE

    def load(self, cfg: LoadConfig) -> None:
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
        self._resolve_device(cfg)
        self._processor = self._local_first(AutoProcessor.from_pretrained, cfg.model_id)
        self._model = self._load_model(
            AutoModelForSpeechSeq2Seq.from_pretrained, cfg.model_id)
        self._model.eval()

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        import torch
        inputs = self._processor(audio, sampling_rate=16000, return_tensors="pt")
        feats = {k: v.to(self._torch_device,
                         dtype=self._torch_dtype if v.dtype.is_floating_point else v.dtype)
                 for k, v in inputs.items()}
        with torch.no_grad():
            out = self._model.generate(
                **feats, num_beams=max(1, int(opts.get("num_beams") or 1)),
                max_new_tokens=440,
            )
        text = self._processor.batch_decode(out, skip_special_tokens=True)[0]
        return (text or "").strip()


class _S2THandler(_BaseHandler):
    family = fd.STT_S2T

    def load(self, cfg: LoadConfig) -> None:
        from transformers import (
            Speech2TextForConditionalGeneration, Speech2TextProcessor,
        )
        self._resolve_device(cfg)
        self._processor = self._local_first(Speech2TextProcessor.from_pretrained, cfg.model_id)
        self._model = self._load_model(
            Speech2TextForConditionalGeneration.from_pretrained, cfg.model_id)
        self._model.eval()

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        import torch
        inputs = self._processor(audio, sampling_rate=16000, return_tensors="pt")
        feats = inputs.input_features.to(self._torch_device,
                                          dtype=self._torch_dtype)
        with torch.no_grad():
            out = self._model.generate(
                feats, num_beams=max(1, int(opts.get("num_beams") or 1)),
            )
        text = self._processor.batch_decode(out, skip_special_tokens=True)[0]
        return (text or "").strip()


class _PromptedASRHandler(_BaseHandler):
    """Shared handler for instruction-tuned audio LLMs (Voxtral,
    Granite-Speech, Phi-4-Multimodal, Qwen2-Audio). They all share the
    pattern: processor(audios=…, text=<instruction>) → model.generate.
    Concrete subclasses override `_default_prompt` and `_model_cls`."""
    _model_cls_name = "AutoModelForSpeechSeq2Seq"
    _default_prompt = "Transcribe the audio."

    def _resolve_model_cls(self):
        # Late import — keeps a missing transformers version from
        # breaking the whole module at import time.
        import importlib
        try:
            tf = importlib.import_module("transformers")
            return getattr(tf, self._model_cls_name)
        except AttributeError:
            from transformers import AutoModelForSpeechSeq2Seq
            log.warning("%s: %s not in transformers; using "
                        "AutoModelForSpeechSeq2Seq", self.family,
                        self._model_cls_name)
            return AutoModelForSpeechSeq2Seq

    def load(self, cfg: LoadConfig) -> None:
        from transformers import AutoProcessor
        self._resolve_device(cfg)
        self._processor = AutoProcessor.from_pretrained(
            cfg.model_id, trust_remote_code=True,
        )
        cls = self._resolve_model_cls()
        self._model = self._load_model(
            cls.from_pretrained, cfg.model_id, trust_remote_code=True)
        self._model.eval()

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        import torch
        prompt = str(opts.get("prompt") or "").strip() or self._default_prompt
        task = str(opts.get("task") or "transcribe")
        if task == "translate" and "translate" not in prompt.lower():
            prompt = "Translate the audio to English."
        try:
            inputs = self._processor(
                audios=audio, sampling_rate=16000, text=prompt,
                return_tensors="pt",
            )
        except TypeError:
            # Some processors don't accept `text=`; build the input
            # the conversational way.
            inputs = self._processor(
                audio, sampling_rate=16000, return_tensors="pt",
            )
        inputs = {k: v.to(self._torch_device,
                          dtype=self._torch_dtype if hasattr(v, "dtype")
                                  and v.dtype.is_floating_point else None)
                  for k, v in inputs.items() if hasattr(v, "to")}
        temp = float(opts.get("temperature") or 0.0)
        top_p = float(opts.get("top_p") or 0.0)
        gen: dict[str, Any] = {"max_new_tokens": 440}
        if temp > 0.0:
            gen["do_sample"] = True
            gen["temperature"] = temp
            if 0.0 < top_p < 1.0:
                gen["top_p"] = top_p
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen)
        # Skip the prompt tokens from the decoded output when the
        # processor doesn't do it for us.
        text = self._processor.batch_decode(out, skip_special_tokens=True)[0]
        return (text or "").strip()


class _VoxtralHandler(_PromptedASRHandler):
    family = fd.STT_VOXTRAL
    _model_cls_name = "VoxtralForConditionalGeneration"
    _default_prompt = ""   # Voxtral has a built-in transcription mode


class _GraniteSpeechHandler(_PromptedASRHandler):
    family = fd.STT_GRANITE
    _model_cls_name = "GraniteSpeechForConditionalGeneration"


class _Phi4MMHandler(_PromptedASRHandler):
    family = fd.STT_PHI4MM
    _model_cls_name = "Phi4MultimodalForCausalLM"


class _QwenAudioHandler(_PromptedASRHandler):
    family = fd.STT_QWEN_AUDIO
    _model_cls_name = "Qwen2AudioForConditionalGeneration"


class _GenericPipelineHandler(_BaseHandler):
    """Universal fallback — `transformers.pipeline("automatic-speech-recognition")`.
    Handles any model HF registers as ASR, including ones we haven't
    written a specific handler for."""
    family = fd.STT_GENERIC

    def __init__(self) -> None:
        super().__init__()
        self._pipe: Any = None

    def load(self, cfg: LoadConfig) -> None:
        from transformers import pipeline
        on_cuda = self._resolve_device(cfg)
        self._pipe = pipeline(
            "automatic-speech-recognition",
            model=cfg.model_id,
            device=0 if on_cuda else -1,
            torch_dtype=self._torch_dtype,
        )

    def transcribe(self, audio: np.ndarray, opts: dict[str, Any]) -> str:
        # pipeline accepts a numpy float32 array directly.
        out = self._pipe(audio.astype(np.float32))
        if isinstance(out, dict):
            return str(out.get("text") or "").strip()
        return str(out).strip()

    def unload(self) -> None:
        self._pipe = None
        super().unload()


# Family → handler class.
_HANDLERS: dict[str, type[_BaseHandler]] = {
    fd.STT_WHISPER:    _WhisperHandler,
    fd.STT_WAV2VEC2:   _Wav2Vec2Handler,
    fd.STT_MMS:        _MMSHandler,
    fd.STT_SEAMLESS:   _SeamlessHandler,
    fd.STT_MOONSHINE:  _MoonshineHandler,
    fd.STT_S2T:        _S2THandler,
    fd.STT_SPEECHT5:   _GenericPipelineHandler,
    fd.STT_QWEN_AUDIO: _QwenAudioHandler,
    fd.STT_VOXTRAL:    _VoxtralHandler,
    fd.STT_GRANITE:    _GraniteSpeechHandler,
    fd.STT_PHI4MM:     _Phi4MMHandler,
    fd.STT_VIBEVOICE:  _GenericPipelineHandler,
    fd.STT_GENERIC:    _GenericPipelineHandler,
}


# ── Public backend class ─────────────────────────────────────────────


class GenericSTTBackend(STTBackend):
    """One backend to rule them all."""
    name = "generic"
    default_model = "openai/whisper-large-v3"
    priority = 0   # universal fallback; specialists outrank if needed

    def __init__(self) -> None:
        self._handler: _BaseHandler | None = None
        self._family: str = ""
        self._model_id: str = ""

    # ── Identity / capabilities ──────────────────────────────────────

    def detected_family(self) -> str:
        return self._family or ""

    def supports(self, feature: str) -> bool:
        return feature in fd.stt_capabilities(self._family or fd.STT_GENERIC)

    def language_options(self) -> list[tuple[str, str]]:
        from voxtype.backends.shared import WHISPER_LANGUAGES
        if self._family == fd.STT_MMS:
            # MMS supports 1107 langs; show a curated subset (its
            # processor uses 3-letter ISO 639-3, but we accept 2-letter
            # input and map). For now reuse the Whisper table — covers
            # the user's likely choices.
            return WHISPER_LANGUAGES
        if self._family == fd.STT_SEAMLESS:
            return WHISPER_LANGUAGES
        if self._family in {fd.STT_WAV2VEC2, fd.STT_MOONSHINE,
                             fd.STT_SPEECHT5}:
            # Single-language families. UI hides the picker.
            return [("en", "English")]
        return WHISPER_LANGUAGES

    def runtime_options(self) -> list[OptionSpec]:
        return fd.stt_runtime_options(self._family) if self._family else []

    # ── Lifecycle ────────────────────────────────────────────────────

    def load_sync(self, cfg: LoadConfig) -> None:
        self._model_id = cfg.model_id
        family = fd.detect_stt_family(cfg.model_id) or fd.STT_GENERIC
        log.info("generic-stt: detected family=%s for model=%s",
                 family, cfg.model_id)
        self._family = family
        cls = _HANDLERS.get(family, _GenericPipelineHandler)
        self._handler = cls()
        try:
            self._handler.load(cfg)
        except Exception as exc:
            log.warning("generic-stt: %s loader failed (%s); falling back "
                        "to pipeline()", family, exc)
            self._handler = _GenericPipelineHandler()
            self._family = fd.STT_GENERIC
            self._handler.load(cfg)

    def unload_sync(self) -> None:
        if self._handler is not None:
            try:
                self._handler.unload()
            except Exception as exc:
                log.debug("generic-stt unload exc: %s", exc)
        self._handler = None
        self._family = ""

    def transcribe_sync(self, pcm: bytes, opts: dict[str, Any]) -> str:
        if self._handler is None:
            raise RuntimeError("generic-stt: not loaded")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return self._handler.transcribe(audio, opts or {})

    def runtime_info(self) -> dict:
        h = self._handler
        return {
            "family": self._family,
            "model_id": self._model_id,
            "device": getattr(h, "_torch_device", "cpu") if h else "cpu",
            "dtype": str(getattr(h, "_torch_dtype", "")) if h else "",
        }


# ── Helpers ──────────────────────────────────────────────────────────

# Bare-minimum ISO 639-1 → ISO 639-3 map for MMS / Seamless. Adding more
# entries is a one-line tweak.
_ISO2_TO_ISO3: dict[str, str] = {
    "en": "eng", "es": "spa", "fr": "fra", "de": "deu", "it": "ita",
    "pt": "por", "nl": "nld", "ru": "rus", "pl": "pol", "tr": "tur",
    "zh": "cmn", "ja": "jpn", "ko": "kor", "ar": "ara", "hi": "hin",
    "bn": "ben", "ur": "urd", "sw": "swh", "ta": "tam", "te": "tel",
    "ml": "mal", "mr": "mar", "gu": "guj", "kn": "kan", "pa": "pan",
    "vi": "vie", "th": "tha", "id": "ind", "ms": "msa", "fa": "fas",
    "he": "heb", "el": "ell", "cs": "ces", "hu": "hun", "ro": "ron",
    "fi": "fin", "sv": "swe", "no": "nor", "da": "dan", "uk": "ukr",
}
