"""Cover art: fetch the source thumbnail in memory and embed it with mutagen.

FFmpeg never sees an image. The fetch goes through the worker's guarded, size-capped
`ydl.urlopen`; only JPEG and PNG (by magic bytes) are accepted, at most COVER_MAX_BYTES
each and MAX_COVER_TRIES URLs, all within COVER_BUDGET_SECONDS. Any problem means no
cover, never a failed job.
"""
from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

COVER_MAX_BYTES = 5 * 1024 * 1024
MAX_COVER_TRIES = 3
# The whole fetch, every try included. A host that sends a few bytes at a time never
# trips a socket timeout, so after this long the file goes out without a cover.
COVER_BUDGET_SECONDS = 15
# Socket timeout (connect, and each read) of one cover request, in place of the
# download's socket_timeout.
COVER_REQUEST_TIMEOUT = 5
_READ_BLOCK = 64 * 1024
_SUFFIXES = (".jpg", ".jpeg", ".png")
_MIME = {"jpeg": "image/jpeg", "png": "image/png"}


def _qualifying(candidates: list) -> list[str]:
    from yt_dlp.utils import sanitize_url

    urls: list[str] = []
    for candidate in candidates:
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
    return urls


def cover_urls(info: dict) -> list[str]:
    """Candidate image URLs, best first, at most MAX_COVER_TRIES: http(s) URLs whose path
    ends in .jpg/.jpeg/.png. yt-dlp guesses some thumbnail names that 404, so the
    extractor's own info["thumbnail"], when it qualifies, is always among the tries: if
    it isn't already in the top candidates it replaces the last of them."""
    thumbnails = info.get("thumbnails")
    raw = [t.get("url") for t in thumbnails if isinstance(t, dict)] if isinstance(thumbnails, list) else []
    candidates = _qualifying(raw[::-1])  # yt-dlp sorts worst to best
    own = _qualifying([info.get("thumbnail")])
    if not candidates:
        return own
    top = candidates[:MAX_COVER_TRIES]
    if own and own[0] not in top:
        top = top[:MAX_COVER_TRIES - 1] + own
    return top


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


def cover_opener(urlopen: Callable[[Any], Any]) -> Callable[[str], Any]:
    """`urlopen` (the worker's ydl.urlopen) for one cover URL: a yt-dlp Request whose
    `timeout` extension gives it COVER_REQUEST_TIMEOUT."""
    from yt_dlp.networking import Request

    return lambda url: urlopen(Request(url, extensions={"timeout": COVER_REQUEST_TIMEOUT}))


def _first_image(urlopen: Callable[[str], Any], urls: list[str], deadline: float,
                 found: list[tuple[bytes, str]], notes: list[str]) -> None:
    """Try `urls` in order and put the first JPEG or PNG in `found`. It runs on
    fetch_cover's thread, which may be abandoned, so it never raises, prints nothing
    (log lines go to `notes`) and starts no request after `deadline`."""
    for url in urls:
        if time.monotonic() >= deadline:
            return
        response = None
        try:
            response = urlopen(url)
            data = _read_capped(response)
            kind = image_kind(data) if data is not None else None
            if kind:
                found.append((data, kind))
                return
            notes.append("thumbnail skipped (too large or not JPEG/PNG)")
        except Exception as exc:
            notes.append(f"thumbnail fetch failed: {type(exc).__name__}")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


def fetch_cover(urlopen: Callable[[str], Any], info: dict) -> tuple[bytes, str] | None:
    """The first acceptable (image bytes, kind) among cover_urls(info), else None. Never
    raises, and returns within COVER_BUDGET_SECONDS: the tries run on a daemon thread,
    abandoned when the budget runs out. That thread keeps the image in memory (about
    COVER_MAX_BYTES at most), starts no new request, prints nothing and dies with the
    worker."""
    try:
        urls = cover_urls(info)
    except Exception as exc:
        print(f"worker: cover: no usable thumbnail list: {exc!r}", file=sys.stderr)
        return None
    if not urls:
        return None
    budget = COVER_BUDGET_SECONDS
    found: list[tuple[bytes, str]] = []
    notes: list[str] = []
    thread = threading.Thread(target=_first_image, name="cover", daemon=True,
                              args=(urlopen, urls, time.monotonic() + budget, found, notes))
    thread.start()
    thread.join(budget)
    for note in list(notes):
        print(f"worker: cover: {note}", file=sys.stderr)
    if found:
        return found[0]
    if thread.is_alive():
        print(f"worker: cover: gave up after {budget:g} s", file=sys.stderr)
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
