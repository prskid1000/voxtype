"""VoxType — local voice dictation overlay for Windows.

Pure-Python port of the Electron/React original. Routes LLM calls
through telecode's dual-protocol proxy at http://127.0.0.1:1235
instead of talking to LM Studio directly. Whisper STT and Kokoro TTS
remain as subprocess-managed sidecars."""
__version__ = "0.2.0"
