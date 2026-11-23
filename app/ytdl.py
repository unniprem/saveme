"""yt-dlp option building, format safety, inspect mapping and selection (pure, no network)."""
import itertools
import re
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
# Playlist entries examined for a first item; entries can be lazy pages from the source.
MAX_PLAYLIST_SCAN = 50
# An acodec that is AAC, which M4A stream-copies: "aac", a bare "mp4a", MPEG-4 audio
# "mp4a.40.<n>" except MP3 in MP4 (n = 32, 33, 34), and MPEG-2 AAC "mp4a.66" to "mp4a.68".
# Other "mp4a.*" (MP3, AC-3, E-AC-3, Opus) are not. Case-insensitive. Inspect's m4a flag
# and the M4A format filter both use it; no backslashes, so it fits yt-dlp's quoted filter.
AAC_CODEC = "(?i)^(?:aac|mp4a|mp4a[.]40[.](?!3[234]$)[0-9]+|mp4a[.]6[678])$"
_AAC = re.compile(AAC_CODEC)


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


def m4a_opts() -> dict:
    """AAC only, stream-copied into an M4A container (no re-encode)."""
    return {
        "format": f"ba[acodec~='{AAC_CODEC}']/b[acodec~='{AAC_CODEC}']",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
    }


# FFmpegMetadata writes title, artist, date, description and chapters. The two meta_ fields
# below are copied verbatim into `-metadata purl=` / `comment=`, blanking the source link
# (and any `comment` tag the source file already carries).
TAGS_PP = {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True, "add_infojson": False}
NO_SOURCE_LINK = {"meta_purl": "", "meta_comment": ""}


def output_opts(output: str, quality: Quality) -> dict:
    """The yt-dlp options for `output`, with the tag postprocessor last so it runs
    after any audio extraction."""
    opts = {"video": lambda: video_opts(quality), "audio": audio_opts, "m4a": m4a_opts}[output]()
    return {**opts, "postprocessors": [*opts.get("postprocessors", []), TAGS_PP]}


def without_source_link(info: dict) -> dict:
    return {**info, **NO_SOURCE_LINK}


# FFmpegMetadata passes the description as single `-metadata description=…` and
# `synopsis=…` arguments. Linux refuses to start a program with any one argument over
# 128 KiB (MAX_ARG_STRLEN), so the tags get at most this much of it.
MAX_DESCRIPTION_TAG_BYTES = 16 * 1024


def _cut_utf8(text: str, limit: int) -> str:
    """`text` cut to at most `limit` bytes of UTF-8, never inside a character."""
    data = text.encode("utf-8", "surrogatepass")
    if len(data) <= limit:
        return text
    end = limit
    while end and (data[end] & 0xC0) == 0x80:  # a continuation byte: back up to the character's start
        end -= 1
    return data[:end].decode("utf-8", "surrogatepass")


def with_capped_description(info: dict) -> dict:
    """`info` whose description tags (meta_description, meta_synopsis, which FFmpegMetadata
    uses in place of the description) are cut to MAX_DESCRIPTION_TAG_BYTES when the
    description is longer. info["description"] itself is unchanged."""
    description = info.get("description")
    if not isinstance(description, str):
        return info
    cut = _cut_utf8(description, MAX_DESCRIPTION_TAG_BYTES)
    if cut is description:
        return info
    return {**info, "meta_description": cut, "meta_synopsis": cut}


def track_available(payload: dict, output: str) -> bool:
    """Whether the inspected `payload` offers the track `output` needs."""
    if output == "m4a":
        return payload["audio"]["m4a"]
    return payload[output]["available"]


def _is_aac(fmt: dict) -> bool:
    """The same test as yt-dlp's `acodec~=` filter in m4a_opts (re.search)."""
    acodec = fmt.get("acodec")
    return isinstance(acodec, str) and _AAC.search(acodec) is not None


def short_side(fmt: dict) -> int | None:
    w, h = fmt.get("width"), fmt.get("height")
    if w and h:
        return min(w, h)
    return h or None


_CODEC_PREFIXES = (
    (("av01",), "AV1"),
    (("vp09", "vp9"), "VP9"),
    (("hev1", "hvc1", "h265", "hevc"), "H.265"),
    (("avc1", "avc3", "h264"), "H.264"),
)


def codec_family(vcodec: str | None) -> str | None:
    if not isinstance(vcodec, str):
        return None
    v = vcodec.lower()
    for prefixes, family in _CODEC_PREFIXES:
        if v.startswith(prefixes):
            return family
    return None


def best_label(video_fmts: list[dict]) -> str:
    """Label for the "best" quality: real size, frame rate above 30, and codecs."""
    sized = [(s, x) for x in video_fmts if (s := short_side(x))]
    if not sized:
        return "Best"
    top = max(s for s, _ in sized)
    kept = [x for s, x in sized if s == top]
    fps_values = [x["fps"] for x in kept
                  if isinstance(x.get("fps"), (int, float)) and not isinstance(x["fps"], bool) and x["fps"] > 0]
    top_fps = max(fps_values) if fps_values else None
    if top_fps is not None:
        kept = [x for x in kept if x.get("fps") == top_fps]
    fps_text = str(round(top_fps)) if top_fps is not None and top_fps > 30 else ""
    families = [codec_family(x.get("vcodec")) for x in kept]
    codecs = ""
    if None not in families:
        codecs = "/".join(f for f in ("AV1", "VP9", "H.265", "H.264") if f in families)
    return f"Best ({top}p{fps_text}" + (f" \u00b7 {codecs}" if codecs else "") + ")"


def unwrap_first_entry(info: dict) -> tuple[dict, str | None]:
    """The first entry of a playlist and a notice when there are more. Entries may be
    lazy (a generator or yt-dlp's paged lists), so at most two non-empty entries out of
    the first MAX_PLAYLIST_SCAN are read; the rest are never fetched."""
    if info.get("_type") != "playlist" and "entries" not in info:
        return info, None
    entries = info.get("entries") or []
    scanned = itertools.islice(entries, MAX_PLAYLIST_SCAN)
    found = list(itertools.islice((e for e in scanned if e), 2))
    if not found:
        raise AppError("playlist_unsupported")
    count = info.get("playlist_count")
    if not count and isinstance(entries, (list, tuple)):
        count = sum(1 for e in entries if e)
    if count:
        notice = f"This post has {count} videos; the first will be downloaded." if count > 1 else None
    else:
        notice = ("This post has more than one video; the first will be downloaded."
                  if len(found) > 1 else None)
    return found[0], notice


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
    qualities = [{"id": "best", "label": best_label(video_fmts)}]
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
        "audio": {"available": any(_has_audio(x) for x in formats),
                  "m4a": any(_is_aac(x) for x in formats)},
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
