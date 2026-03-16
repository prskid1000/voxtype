"""Apply Windows compatibility patches to VoiceMode.

Usage: python apply-patches.py <venv_path>

Patches:
1. conch.py - Replace fcntl with msvcrt for file locking
2. migration_helpers.py - Replace os.uname() with platform.system()
3. model_install.py - Replace os.uname() with platform.machine()
4. simple_failover.py - Fix response_format and language params
5. converse.py - Fix VAD resampling performance
"""

import os
import sys
from pathlib import Path


def patch_file(path: Path, replacements: list[tuple[str, str]], label: str) -> bool:
    if not path.exists():
        print(f"    [SKIP] {label}: file not found")
        return False
    content = path.read_text(encoding="utf-8")
    modified = False
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
            modified = True
    if modified:
        path.write_text(content, encoding="utf-8")
        print(f"    [OK] Patched: {label}")
    else:
        print(f"    [OK] Already patched: {label}")
    return modified


def main():
    if len(sys.argv) < 2:
        print("Usage: python apply-patches.py <venv_path>")
        sys.exit(1)

    venv_path = Path(sys.argv[1])
    site_packages = venv_path / "Lib" / "site-packages" / "voice_mode"

    if not site_packages.exists():
        print(f"    [FAIL] voice_mode not found in {venv_path}")
        sys.exit(1)

    print(f"\n    Applying Windows patches to: {site_packages}")

    # --- Patch 1: conch.py ---
    conch = site_packages / "conch.py"
    if conch.exists():
        content = conch.read_text(encoding="utf-8")
        if "import fcntl" in content and "import msvcrt" not in content:
            # Replace import
            content = content.replace(
                "import fcntl\nimport json\nimport os",
                'import json\nimport os\nimport sys\n\nif sys.platform == "win32":\n    import msvcrt\nelse:\n    import fcntl'
            )
            # Replace acquire lock
            content = content.replace(
                "            # Try to get exclusive lock (non-blocking)\n            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                '            # Try to get exclusive lock (non-blocking)\n'
                '            if sys.platform == "win32":\n'
                "                os.lseek(self._fd, 0, os.SEEK_SET)\n"
                "                os.write(self._fd, b'\\0')\n"
                "                os.lseek(self._fd, 0, os.SEEK_SET)\n"
                "                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)\n"
                "            else:\n"
                "                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
            )
            # Replace release lock
            content = content.replace(
                "                fcntl.flock(self._fd, fcntl.LOCK_UN)\n                os.close(self._fd)",
                '                if sys.platform == "win32":\n'
                "                    os.lseek(self._fd, 0, os.SEEK_SET)\n"
                "                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)\n"
                "                else:\n"
                "                    fcntl.flock(self._fd, fcntl.LOCK_UN)\n"
                "                os.close(self._fd)"
            )
            conch.write_text(content, encoding="utf-8")
            print("    [OK] Patched: conch.py (fcntl -> msvcrt)")
        else:
            print("    [OK] Already patched: conch.py")

    # --- Patch 2: migration_helpers.py ---
    patch_file(
        site_packages / "utils" / "migration_helpers.py",
        [
            ("import os\nimport subprocess", "import os\nimport platform\nimport subprocess"),
            ("os.uname().sysname", "platform.system()"),
        ],
        "migration_helpers.py (os.uname -> platform)"
    )

    # --- Patch 3: model_install.py ---
    patch_file(
        site_packages / "tools" / "whisper" / "model_install.py",
        [
            ("import os\nimport sys", "import os\nimport platform\nimport sys"),
            ("os.uname().machine", "platform.machine()"),
        ],
        "model_install.py (os.uname -> platform)"
    )

    # --- Patch 4: simple_failover.py ---
    failover = site_packages / "simple_failover.py"
    if failover.exists():
        content = failover.read_text(encoding="utf-8")
        modified = False

        if '"response_format": "text"' in content:
            content = content.replace('"response_format": "text"', '"response_format": "json"')
            modified = True

        if 'transcription_kwargs["language"] = "auto"' in content:
            content = content.replace(
                '            elif is_local_provider(base_url):\n'
                '                # Local whisper.cpp with auto mode - must pass "auto" explicitly\n'
                '                transcription_kwargs["language"] = "auto"\n'
                '            # For OpenAI with "auto" - don\'t pass parameter (auto-detect by default)',
                '            # Omit language param for auto-detect (works for both OpenAI and faster-whisper-server)'
            )
            modified = True

        if modified:
            failover.write_text(content, encoding="utf-8")
            print("    [OK] Patched: simple_failover.py (STT params)")
        else:
            print("    [OK] Already patched: simple_failover.py")

    # --- Patch 5: converse.py - VAD resampling ---
    converse = site_packages / "tools" / "converse.py"
    if converse.exists():
        content = converse.read_text(encoding="utf-8")
        if "signal.resample(chunk_flat" in content:
            content = content.replace(
                "                        # For VAD, we need to downsample from 24kHz to 16kHz\n"
                "                        # Use scipy's resample for proper downsampling\n"
                "                        from scipy import signal\n"
                "                        # Calculate the number of samples we need after resampling\n"
                "                        resampled_length = int(len(chunk_flat) * vad_sample_rate / SAMPLE_RATE)\n"
                "                        vad_chunk = signal.resample(chunk_flat, resampled_length)\n"
                "                        # Take exactly the number of samples VAD expects\n"
                "                        vad_chunk = vad_chunk[:vad_chunk_samples].astype(np.int16)\n"
                "                        chunk_bytes = vad_chunk.tobytes()",

                "                        # For VAD, we need to downsample from 24kHz to 16kHz\n"
                "                        # Use simple decimation (take every Nth sample) for speed\n"
                "                        ratio = SAMPLE_RATE / vad_sample_rate  # 1.5 for 24k->16k\n"
                "                        indices = np.round(np.arange(vad_chunk_samples) * ratio).astype(int)\n"
                "                        indices = np.clip(indices, 0, len(chunk_flat) - 1)\n"
                "                        vad_chunk = chunk_flat[indices].astype(np.int16)\n"
                "                        chunk_bytes = vad_chunk.tobytes()"
            )
            converse.write_text(content, encoding="utf-8")
            print("    [OK] Patched: converse.py (VAD resampling)")
        else:
            print("    [OK] Already patched: converse.py")

    print("\n    All patches applied successfully!")


if __name__ == "__main__":
    main()
