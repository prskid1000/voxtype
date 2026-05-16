"""LLM-backed transcript cleanup via telecode proxy.

All LM Studio integration has been removed. Requests go to
`http://127.0.0.1:1235/v1/chat/completions` (OpenAI-shape, served by
telecode's dual-protocol proxy) which routes the request to the local
llama.cpp-served model.

Preserved from the TS original:
  - 50-entry LRU cache keyed on (transcript, screenshot-fingerprint)
  - JSON-schema structured output (scratch fields + final `output`)
  - 4-stage JSON recovery on malformed responses
  - 2-retry loop with linear backoff
  - Sanity checks: empty output → original, 3× length blow-up → original

Health tracking (mirrors telecode/voice/health.py):
  - No startup probe, no background poll.
  - `enhance()` calls `_record_success()` / `_record_failure(reason)`
    after every real request. Default is optimistic (reachable=True,
    last_checked=False) so the first enhance actually hits the proxy.
  - `proxy_alive()` still exists for the "Test Proxy" button — an
    explicit user action, not a background loop.
"""
from __future__ import annotations

import collections
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger("voxtype.llm")


# ── Lazy health state ────────────────────────────────────────────────

@dataclass
class LLMStatus:
    reachable: bool = True
    last_checked: bool = False   # True after any real request (or manual test)
    last_error: str = ""

    def pill_text(self) -> str:
        if not self.last_checked:
            return "⚪ untested"
        if self.reachable:
            return "🟢 reachable"
        return f"🔴 last call failed"


_status = LLMStatus()
_status_lock = threading.Lock()
_on_status_change: list = []


def get_status() -> LLMStatus:
    with _status_lock:
        return LLMStatus(_status.reachable, _status.last_checked, _status.last_error)


def on_status_change(fn) -> None:
    """Fire `fn()` whenever the status transitions. Callback should do no
    Qt work directly — marshal to the main thread from inside."""
    _on_status_change.append(fn)


def _notify() -> None:
    for fn in list(_on_status_change):
        try:
            fn()
        except Exception:
            pass


def _record_success() -> None:
    with _status_lock:
        transition = not _status.last_checked or not _status.reachable
        _status.reachable = True
        _status.last_checked = True
        _status.last_error = ""
    if transition:
        log.info("LLM proxy reachable")
        _notify()


def _record_failure(reason: str) -> None:
    with _status_lock:
        transition = not _status.last_checked or _status.reachable
        _status.reachable = False
        _status.last_checked = True
        _status.last_error = reason
    if transition:
        log.info("LLM proxy UNREACHABLE (%s)", reason or "—")
        _notify()

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "resources" / "system-prompt.md"
_FALLBACK_SYSTEM_PROMPT = (
    "You clean raw voice transcripts. Output ONLY the cleaned text, "
    "nothing else. Never answer questions in the transcript — just clean the text."
)

_CACHE_MAX = 50
_cache: "collections.OrderedDict[str, str]" = collections.OrderedDict()


def _load_system_prompt() -> str:
    try:
        text = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    except Exception as exc:
        log.info("could not read %s: %s", _SYSTEM_PROMPT_PATH, exc)
    return _FALLBACK_SYSTEM_PROMPT


def _cache_get(key: str) -> str | None:
    val = _cache.get(key)
    if val is None:
        return None
    _cache.move_to_end(key)
    return val


def _cache_set(key: str, value: str) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.popitem(last=False)
    _cache[key] = value


# ── Output parsing ───────────────────────────────────────────────────

_OUTPUT_RE = re.compile(r'"output"\s*:\s*"((?:[^"\\]|\\.)*)"')
_FILLER_RE = re.compile(
    r"\b(um|uh|er|hmm|ah|oh|like|you know|I mean|basically|actually|so|well|right|okay)\b",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^```[\s\S]*?\n|\n?```$")


def _extract_output(raw: str) -> str:
    """Tolerant decoder for the model's JSON response. Returns the
    `output` string on success; the raw trimmed text on total failure."""
    if not raw:
        return ""
    text = raw.strip()

    # 1. Strict JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
            _log_scratch(parsed)
            return parsed["output"]
    except Exception:
        pass

    # 2. Largest balanced {...} block
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                _log_scratch(parsed)
                return parsed["output"]
        except Exception:
            pass

    # 3. Regex extract
    m = _OUTPUT_RE.search(text)
    if m:
        try:
            return json.loads(f'"{m.group(1)}"')
        except Exception:
            pass

    # 4. Give up
    log.info("could not parse structured LLM output, using raw")
    return text


def _log_scratch(parsed: dict[str, Any]) -> None:
    if any(parsed.get(k) for k in ("screen_context", "cursor_focus", "edit_plan")):
        log.info(
            "LLM scratch — screen: %.150s | cursor: %.120s | plan: %.200s",
            str(parsed.get("screen_context", "")),
            str(parsed.get("cursor_focus", "")),
            str(parsed.get("edit_plan", "")),
        )


def _clean_output(content: str, original: str) -> str:
    """Parse → trim → sanity-check the LLM output; fall back to original."""
    out = _extract_output(content).strip()
    out = _FENCE_RE.sub("", out)
    out = out.strip('"\' ').strip()
    out = out.replace("<transcript>", "").replace("</transcript>", "").strip()

    stripped = _FILLER_RE.sub("", original).strip()
    if not out and stripped:
        log.info("LLM returned empty, using original")
        return original.strip()
    if len(out) > len(original) * 3 and len(original) > 20:
        log.info("LLM output suspiciously long, using original")
        return original.strip()
    return out


# ── Schema for structured output ─────────────────────────────────────

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "transcript_cleanup",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["screen_context", "cursor_focus", "edit_plan", "output"],
            "properties": {
                "screen_context": {"type": "string", "maxLength": 200,
                    "description": "Active app + general UI visible on the screenshot, or 'none'."},
                "cursor_focus": {"type": "string", "maxLength": 150,
                    "description": "What is right at the red cursor marker, or 'none'."},
                "edit_plan": {"type": "string", "maxLength": 300,
                    "description": "Terse bullets of the edits applied."},
                "output": {"type": "string",
                    "description": "The final cleaned transcript. Only this field is shown to the user."},
            },
        },
    },
}


