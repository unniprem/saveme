"""Auto Shorts: what the FFmpeg in use can do, which H.264 encoder to use, and the encoder
arguments for each quality preset (docs/AUTO_SHORTS_DESIGN.md §3, §4).

The capability check runs `ffmpeg -encoders` and `-filters` through the app/bin wrapper. These
list compiled-in components and read no media, so they are safe in the API process. Everything
else here is pure.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

from app.mediafile import FFMPEG

SOFTWARE = "libx264"
HARDWARE = "h264_videotoolbox"
AUDIO_ENCODER = "aac"
AUDIO_RATE = 48000

# Filters every render uses, and the one only burned-in captions need.
REQUIRED_FILTERS = frozenset({
    "crop", "scale", "pad", "setsar", "fps", "format", "split", "overlay", "vstack", "boxblur",
    "eq", "colorchannelmixer", "trim", "atrim", "setpts", "asetpts", "concat", "anullsrc", "apad",
    "aresample", "aformat",
})
CAPTION_FILTER = "ass"
_TIMEOUT = 20


@dataclass(frozen=True)
class Capabilities:
    encoders: frozenset[str]
    filters: frozenset[str]

    @property
    def missing(self) -> list[str]:
        """What a render can't do without: an H.264 encoder, AAC and the core filters."""
        missing = []
        if SOFTWARE not in self.encoders and HARDWARE not in self.encoders:
            missing.append("an H.264 encoder (libx264)")
        if AUDIO_ENCODER not in self.encoders:
            missing.append("the AAC encoder")
        missing += sorted(f"the {name} filter" for name in REQUIRED_FILTERS - self.filters)
        return missing

    @property
    def captions(self) -> bool:
        return CAPTION_FILTER in self.filters

    @property
    def jpeg(self) -> bool:
        return "mjpeg" in self.encoders


# `ffmpeg -encoders` entry: six flag characters, then the name. `ffmpeg -filters` entry: two or
# three flag characters, the name, then its pads (`V->V`, `|->A`, ...).
_ENCODER_LINE = re.compile(r"^\s*[VAS][F.][S.][X.][B.][D.]\s+([A-Za-z0-9_]\S*)\s", re.M)
_FILTER_LINE = re.compile(r"^\s*[T.][S.][C.]?\s+(\S+)\s+\S*->\S*\s", re.M)


def parse_encoders(listing: str) -> frozenset[str]:
    return frozenset(_ENCODER_LINE.findall(listing))


def parse_filters(listing: str) -> frozenset[str]:
    return frozenset(_FILTER_LINE.findall(listing))


def detect(ffmpeg: str = FFMPEG) -> Capabilities:
    """What the FFmpeg the wrapper resolves to was built with; empty sets if it can't run."""
    out = {}
    for what in ("encoders", "filters"):
        try:
            proc = subprocess.run([ffmpeg, "-hide_banner", f"-{what}"], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=_TIMEOUT)
            out[what] = proc.stdout if proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"render: can't list ffmpeg {what}: {exc!r}", file=sys.stderr)
            out[what] = ""
    return Capabilities(encoders=parse_encoders(out["encoders"]), filters=parse_filters(out["filters"]))


def choose_encoder(setting: str, caps: Capabilities) -> str | None:
    """The video encoder to use: VideoToolbox for `auto` when FFmpeg has it (macOS native runs),
    else libx264. A named encoder is used only if present. None when nothing fits."""
    if setting == HARDWARE:
        return HARDWARE if HARDWARE in caps.encoders else None
    if setting == SOFTWARE:
        return SOFTWARE if SOFTWARE in caps.encoders else None
    if HARDWARE in caps.encoders:
        return HARDWARE
    return SOFTWARE if SOFTWARE in caps.encoders else None


@dataclass(frozen=True)
class Quality:
    """One quality preset. `bpp` values are bits per pixel per frame: the rate cap for x264 (CRF
    keeps most frames well under it) and the target for VideoToolbox (which has no CRF)."""
    name: str
    x264_preset: str
    crf: int
    cap_bpp: float
    hw_bpp: float
    audio_kbps: int
    scale: float  # output size factor (draft renders at half size)


QUALITIES: dict[str, Quality] = {
    "draft": Quality("draft", "ultrafast", 30, 0.10, 0.06, 96, 0.5),
    "standard": Quality("standard", "veryfast", 21, 0.20, 0.16, 160, 1.0),
    "high": Quality("high", "medium", 18, 0.30, 0.25, 192, 1.0),
}


def scaled_size(width: int, height: int, quality: str) -> tuple[int, int]:
    """The output size for a preset: draft halves it, keeping both sides even and at least 2."""
    factor = QUALITIES[quality].scale
    return max(2, int(width * factor) // 2 * 2), max(2, int(height * factor) // 2 * 2)


def _kbps(bpp: float, width: int, height: int, fps: float) -> int:
    return max(200, int(bpp * width * height * fps / 1000))


def video_args(encoder: str, quality: str, *, width: int, height: int, fps: float, threads: int) -> list[str]:
    """Output arguments for H.264 High, yuv420p, BT.709 tags and a 2-second GOP."""
    q = QUALITIES[quality]
    gop = max(1, round(fps * 2))
    cap = _kbps(q.cap_bpp, width, height, fps)
    common = ["-pix_fmt", "yuv420p", "-g", str(gop), "-color_primaries", "bt709", "-color_trc", "bt709",
              "-colorspace", "bt709", "-color_range", "tv"]
    if encoder == HARDWARE:
        target = _kbps(q.hw_bpp, width, height, fps)
        return ["-c:v", HARDWARE, "-profile:v", "high", "-b:v", f"{target}k", "-maxrate", f"{cap}k",
                "-bufsize", f"{cap * 2}k", "-allow_sw", "0", "-realtime", "0", *common]
    return ["-c:v", SOFTWARE, "-preset", q.x264_preset, "-crf", str(q.crf), "-profile:v", "high",
            "-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k", "-threads", str(threads),
            "-x264-params", f"keyint={gop}:min-keyint={max(1, gop // 2)}:log-level=error", *common]


def audio_args(quality: str) -> list[str]:
    return ["-c:a", AUDIO_ENCODER, "-b:a", f"{QUALITIES[quality].audio_kbps}k", "-ar", str(AUDIO_RATE),
            "-ac", "2"]


def bitrate_cap_bytes_per_second(quality: str, width: int, height: int, fps: float) -> int:
    """Bytes a second the video can reach at most (plus audio), for disk reservations."""
    q = QUALITIES[quality]
    return (_kbps(q.cap_bpp, width, height, fps) + q.audio_kbps) * 1000 // 8
