"""Generated test media, made once per session with the real ffmpeg on PATH
(never the app/bin wrappers)."""
import os
import shutil
import subprocess
from pathlib import Path

_WRAPPER_DIR = Path(__file__).resolve().parents[2] / "app" / "bin"
_COMMON = ["-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
_VIDEO_SRC = ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25"]
_AUDIO_SRC = ["-f", "lavfi", "-i", "sine=frequency=440"]
_H264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
_HLS = [
    *_VIDEO_SRC, *_AUDIO_SRC, "-t", "4",
    "-map", "0:v", "-map", "1:a", *_H264, "-c:a", "aac",
    "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
    "-var_stream_map", "v:0,agroup:aud a:0,agroup:aud,default:yes",
    "-master_pl_name", "master.m3u8",
    "-hls_segment_filename", "%v_%03d.ts", "%v.m3u8",
]


def real_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg is not on PATH; the offline tests need it to generate media")
    if Path(path).resolve().parent == _WRAPPER_DIR.resolve():
        raise RuntimeError("PATH resolves ffmpeg to the app/bin wrapper; put the real ffmpeg first")
    return path


def _ffmpeg(args: list[str], cwd: Path) -> None:
    proc = subprocess.run([real_ffmpeg(), *_COMMON, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg {' '.join(args)} failed:\n{proc.stderr}")


def make_media(dir: Path) -> dict[str, Path]:
    """Write the test media into `dir`; return {relative name: path}."""
    _ffmpeg([*_VIDEO_SRC, *_AUDIO_SRC, "-t", "2", "-map", "0:v", "-map", "1:a",
             *_H264, "-c:a", "aac", "-movflags", "+faststart", "sample.mp4"], dir)
    _ffmpeg([*_VIDEO_SRC, "-t", "2", *_H264, "-movflags", "+faststart", "video_only.mp4"], dir)
    _ffmpeg([*_AUDIO_SRC, "-t", "2", "-c:a", "libmp3lame", "-q:a", "4", "sample.mp3"], dir)
    (dir / "big.mp4").write_bytes(os.urandom(3 * 1024 * 1024))
    hls = dir / "hls"
    hls.mkdir()
    _ffmpeg(_HLS, hls)
    names = ["sample.mp4", "video_only.mp4", "sample.mp3", "big.mp4", "hls/master.m3u8"]
    out = {name: dir / name for name in names}
    missing = [name for name, path in out.items() if not path.is_file()]
    if missing:
        raise RuntimeError(f"media generation did not produce {missing}")
    return out