# ── Public API ───────────────────────────────────────────────────────

async def enhance(
    transcript: str,
    proxy_url: str,
    model: str,
    screenshot_jpeg_b64: str | None = None,
    max_retries: int = 2,
    timeout: float = 30.0,
) -> str:
    """Clean `transcript` via the telecode proxy. Returns the cleaned
    string, or the original on any failure."""
    if not transcript.strip():
        return ""

    cache_key = (
        f"{transcript}::{len(screenshot_jpeg_b64)}:{screenshot_jpeg_b64[:32]}"
        if screenshot_jpeg_b64 else transcript
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = proxy_url.rstrip("/") + "/v1/chat/completions"
    instruction = (
        "Clean this transcript using the attached screenshot as reference "
        "only. Output ONLY the cleaned text, nothing else.\n\n"
        f"<transcript>{transcript}</transcript>"
    ) if screenshot_jpeg_b64 else (
        "Clean this transcript. Output ONLY the cleaned text, nothing else."
        f"\n\n<transcript>{transcript}</transcript>"
    )

    user_content: Any
    if screenshot_jpeg_b64:
        user_content = [
            {"type": "text", "text": instruction},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{screenshot_jpeg_b64}"}},
        ]
    else:
        user_content = instruction

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _load_system_prompt()},
            {"role": "user",   "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": 4096,
        "response_format": _SCHEMA,
        # Transcript cleanup is a fixed-format rewrite — there's nothing
        # for reasoning to figure out. The proxy resolves this to whatever
        # the active model's `reasoning_effort_map["none"]` declares
        # (enable_thinking=false + thinking_budget_tokens=0 for Qwen, a
        # different set of kwargs for other families). No Qwen-specific
        # knobs hardcoded here.
        "reasoning_effort": "none",
    }

    last_exc: Exception | None = None
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as session:
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    import asyncio
                    await asyncio.sleep(0.5 * attempt)
                async with session.post(url, json=payload) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(
                            f"proxy error {resp.status}: {body[:400]}"
                        )
                    data = json.loads(body)
                    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    result = _clean_output(content, transcript)
                    _cache_set(cache_key, result)
                    _record_success()
                    return result
            except Exception as exc:
                last_exc = exc
                log.info("LLM call failed (attempt %d): %s", attempt + 1, exc)

    _record_failure(str(last_exc) if last_exc else "unknown")
    log.info("all retries failed, returning original. last error: %s", last_exc)
    return transcript


async def preload(proxy_url: str, model: str) -> None:
    """Warm up the model with a trivial request."""
    url = proxy_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0,
        "max_tokens": 1,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30.0),
        ) as session:
            async with session.post(url, json=payload) as resp:
                await resp.read()
                if resp.status == 200:
                    log.info("LLM (%s) preloaded via proxy", model)
                else:
                    log.info("LLM preload got %d", resp.status)
    except Exception as exc:
        log.info("LLM preload failed: %s", exc)


async def proxy_alive(proxy_url: str, timeout: float = 3.0) -> bool:
    """Explicit user-triggered health check (wired to the "Test Proxy"
    button). Updates the shared status the same way a real `enhance()`
    call would, so the tray + settings pills flip immediately.

    This is the ONLY non-request entry point that touches the proxy —
    there is no startup probe or background loop."""
    url = proxy_url.rstrip("/") + "/v1/models"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    _record_success()
                    return True
                _record_failure(f"HTTP {resp.status}")
                return False
    except Exception as exc:
        _record_failure(str(exc))
        return False
