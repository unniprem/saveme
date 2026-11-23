"""Cover art: fetch the source thumbnail in memory and embed it with mutagen.

FFmpeg never sees an image. The fetch goes through the worker's guarded, size-capped
`ydl.urlopen`; only JPEG and PNG (by magic bytes) are accepted, at most COVER_MAX_BYTES
each and MAX_COVER_TRIES URLs. Any problem means no cover, never a failed job.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

COVER_MAX_BYTES = 5 * 1024 * 1024
MAX_COVER_TRIES = 3
_READ_BLOCK = 64 * 1024
_SUFFIXES = (".jpg", ".jpeg", ".png")
_MIME = {"jpeg": "image/jpeg", "png": "image/png"}


def cover_urls(info: dict) -> list[str]:
    """Candidate image URLs, best first: http(s) URLs whose path ends in .jpg/.jpeg/.png."""
    from yt_dlp.utils import sanitize_url

    thumbnails = info.get("thumbnails")
    raw = [t.get("url") for t in thumbnails if isinstance(t, dict)] if isinstance(thumbnails, list) else []
    raw = raw[::-1]  # yt-dlp sorts worst to best
    if not raw:
        raw = [info.get("thumbnail")]
    urls: list[str] = []
    for candidate in raw:
        if not isinstance(candidate, str):
            continue
        url = sanitize_url(candidate, scheme="https")
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if (parts.scheme in ("http", "https") and parts.path.lower().endswith(_SUFFIXES)
                and url not in urls):
            urls.append(url)
    return urls[:MAX_COVER_TRIES]


def image_kind(data: bytes) -> Literal["jpeg", "png"] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG"):
        return "png"
    return None


def _read_capped(response: Any) -> bytes | None:
    """The whole body, or None once it passes COVER_MAX_BYTES."""
    chunks, size = [], 0
    while block := response.read(_READ_BLOCK):
        size += len(block)
        if size > COVER_MAX_BYTES:
            return None
        chunks.append(block)
    return b"".join(chunks)


def fetch_cover(urlopen: Callable[[str], Any], info: dict) -> tuple[bytes, str] | None:
    """The first acceptable (image bytes, kind) among cover_urls(info), else None. Never raises."""
    try:
        urls = cover_urls(info)
    except Exception as exc:
        print(f"worker: cover: no usable thumbnail list: {exc!r}", file=sys.stderr)
        return None
    for url in urls:
        response = None
        try:
            response = urlopen(url)
            data = _read_capped(response)
            kind = image_kind(data) if data is not None else None
            if kind:
                return data, kind
            print("worker: cover: thumbnail skipped (too large or not JPEG/PNG)", file=sys.stderr)
        except Exception as exc:
            print(f"worker: cover: thumbnail fetch failed: {type(exc).__name__}", file=sys.stderr)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
    return None


def embed_cover(path: Path, data: bytes, kind: str) -> bool:
    """Embed `data` as the cover of an .mp3, .mp4 or .m4a. False (file untouched) for any
    other extension or on any error."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".mp3":
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError

            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=_MIME[kind], type=3, desc="Cover", data=data))
            tags.save(path)
        elif suffix in (".mp4", ".m4a"):
            from mutagen.mp4 import MP4, MP4Cover

            fmt = MP4Cover.FORMAT_JPEG if kind == "jpeg" else MP4Cover.FORMAT_PNG
            mp4 = MP4(path)
            mp4["covr"] = [MP4Cover(data, imageformat=fmt)]
            mp4.save()
        else:
            return False
        return True
    except Exception as exc:
        print(f"worker: cover: embedding failed: {type(exc).__name__}", file=sys.stderr)
        return False
