import http from 'http';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { execSync } from 'child_process';

const VOICEMODE_ENV = path.join(os.homedir(), '.voicemode', 'voicemode.env');
const KOKORO_URL = 'http://127.0.0.1:6500';

// Curated top voices (the full list is 60+, too many for a menu)
export const FEATURED_VOICES = [
  { id: 'af_sky', label: 'Sky (F, American)' },
  { id: 'af_heart', label: 'Heart (F, American)' },
  { id: 'af_bella', label: 'Bella (F, American)' },
  { id: 'af_nova', label: 'Nova (F, American)' },
  { id: 'af_sarah', label: 'Sarah (F, American)' },
  { id: 'af_nicole', label: 'Nicole (F, American)' },
  { id: 'af_jessica', label: 'Jessica (F, American)' },
  { id: 'am_adam', label: 'Adam (M, American)' },
  { id: 'am_michael', label: 'Michael (M, American)' },
  { id: 'am_eric', label: 'Eric (M, American)' },
  { id: 'am_liam', label: 'Liam (M, American)' },
  { id: 'bf_emma', label: 'Emma (F, British)' },
  { id: 'bf_alice', label: 'Alice (F, British)' },
  { id: 'bm_george', label: 'George (M, British)' },
  { id: 'bm_daniel', label: 'Daniel (M, British)' },
] as const;

/**
 * Read current voice from ~/.voicemode/voicemode.env
 */
export function getCurrentVoice(): string {
  try {
    if (!fs.existsSync(VOICEMODE_ENV)) return 'af_sky';
    const content = fs.readFileSync(VOICEMODE_ENV, 'utf-8');

    // Look for uncommented VOICEMODE_VOICES line
    for (const line of content.split('\n')) {
      const trimmed = line.trim();
      if (trimmed.startsWith('#')) continue;
      const match = trimmed.match(/^VOICEMODE_VOICES\s*=\s*(.+)/);
      if (match) {
        // First voice in comma-separated list is the primary
        return match[1].split(',')[0].trim();
      }
    }
    return 'af_sky';
  } catch {
    return 'af_sky';
  }
}

/**
 * Set voice in ~/.voicemode/voicemode.env
 * Updates VOICEMODE_VOICES line, or adds it if missing
 */
export function setVoice(voiceId: string) {
  const dir = path.dirname(VOICEMODE_ENV);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }

  let content = '';
  if (fs.existsSync(VOICEMODE_ENV)) {
    content = fs.readFileSync(VOICEMODE_ENV, 'utf-8');
  }

  const newLine = `VOICEMODE_VOICES=${voiceId},alloy`;
  let found = false;

  const lines = content.split('\n').map((line) => {
    const trimmed = line.trim();
    // Match both commented and uncommented VOICEMODE_VOICES
    if (trimmed.match(/^#?\s*VOICEMODE_VOICES\s*=/)) {
      found = true;
      return newLine;
    }
    return line;
  });

  if (!found) {
    lines.push('', '# Voice for VoiceMode TTS (set by VoxType)', newLine);
  }

  fs.writeFileSync(VOICEMODE_ENV, lines.join('\n'), 'utf-8');
  console.log(`[VoxType] Kokoro voice set to: ${voiceId}`);
}

/**
 * Preload Kokoro TTS by sending a short TTS request to warm up the model.
 */
export async function preloadKokoro(): Promise<void> {
  console.log('[VoxType] Preloading Kokoro TTS model...');
  const voice = getCurrentVoice();
  const payload = JSON.stringify({
    model: 'kokoro',
    input: 'ok',
    voice,
  });
  return new Promise((resolve) => {
    const req = http.request(`${KOKORO_URL}/v1/audio/speech`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    }, (res) => {
      // Drain response
      res.on('data', () => {});
      res.on('end', () => {
        if (res.statusCode === 200) {
          console.log('[VoxType] Kokoro TTS model preloaded');
        } else {
          console.log(`[VoxType] Kokoro preload returned ${res.statusCode} (non-fatal)`);
        }
        resolve();
      });
    });
    req.on('error', (e) => {
      console.log(`[VoxType] Kokoro preload failed (non-fatal): ${e.message}`);
      resolve();
    });
    req.setTimeout(30000, () => { req.destroy(); resolve(); });
    req.write(payload);
    req.end();
  });
}

/**
 * Unload Kokoro TTS by killing the uvicorn process and restarting the scheduled task.
 * The model reloads on the next TTS request.
 */
export async function unloadKokoro(): Promise<void> {
  console.log('[VoxType] Unloading Kokoro TTS (restarting server without model)...');
  try {
    execSync('taskkill /IM uvicorn.exe /F', { timeout: 5000, stdio: 'ignore' });
    console.log('[VoxType] Kokoro process killed');
  } catch {
    console.log('[VoxType] Kokoro process not running');
  }
  // Restart the scheduled task so the server is ready (model loads on first request)
  try {
    execSync('schtasks /Run /TN "VoiceMode-Kokoro-TTS"', { timeout: 5000, stdio: 'ignore' });
    console.log('[VoxType] Kokoro task restarted (no model loaded)');
  } catch {
    console.log('[VoxType] Could not restart Kokoro task');
  }
}

/**
 * Fetch all available voices from Kokoro API (live query)
 */
export function fetchAllVoices(): Promise<string[]> {
  return new Promise((resolve) => {
    const req = http.request(`${KOKORO_URL}/v1/audio/voices`, { method: 'GET' }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        try {
          const json = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
          resolve(json.voices || []);
        } catch {
          resolve([]);
        }
      });
    });
    req.on('error', () => resolve([]));
    req.end();
  });
}
