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


def _title_stem(title: str | None) -> str:
    """A title as a safe file name stem of at most _MAX_STEM_BYTES, else "download"."""
    stem = unicodedata.normalize("NFKC", title or "")
    stem = _BAD_CHARS.sub(" ", stem)
    stem = _tidy(stem)
    if _RESERVED.match(stem):
        stem += "_"
    stem = stem.encode("utf-8")[:_MAX_STEM_BYTES].decode("utf-8", errors="ignore")
    return _tidy(stem) or "download"


def sanitize_filename(title: str | None, ext: str) -> str:
    if ext not in _MEDIA_TYPES:
        raise ValueError(f"unsupported extension: {ext!r}")
    return f"{_title_stem(title)}.{ext}"


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


def _labelled(title: str | None, label: str, ext: str) -> str:
    """`<title><label>.<ext>`, with the title cut short (not the label) so the stem stays
    within the usual length."""
    stem = _title_stem(title)
    room = _MAX_STEM_BYTES - len(label.encode("utf-8"))
    stem = _tidy(stem.encode("utf-8")[:room].decode("utf-8", errors="ignore")) or "download"
    return f"{stem}{label}.{ext}"


def clip_filename(title: str | None, start: float, end: float, ext: str) -> str:
    """`<title> (clip <start>-<end>).<ext>`."""
    if ext not in _MEDIA_TYPES:
        raise ValueError(f"unsupported extension: {ext!r}")
    return _labelled(title, f" (clip {_clip_time(start)}-{_clip_time(end)})", ext)


# A batch of clips is delivered as one ZIP. "zip" is deliberately not in _MEDIA_TYPES: those
# are what a download may be delivered as, and a download must never come out as a ZIP.
ZIP_MEDIA_TYPE = "application/zip"


def batch_entry_name(index: int, title: str | None, start: float, end: float, ext: str) -> str:
    """A clip's name inside a batch ZIP: `01 <title> (clip <start>-<end>).<ext>`, numbered so
    the names sort in list order and stay unique."""
    return f"{index:02d} {clip_filename(title, start, end, ext)}"


def batch_filename(title: str | None, count: int) -> str:
    """`<title> (<count> clips).zip`."""
    return _labelled(title, f" ({count} clip{'' if count == 1 else 's'})", "zip")


def is_plain_filename(name: object, ext: str) -> bool:
    """True for a name that is safe as a file name anywhere it is unpacked: no folder parts,
    control or Windows-forbidden characters, no leading dot, at most 255 UTF-8 bytes, and
    ending in .<ext>."""
    if not isinstance(name, str) or name.startswith(".") or not name.endswith(f".{ext}"):
        return False
    if _BAD_CHARS.search(name) or name != name.strip():
        return False
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return len(ext) + 1 < size <= 255


_SHORT_EXTS = {"mp4", "srt", "ass"}


def short_filename(title: str | None, number: int, start: float, end: float, ext: str, *,
                   numbered: bool = False) -> str:
    """An Auto Shorts file: `<title> (short <n>, <start>-<end>).<ext>`, or with `numbered` (inside a
    ZIP) `<nn> <title> (short …).<ext>` so the entries sort in order."""
    if ext not in _SHORT_EXTS:
        raise ValueError(f"unsupported extension: {ext!r}")
    name = _labelled(title, f" (short {number}, {_clip_time(start)}-{_clip_time(end)})", ext)
    return f"{number:02d} {name}" if numbered else name


def shorts_zip_name(title: str | None, count: int) -> str:
    """`<title> (<count> shorts).zip`."""
    return _labelled(title, f" ({count} short{'' if count == 1 else 's'})", "zip")
