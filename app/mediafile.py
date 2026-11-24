"""Local media files: probing, remuxing and (for Clip Studio) cutting.

Runs only inside the worker subprocess, and only on files in a job folder. Every
ffmpeg/ffprobe call goes through the app/bin wrappers, which pin
`-protocol_whitelist file,pipe`. Like app.worker, this module must not import yt_dlp
(or app.ytdl) at module level: the worker installs its connection guard first.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.errors import AppError
from app.filenames import media_type_for

WRAPPER_DIR = Path(__file__).resolve().parent / "bin"
FFMPEG = str(WRAPPER_DIR / "ffmpeg")
FFPROBE = str(WRAPPER_DIR / "ffprobe")
_PROBE_TIMEOUT = 60


@dataclass(frozen=True)
class Probe:
    """What ffprobe found. Cover art (an attached picture) is left out of kinds and codecs."""

    kinds: frozenset[str]
    duration: float | None
    format_name: str
    codecs: tuple[str, ...]


def stderr_tail(data: bytes) -> str:
    return data.decode(errors="replace")[-2000:]


def _duration(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 and seconds != float("inf") else None


def probe(path: Path) -> Probe | None:
    """Probe a local file through the ffprobe wrapper; None when ffprobe can't read it."""
    cmd = [FFPROBE, "-v", "error", "-show_entries",
           "format=format_name,duration:stream=codec_type,codec_name:stream_disposition=attached_pic",
           "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=_PROBE_TIMEOUT)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
        streams, fmt = data["streams"], data["format"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        print(f"worker: ffprobe failed: {exc!r}", file=sys.stderr)
        return None
    if not isinstance(streams, list) or not isinstance(fmt, dict):
        print(f"worker: ffprobe failed: {stderr_tail(proc.stderr)}", file=sys.stderr)
        return None
    kept = [s for s in streams
            if isinstance(s, dict) and not (s.get("disposition") or {}).get("attached_pic")]
    name = fmt.get("format_name")
    return Probe(kinds=frozenset(str(s.get("codec_type")) for s in kept),
                 duration=_duration(fmt.get("duration")),
                 format_name=name if isinstance(name, str) else "",
                 codecs=tuple(str(s.get("codec_name")) for s in kept))


def remux_to_mp4(src: Path) -> tuple[Path, str | None]:
    """Remux `src` to media.mp4 next to it (stream copy). On failure keep `src` and
    return a notice, provided the server can deliver its container at all."""
    dst = src.with_name("media.mp4")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", str(src), "-c", "copy", "-sn", "-dn", str(dst)]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)
        ok = proc.returncode == 0 and dst.is_file()
        if not ok:
            print(f"worker: remux to MP4 failed: {stderr_tail(proc.stderr)}", file=sys.stderr)
    except OSError as exc:
        print(f"worker: remux to MP4 failed: {exc!r}", file=sys.stderr)
        ok = False
    if ok:
        src.unlink()
        return dst, None
    dst.unlink(missing_ok=True)
    ext = src.suffix[1:]
    try:
        media_type_for(ext)
    except KeyError:
        raise AppError("unsupported") from None
    return src, f"Delivered as {ext.upper()} (couldn't convert to MP4)."


# Codecs a WebM file may hold; anything else in Matroska is MKV.
_WEBM_CODECS = frozenset({"vp8", "vp9", "av1", "opus", "vorbis"})
# Containers an upload may arrive in, by ffprobe's format_name, apart from Matroska/WebM.
_CONTAINERS = {"mov,mp4,m4a,3gp,3g2,mj2": "mp4", "mpegts": "ts", "flv": "flv", "avi": "avi"}


def container_ext(found: Probe) -> str | None:
    """The extension an uploaded file is stored under, or None for a container SaveMe
    doesn't take. Only mp4, mkv and webm can be delivered; the others are remuxed."""
    if found.format_name == "matroska,webm":
        return "webm" if found.codecs and set(found.codecs) <= _WEBM_CODECS else "mkv"
    return _CONTAINERS.get(found.format_name)


def prepare_upload(src: Path, workdir: Path) -> dict:
    """Check an uploaded file and make it workdir/media.<ext>, remuxed to MP4 where the
    codecs allow. Returns {"file", "ext", "duration", "notice"}."""
    found = probe(src)
    if found is None:
        raise AppError("unreadable_media")
    if "video" not in found.kinds:
        raise AppError("no_video")
    ext = container_ext(found)
    if found.duration is None or ext is None:
        raise AppError("unreadable_media")
    media = src.rename(workdir / f"media.{ext}")
    if ext != "mp4":
        try:
            media, _notice = remux_to_mp4(media)
        except AppError:  # a TS, FLV or AVI that won't go into MP4 can't be delivered
            raise AppError("unreadable_media") from None
    return {"file": media.name, "ext": media.suffix[1:], "duration": found.duration, "notice": None}
