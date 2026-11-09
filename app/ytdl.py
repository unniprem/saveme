"""yt-dlp option building, format safety, inspect mapping and selection (pure, no network)."""
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp.utils import determine_protocol, sanitize_url

from app.errors import AppError
from app.schemas import Quality

FFMPEG_WRAPPER_DIR = Path(__file__).parent / "bin"
NATIVE_PROTOCOLS = frozenset(
    {"http", "https", "m3u8", "m3u8_native", "http_dash_segments", "http_dash_segments_generator"}
)
TIERS = (1080, 720, 480)
TIER_TOLERANCE = 16


def base_opts(workdir: Path | None) -> dict:
    opts: dict = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": False,
        "noplaylist": True,
        "playlist_items": "1",
        "cachedir": False,
        "usenetrc": False,
        "proxy": "",
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
        "external_downloader": {"default": "native"},
        "hls_prefer_native": True,
        "outtmpl": {"default": "media.%(ext)s"},
        "ffmpeg_location": str(FFMPEG_WRAPPER_DIR),
        "js_runtimes": {"deno": {}},
        "remote_components": set(),
    }
    if workdir is not None:
        opts["paths"] = {"home": str(workdir), "temp": str(workdir)}
    return opts


def video_opts(quality: Quality) -> dict:
    opts: dict = {"format": "bv*+ba/b", "merge_output_format": "mp4/mkv"}
    if quality != "best":
        opts["format_sort"] = [f"res:{quality}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
    return opts


def audio_opts() -> dict:
    return {
        "format": "ba/b",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "2"}],
    }


def short_side(fmt: dict) -> int | None:
    w, h = fmt.get("width"), fmt.get("height")
    if w and h:
        return min(w, h)
    return h or None


def unwrap_first_entry(info: dict) -> tuple[dict, str | None]:
    if info.get("_type") != "playlist" and "entries" not in info:
        return info, None
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise AppError("playlist_unsupported")
    count = info.get("playlist_count") or len(entries)
    notice = f"This post has {count} videos; the first will be downloaded." if count > 1 else None
    return entries[0], notice


def _is_native(fmt: dict) -> bool:
    """Native-protocol check; infers the protocol (as yt-dlp will) when the raw format omits it."""
    if not fmt.get("url") and not fmt.get("protocol"):
        return False
    return determine_protocol(fmt) in NATIVE_PROTOCOLS


def keep_safe_formats(info: dict) -> dict:
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise AppError("live_unsupported")
    out = dict(info)
    formats = info.get("formats")
    if formats is None:
        if not _is_native(info):
            raise AppError("unsupported")
        return out
    if formats and all(x.get("has_drm") is True for x in formats):
        raise AppError("drm")
    safe = [x for x in formats if x.get("has_drm") is not True and _is_native(x)]
    if not safe:
        raise AppError("unsupported")
    out["formats"] = safe
    return out


def _has_video(x: dict) -> bool:
    return x.get("vcodec") != "none"


def _has_audio(x: dict) -> bool:
    return x.get("acodec") != "none"


def _source(info: dict) -> str:
    domain = urlparse(info.get("webpage_url") or info.get("original_url") or "").hostname or ""
    name = info.get("extractor_key") or info.get("extractor") or ""
    if name == "Generic":
        name = "Web page"
    return f"{name} · {domain}" if domain else name


def _thumbnail(info: dict) -> str | None:
    """`thumbnail`, else the last `thumbnails` entry (yt-dlp's own fallback, which
    process=False skips); a protocol-relative URL gets https."""
    url = info.get("thumbnail")
    if not url:
        entries = [t.get("url") for t in info.get("thumbnails") or [] if isinstance(t, dict)]
        urls = [u for u in entries if isinstance(u, str) and u]
        url = urls[-1] if urls else None
    return sanitize_url(url, scheme="https") if isinstance(url, str) else None


def inspect_payload(info: dict) -> dict:
    formats = info.get("formats") or [info]
    notices: list[str] = []
    video_fmts = [x for x in formats if _has_video(x)]
    sizes = [s for s in (short_side(x) for x in video_fmts) if s]
    max_side = max(sizes) if sizes else None
    tiers = [n for n in TIERS if any(abs(s - n) <= TIER_TOLERANCE for s in sizes)]
    qualities = [{"id": "best", "label": f"Best ({max_side}p)" if max_side else "Best"}]
    qualities += [{"id": str(n), "label": f"{n}p"} for n in tiers]
    if not tiers and max_side:
        notices.append(f"Only {max_side}p is available.")
    if any(x.get("vcodec") is None for x in formats):  # unknown video codec (spec §5.2)
        notices.append("Details for this link are unknown until download.")
    return {
        "title": info.get("title"),
        "duration_seconds": info.get("duration"),
        "thumbnail_url": _thumbnail(info),
        "source": _source(info),
        "webpage_url": info.get("webpage_url"),
        "video": {
            "available": bool(video_fmts),
            "qualities": qualities if video_fmts else [],
            "default": str(tiers[0]) if tiers else "best",
        },
        "audio": {"available": any(_has_audio(x) for x in formats)},
        "notices": notices,
    }


def chosen_formats(processed_info: dict) -> list[dict]:
    return processed_info.get("requested_formats") or [processed_info]


def downgrade_notice(chosen: list[dict], quality: Quality) -> str | None:
    if quality == "best":
        return None
    sides = [s for s in (short_side(x) for x in chosen if _has_video(x)) if s]
    if not sides:
        return None
    got, req = max(sides), int(quality)
    if abs(got - req) > TIER_TOLERANCE and got < req:
        return f"{req}p wasn't available; downloaded {got}p."
    return None
