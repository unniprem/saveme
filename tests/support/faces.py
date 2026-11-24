"""Test videos with real faces (public-domain NASA portraits in tests/fixtures/faces), made once per
session with the real FFmpeg on PATH."""
import subprocess
from pathlib import Path

import numpy as np

from tests.support.media import real_ffmpeg

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "faces"
_COMMON = ["-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
_OUT = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
BG = "color=c=0x404850:s=1280x720:r=25"
TONE = "sine=frequency=220:sample_rate=48000"


def _ffmpeg(args: list[str], cwd: Path) -> None:
    proc = subprocess.run([real_ffmpeg(), *_COMMON, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr}")


def mouth_of(image: Path) -> tuple[int, int]:
    """The mouth centre of the one face in a 320×400 fixture, from YuNet's landmarks."""
    from app.faces import Detector, letterbox_size

    sw, sh = letterbox_size(320, 400)
    raw = subprocess.run([real_ffmpeg(), "-v", "error", "-i", str(image), "-vf",
                          f"scale={sw}:{sh},pad=640:640:0:0,format=bgr24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True, timeout=60).stdout
    faces = Detector(1).detect(np.frombuffer(raw, np.uint8).reshape(640, 640, 3))
    _score, _box, points = faces[0]
    scale = sw / 320
    return round((points[6] + points[8]) / 2 / scale), round((points[7] + points[9]) / 2 / scale)


def make_face_media(dir: Path) -> dict[str, Path]:
    """
    - moving_face.mp4: one face crossing a 1280×720 frame left to right in 5 s.
    - two_people.mp4: two faces side by side, still, 4 s.
    - cut.mp4: one face on the left for 2 s, then a hard cut (new background) to it on the right, 4 s.
    - speakers.mp4: the two people for 7 s; the left one's mouth flickers until 3.5 s, then the right one's.
    """
    a, j = str(FIXTURES / "armstrong.jpg"), str(FIXTURES / "jemison.jpg")
    _ffmpeg(["-f", "lavfi", "-i", BG, "-loop", "1", "-i", a, "-f", "lavfi", "-i", TONE, "-t", "5",
             "-filter_complex", "[0][1]overlay=x='80+160*t':y=160:shortest=1[v]", "-map", "[v]", "-map", "2:a",
             *_OUT, "moving_face.mp4"], dir)
    two = "[0][1]overlay=x=160:y=160[t];[t][2]overlay=x=800:y=160"
    _ffmpeg(["-f", "lavfi", "-i", BG, "-loop", "1", "-i", j, "-loop", "1", "-i", a, "-f", "lavfi", "-i", TONE,
             "-t", "4", "-filter_complex", f"{two}[v]", "-map", "[v]", "-map", "3:a", *_OUT, "two_people.mp4"], dir)
    # A real cut: the whole scene changes (another background), and the face is elsewhere.
    _ffmpeg(["-f", "lavfi", "-i", BG, "-f", "lavfi", "-i", "color=c=0xC08040:s=1280x720:r=25", "-loop", "1",
             "-i", a, "-f", "lavfi", "-i", TONE, "-t", "4", "-filter_complex",
             "[0][1]overlay=enable='gte(t,2)'[bg];[bg][2]overlay=x='if(lt(t,2),100,860)':y=160[v]",
             "-map", "[v]", "-map", "3:a", *_OUT, "cut.mp4"], dir)
    (jx, jy), (ax, ay) = mouth_of(FIXTURES / "jemison.jpg"), mouth_of(FIXTURES / "armstrong.jpg")
    flicker = "lt(mod(t,0.4),0.2)"
    boxes = (f"drawbox=x={160 + jx - 14}:y={160 + jy - 5}:w=28:h=10:color=black:t=fill:"
             f"enable='lt(t,3.5)*{flicker}',"
             f"drawbox=x={800 + ax - 14}:y={160 + ay - 5}:w=28:h=10:color=black:t=fill:"
             f"enable='gte(t,3.5)*{flicker}'")
    _ffmpeg(["-f", "lavfi", "-i", BG, "-loop", "1", "-i", j, "-loop", "1", "-i", a, "-f", "lavfi", "-i", TONE,
             "-t", "7", "-filter_complex", f"{two},{boxes}[v]", "-map", "[v]", "-map", "3:a", *_OUT,
             "speakers.mp4"], dir)
    names = ["moving_face.mp4", "two_people.mp4", "cut.mp4", "speakers.mp4"]
    return {name: dir / name for name in names}


def faces_in(frame_rgb: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Face boxes (x, y, w, h) in an RGB frame of any size, by YuNet."""
    from app.faces import SIZE, Detector, letterbox_size

    h, w = frame_rgb.shape[:2]
    sw, sh = letterbox_size(w, h)
    ys = np.linspace(0, h - 1, sh).astype(int)
    xs = np.linspace(0, w - 1, sw).astype(int)
    small = frame_rgb[ys[:, None], xs[None, :]][:, :, ::-1]
    padded = np.zeros((SIZE, SIZE, 3), np.uint8)
    padded[:sh, :sw] = small
    scale = sw / w
    return [tuple(v / scale for v in box) for _s, box, _p in Detector(1).detect(padded)]
