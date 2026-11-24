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


def _master(playlist: str, codecs: str) -> str:
    return f'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=70000,CODECS="{codecs}"\n{playlist}\n'


def make_media(dir: Path) -> dict[str, Path]:
    """Write the test media into `dir`; return {relative name: path}."""
    _ffmpeg([*_VIDEO_SRC, *_AUDIO_SRC, "-t", "2", "-map", "0:v", "-map", "1:a",
             *_H264, "-c:a", "aac", "-movflags", "+faststart", "sample.mp4"], dir)
    _ffmpeg([*_VIDEO_SRC, *_AUDIO_SRC, "-t", "2", "-map", "0:v", "-map", "1:a",
             *_H264, "-c:a", "aac", "-movflags", "+faststart",
             "-metadata", "comment=http://leak.example/x", "tagged.mp4"], dir)
    _ffmpeg([*_VIDEO_SRC, "-t", "2", *_H264, "-movflags", "+faststart", "video_only.mp4"], dir)
    _ffmpeg([*_AUDIO_SRC, "-t", "2", "-c:a", "libmp3lame", "-q:a", "4", "sample.mp3"], dir)
    # Clip Studio sources: other containers an upload may arrive in, and a 10 s video with a
    # keyframe every second, a title and two chapters, to cut ranges from.
    _ffmpeg(["-i", "sample.mp4", "-c", "copy", "sample.mkv"], dir)
    _ffmpeg(["-i", "sample.mp4", "-c", "copy", "-f", "mpegts", "sample.ts"], dir)
    _ffmpeg([*_VIDEO_SRC, *_AUDIO_SRC, "-t", "2", "-map", "0:v", "-map", "1:a",
             "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "200k",
             "-c:a", "libopus", "sample.webm"], dir)
    (dir / "chapters.meta").write_text(
        ";FFMETADATA1\ntitle=Long Clip\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=5000\ntitle=One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=5000\nEND=10000\ntitle=Two\n")
    _ffmpeg([*_VIDEO_SRC, *_AUDIO_SRC, "-i", "chapters.meta", "-t", "10", "-map", "0:v", "-map", "1:a",
             "-map_metadata", "2", "-map_chapters", "2", *_H264, "-g", "25", "-keyint_min", "25",
             "-sc_threshold", "0", "-c:a", "aac", "-movflags", "+faststart", "long.mp4"], dir)
    for image in ("cover.jpg", "cover.png"):
        _ffmpeg(["-f", "lavfi", "-i", "color=c=red:size=64x64", "-frames:v", "1", image], dir)
    (dir / "junk.jpg").write_bytes(b"not an image")
    (dir / "big.jpg").write_bytes(b"\xff\xd8\xff\xe0" + bytes(64 * 1024))  # 32 s through /slow/
    (dir / "big.mp4").write_bytes(os.urandom(3 * 1024 * 1024))
    hls = dir / "hls"
    hls.mkdir()
    _ffmpeg(_HLS, hls)
    aac = hls / "aac"
    aac.mkdir()
    _ffmpeg([*_AUDIO_SRC, "-t", "4", "-c:a", "aac", "-b:a", "64k", "-f", "hls", "-hls_time", "2",
             "-hls_playlist_type", "vod", "-hls_segment_filename", "a_%03d.ts", "a.m3u8"], aac)
    (aac / "master.m3u8").write_text(_master("a.m3u8", "mp4a.40.2"))
    opus = hls / "opus"
    opus.mkdir()
    (opus / "master.m3u8").write_text(_master("../aac/a.m3u8", "opus"))
    names = ["sample.mp4", "tagged.mp4", "video_only.mp4", "sample.mp3", "sample.mkv", "sample.ts",
             "sample.webm", "long.mp4",
             "cover.jpg", "cover.png", "junk.jpg", "big.jpg", "big.mp4", "hls/master.m3u8",
             "hls/aac/master.m3u8", "hls/opus/master.m3u8"]
    out = {name: dir / name for name in names}
    missing = [name for name, path in out.items() if not path.is_file()]
    if missing:
        raise RuntimeError(f"media generation did not produce {missing}")
    return out
