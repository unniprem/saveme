"""Speech with known word times, for checking caption timing against real speech (opt-in tests).

Each word (or phrase) is said on its own by macOS `say`, trimmed to its voiced samples, and joined
to the others with known gaps, so the ground-truth onsets and ends come from the audio itself."""
import json
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from tests.support.media import real_ffmpeg

RATE = 16000
GAPS = (0.6, 0.35, 0.5, 0.8, 0.4, 0.3, 0.7, 0.45, 0.55, 0.35, 0.65, 0.4, 0.5, 0.3, 0.6, 0.45)


def can_speak() -> bool:
    return shutil.which("say") is not None


def make_speech(out: Path, items: list[str]) -> list[dict]:
    """Write out/speech.wav and out/media.mp4 (black video with the speech); return the truth:
    [{"text", "start", "end"}] for each item."""
    out.mkdir(parents=True, exist_ok=True)
    pieces, truth, t = [], [], 0.0
    with tempfile.TemporaryDirectory() as tmp:
        for i, item in enumerate(items):
            aiff = Path(tmp) / f"w{i}.aiff"
            subprocess.run(["say", "-o", str(aiff), item], check=True, timeout=60)
            raw = subprocess.run([real_ffmpeg(), "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", str(RATE),
                                  "-f", "s16le", "-"], check=True, capture_output=True, timeout=60).stdout
            audio = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
            level = np.abs(audio)
            voiced = np.nonzero(level > 0.02 * level.max())[0]
            audio = audio[voiced[0]:voiced[-1] + 1]
            gap = GAPS[i % len(GAPS)]
            pieces.append(np.zeros(int(gap * RATE), np.float32))
            t += gap
            truth.append({"text": item, "start": round(t, 3), "end": round(t + len(audio) / RATE, 3)})
            pieces.append(audio)
            t += len(audio) / RATE
    pieces.append(np.zeros(int(0.8 * RATE), np.float32))
    samples = np.concatenate(pieces)
    with wave.open(str(out / "speech.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    subprocess.run([real_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x202020:s=640x360:r=25", "-i", str(out / "speech.wav"), "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out / "media.mp4")],
                   check=True, timeout=120)
    (out / "truth.json").write_text(json.dumps(truth))
    return truth
