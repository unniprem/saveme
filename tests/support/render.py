"""Helpers for the Auto Shorts tests: generated source videos (made once per session with the real
FFmpeg on PATH), render requests, and reading rendered pixels back."""
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

from tests.support.media import real_ffmpeg

_COMMON = ["-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
_H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
_TONE = ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000"]


def _ffmpeg(args: list[str], cwd: Path) -> None:
    proc = subprocess.run([real_ffmpeg(), *_COMMON, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg {' '.join(args)} failed:\n{proc.stderr}")


def make_render_media(dir: Path) -> dict[str, Path]:
    """Sources for rendering tests:

    - thirds.mp4: 960×540 at 25 fps, 6 s, the left third red, the middle green, the right blue,
      with a tone; a crop's position can be read from the colour it lands on.
    - portrait.mp4: 360×640 test pattern with a tone, 3 s.
    - silent.mp4: 640×360, no audio, 3 s.
    - rotated.mp4: 640×360 coded, displayed rotated 90° (360×640), with a tone, 3 s.
    - anamorphic.mp4: 480×540 coded with a 2:1 pixel aspect (displayed 960×540), 2 s.
    - intro.mp4 (1 s, 320×180 yellow, with a tone) and outro.mp4 (1 s, magenta, no audio).
    - logo.png: 200×100 white, half of it transparent.
    """
    thirds = ("color=c=red:s=320x540:r=25[a];color=c=lime:s=320x540:r=25[b];"
              "color=c=blue:s=320x540:r=25[c];[a][b][c]hstack=3")
    _ffmpeg(["-f", "lavfi", "-i", thirds, *_TONE, "-t", "6", "-map", "0:v", "-map", "1:a", *_H264,
             "-g", "25", "-c:a", "aac", "thirds.mp4"], dir)
    _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30", *_TONE, "-t", "3", "-map", "0:v",
             "-map", "1:a", *_H264, "-c:a", "aac", "portrait.mp4"], dir)
    _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "3", *_H264, "silent.mp4"], dir)
    _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", *_TONE, "-t", "3", "-map", "0:v",
             "-map", "1:a", *_H264, "-c:a", "aac", "flat.mp4"], dir)
    _ffmpeg(["-display_rotation", "90", "-i", "flat.mp4", "-c", "copy", "rotated.mp4"], dir)
    _ffmpeg(["-f", "lavfi", "-i", thirds, "-t", "2", "-vf", "scale=480:540,setsar=2/1", *_H264,
             "anamorphic.mp4"], dir)
    _ffmpeg(["-f", "lavfi", "-i", "color=c=yellow:s=320x180:r=25", *_TONE, "-t", "1", "-map", "0:v",
             "-map", "1:a", *_H264, "-c:a", "aac", "intro.mp4"], dir)
    _ffmpeg(["-f", "lavfi", "-i", "color=c=magenta:s=320x180:r=25", "-t", "1", *_H264, "outro.mp4"], dir)
    _ffmpeg(["-f", "lavfi", "-i", "color=c=white@1.0:s=100x100,format=rgba[a];"
             "color=c=black@0.0:s=100x100,format=rgba[b];[a][b]hstack=2", "-frames:v", "1", "logo.png"], dir)
    names = ["thirds.mp4", "portrait.mp4", "silent.mp4", "rotated.mp4", "anamorphic.mp4", "intro.mp4",
             "outro.mp4", "logo.png"]
    return {name: dir / name for name in names}


def as_source(media: Path, folder: Path) -> Path:
    """`media` copied as a ready source's media file (TEMP_DIR/<id>/media.mp4)."""
    folder.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy(media, folder / "media.mp4"))


def render_request(source: Path, workdir: Path, **change) -> dict:
    req = {"mode": "render", "kind": "video", "source": str(source), "workdir": str(workdir),
           "output": "clip-01.mp4", "start": 0.0, "end": 2.0, "aspect": "9:16", "width": 270,
           "height": 480, "fps": None, "quality": "standard", "encoder": "libx264", "threads": 2,
           "layout": {"mode": "center"}, "analysis": None, "speech": [], "subtitles": False,
           "fonts_dir": None, "logo": None, "intro": None, "outro": None, "max_bytes": 200 * 1024 * 1024,
           "memory_bytes": 1536 * 1024 * 1024}
    req.update(change)
    return req


def ffprobe(path: Path) -> dict:
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries",
                           "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,profile,"
                           "sample_rate,channels,start_time:format=duration,format_name", "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=60, check=True)
    data = json.loads(proc.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    start = (lambda s: None if s is None or "start_time" not in s else float(s["start_time"]))
    return {"video": video, "audio": audio, "duration": float(data["format"]["duration"]),
            "format": data["format"]["format_name"], "video_start": start(video), "audio_start": start(audio)}


def frame_rgb(path: Path, at: float) -> np.ndarray:
    """The frame of a video (or the image) nearest `at` seconds as an H×W×3 uint8 RGB array."""
    probe = ffprobe(path)
    width, height = probe["video"]["width"], probe["video"]["height"]
    seek = [] if path.suffix in (".jpg", ".png") else ["-ss", f"{at:.3f}"]
    proc = subprocess.run([real_ffmpeg(), *_COMMON, *seek, "-i", str(path), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    return np.frombuffer(proc.stdout, np.uint8).reshape(height, width, 3)


def colour_at(frame: np.ndarray, x: float, y: float, size: int = 4) -> str:
    """The dominant colour name of a small patch around (x, y), given as fractions of the frame."""
    h, w = frame.shape[:2]
    cx, cy = int(x * (w - 1)), int(y * (h - 1))
    patch = frame[max(0, cy - size):cy + size + 1, max(0, cx - size):cx + size + 1].reshape(-1, 3).mean(axis=0)
    r, g, b = patch
    if max(r, g, b) < 40:
        return "black"
    if r > 150 and g > 150 and b > 150:
        return "white"
    if r > 150 and g > 150 and b < 100:
        return "yellow"
    if r > 150 and b > 150 and g < 100:
        return "magenta"
    names = {"red": r, "green": g, "blue": b}
    top = max(names, key=names.get)
    others = sorted(names.values())[:2]
    return top if names[top] > 120 and names[top] > 2 * max(others) else "mixed"
