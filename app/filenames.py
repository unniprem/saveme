"""Download filenames and Content-Disposition. Titles never touch the filesystem."""
import re
import unicodedata
from urllib.parse import quote

_MEDIA_TYPES = {
    "mp4": "video/mp4",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
}
# Control characters, characters Windows forbids in names, and lone surrogates (not UTF-8).
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff/\\:*?"<>|]')
_WS = re.compile(r"\s+")
_RESERVED = re.compile(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$")
_MAX_STEM_BYTES = 150


def _tidy(text: str) -> str:
    text = _WS.sub(" ", text)
    return text.strip(". ")


def sanitize_filename(title: str | None, ext: str) -> str:
    if ext not in _MEDIA_TYPES:
        raise ValueError(f"unsupported extension: {ext!r}")
    stem = unicodedata.normalize("NFKC", title or "")
    stem = _BAD_CHARS.sub(" ", stem)
    stem = _tidy(stem)
    if _RESERVED.match(stem):
        stem += "_"
    stem = stem.encode("utf-8")[:_MAX_STEM_BYTES].decode("utf-8", errors="ignore")
    stem = _tidy(stem)
    if not stem:
        stem = "download"
    return f"{stem}.{ext}"


def media_type_for(ext: str) -> str:
    return _MEDIA_TYPES[ext]


def content_disposition(filename: str) -> str:
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    ascii_stem = ascii_stem.replace('"', "").replace("\\", "")
    ascii_stem = _BAD_CHARS.sub(" ", ascii_stem)
    ascii_stem = _tidy(ascii_stem) or "download"
    ascii_name = f"{ascii_stem}.{ext}" if ext else ascii_stem
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _clip_time(seconds: float) -> str:
    """0m12s, 1m40.5s, 1h02m05s: tenths only when there are any. Never contains ':'."""
    tenths = round(max(0.0, seconds) * 10)
    whole, frac = divmod(tenths, 10)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{secs:02d}" + (f".{frac}" if frac else "") + "s"
    return f"{hours}h{minutes:02d}m{text}" if hours else f"{minutes}m{text}"


def clip_filename(title: str | None, start: float, end: float, ext: str) -> str:
    """`<title> (clip <start>-<end>).<ext>`, with the title cut short (not the range) so the
    stem stays within the usual length."""
    label = f" (clip {_clip_time(start)}-{_clip_time(end)})"
    stem = sanitize_filename(title, ext).removesuffix(f".{ext}")
    room = _MAX_STEM_BYTES - len(label.encode("utf-8"))
    stem = _tidy(stem.encode("utf-8")[:room].decode("utf-8", errors="ignore")) or "download"
    return f"{stem}{label}.{ext}"
