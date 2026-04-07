@echo off
set CUDA_VISIBLE_DEVICES=-1
title Whisper STT Server (port 6600)
"C:\Users\prith\.voicemode-windows\stt-venv\Scripts\faster-whisper-server.exe" Systran/faster-whisper-medium --host 127.0.0.1 --port 6600
