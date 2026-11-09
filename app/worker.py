"""Worker subprocess (`python -m app.worker`): every media network request
happens here, behind the connect-time guard (spec §4.3).

Protocol: the parent writes one JSON request to stdin and closes it. The worker
writes only JSON lines to stdout and ends with exactly one `result` event
(exit 0) or `error` event (exit 1).

yt-dlp (and app.ytdl, which imports it) is imported lazily inside the functions
that run after the guard is installed, never at module import time.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import traceback
from typing import TYPE_CHECKING, TextIO
from urllib.parse import urlsplit

from app.errors import AppError, classify_ytdlp_error
from app.netguard import BlockedAddressError, check_url_syntax, install_connect_guard

if TYPE_CHECKING:
    from yt_dlp import YoutubeDL

# url / url_transparent hops resolve() follows before giving up.
_MAX_HOPS = 5


class _StderrLogger:
    """yt-dlp logger: every message goes to stderr, never to the protocol."""

    def debug(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    info = warning = error = debug


def _ydl_params(*parts: dict) -> dict:
    params: dict = {}
    for part in parts:
        params.update(part)
    params["logger"] = _StderrLogger()
    return params


def protocol_out() -> TextIO:
    """Move the protocol off fd 1 and return it as a private stream.

    The returned stream writes to a dup of the original fd 1, which is
    non-inheritable (PEP 446), so no child (ffmpeg, ffprobe, deno) holds the
    parent's pipe open. fd 1 itself is pointed at stderr, so stray prints from
    this process or its children can never corrupt the protocol.
    """
    sys.stdout.flush()
    fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(fd, "w", encoding="utf-8")


def safe_thumbnail(url: str | None) -> str | None:
    """`url` if it is http(s) and its host resolves only to allowed addresses
    (through the guarded getaddrinfo), else None. The server never fetches it."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):  # BlockedAddressError is an OSError; bad IDNA a ValueError
        return None
    return url


def _overlay(outer: dict, inner: dict) -> dict:
    """yt-dlp's url_transparent merge: the embedding result's non-None fields win,
    except its own type/url/key and (unless it is a clip) its identity."""
    exempt = {"_type", "url", "ie_key"}
    if not outer.get("section_end") and outer.get("section_start") is None:
        exempt |= {"id", "extractor", "extractor_key"}
    merged = dict(inner)
    merged.update({k: v for k, v in outer.items() if v is not None and k not in exempt})
    if merged.get("_type") == "url":
        merged["_type"] = "url_transparent"
    return merged


def resolve(ydl: YoutubeDL, url: str) -> tuple[dict, str | None]:
    """The single extraction boundary (spec §10): extract `url` without
    processing, take the first entry of a playlist, follow url/url_transparent
    results, then keep only safe formats. Returns (safe info, playlist notice)."""
    from yt_dlp.utils import sanitize_url

    from app.ytdl import keep_safe_formats, unwrap_first_entry

    info = ydl.extract_info(url, download=False, process=False)
    notice: str | None = None
    hops = 0
    while True:
        if not isinstance(info, dict):
            raise AppError("extraction_failed")
        info, entry_notice = unwrap_first_entry(info)
        notice = notice or entry_notice
        kind = info.get("_type")
        if kind not in ("url", "url_transparent"):
            return keep_safe_formats(info), notice
        if hops == _MAX_HOPS:
            raise AppError("extraction_failed")
        hops += 1
        extra = {"original_url": info["original_url"]} if info.get("original_url") else {}
        inner = ydl.extract_info(sanitize_url(info["url"], scheme="https"), download=False,
                                 process=False, ie_key=info.get("ie_key"), extra_info=extra)
        if kind == "url_transparent" and isinstance(inner, dict):
            inner = _overlay(info, inner)
        info = inner


def run_inspect(url: str) -> dict:
    from yt_dlp import YoutubeDL

    from app.ytdl import base_opts, inspect_payload

    with YoutubeDL(_ydl_params(base_opts(None))) as ydl:
        info, notice = resolve(ydl, url)
    payload = inspect_payload(info)
    payload["thumbnail_url"] = safe_thumbnail(payload["thumbnail_url"])
    if notice:
        payload["notices"].insert(0, notice)
    return payload


def _read_request(stdin: TextIO) -> dict:
    try:
        req = json.loads(stdin.read())
    except ValueError:
        req = None
    if not isinstance(req, dict) or not isinstance(req.get("url"), str):
        print("worker: malformed request", file=sys.stderr)
        raise AppError("internal")
    return req


def _to_app_error(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        err = exc
    elif isinstance(exc, BlockedAddressError):
        err = AppError("blocked_address")
    else:
        from yt_dlp.utils import DownloadError, ExtractorError

        if isinstance(exc, (DownloadError, ExtractorError)):
            err = classify_ytdlp_error(str(exc))
        else:
            traceback.print_exception(exc, file=sys.stderr)
            return AppError("internal")
    print(f"worker: {err.code}: {exc}", file=sys.stderr)
    return err


def serve(stdin: TextIO, out: TextIO) -> int:
    """Handle one request from `stdin`, writing protocol events to `out`.
    Returns the exit code: 0 after a result, 1 after an error."""
    try:
        req = _read_request(stdin)
        url = check_url_syntax(req["url"])
        mode = req.get("mode")
        if mode == "inspect":
            data = run_inspect(url)
        else:
            print(f"worker: unknown mode {mode!r}", file=sys.stderr)
            raise AppError("internal")
        line, code = json.dumps({"event": "result", "data": data}), 0
    except Exception as exc:
        err = _to_app_error(exc)
        line, code = json.dumps({"event": "error", "code": err.code, "message": err.message}), 1
    out.write(line + "\n")
    out.flush()
    return code


def main() -> None:
    install_connect_guard()
    with protocol_out() as out:
        code = serve(sys.stdin, out)
    sys.exit(code)


if __name__ == "__main__":
    main()
