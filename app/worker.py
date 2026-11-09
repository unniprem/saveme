"""Worker subprocess (`python -m app.worker`): every media network request
happens here, behind the connect-time guard (spec §4.3).

Protocol: the parent writes one JSON request to stdin and closes it. The worker
writes only JSON lines to stdout and ends with exactly one `result` event
(exit 0) or `error` event (exit 1). Modes: `inspect` (run_inspect) and
`download` (run_download, which also emits `progress` and `phase` events).

yt-dlp (and app.ytdl, which imports it) is imported lazily inside the functions
that run after the guard is installed, never at module import time.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from urllib.parse import urlsplit

from app.errors import BLOCKED_MARKER, AppError, classify_ytdlp_error, too_large_message
from app.filenames import media_type_for
from app.netguard import BlockedAddressError, check_url_syntax, install_connect_guard

if TYPE_CHECKING:
    from yt_dlp import YoutubeDL

# url / url_transparent hops resolve() follows before giving up.
_MAX_HOPS = 5
# At most 4 progress events per second (the first and last of each part always go out).
_EMIT_INTERVAL = 0.25
_PROBE_TIMEOUT = 60
_MEDIA_NAME = re.compile(r"media\.[A-Za-z0-9]+")
_MISSING_TRACK = {"video": "no_video", "audio": "no_audio"}


class _StderrLogger:
    """yt-dlp logger: every message goes to stderr, never to the protocol."""

    def debug(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    info = warning = error = debug


class _DownloadLogger(_StderrLogger):
    """Also notes yt-dlp's max_filesize skip: with a Content-Length over the limit
    it only logs "File is larger than max-filesize" and returns without a file."""

    oversize = False

    def debug(self, msg: str) -> None:
        if msg.lstrip("\r").startswith("[download] File is larger than max-filesize"):
            self.oversize = True
        super().debug(msg)

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


class _TooLarge(Exception):
    """Raised from the progress hook, inside yt-dlp, to abort a download over max_bytes."""


class _Progress:
    """yt-dlp progress hook: throttled `progress` events per part (track), and the
    size cap on the running byte total across parts (spec §5.5, §6.1)."""

    def __init__(self, emit: Callable[[dict], None], max_bytes: int,
                 clock: Callable[[], float] = time.monotonic):
        self._emit, self._max_bytes, self._clock = emit, max_bytes, clock
        self.parts: list[dict] = []  # info["requested_formats"], recorded just before download
        self._done: dict[str, int] = {}  # part file -> bytes downloaded so far
        self._exact: dict[str, tuple[int | None, bool]] = {}  # part file -> (exact size, percent known)
        self._last = float("-inf")

    def _detail(self, info: dict) -> str:
        n = len(self.parts)
        for i, fmt in enumerate(self.parts, 1):
            if n > 1 and fmt.get("format_id") == info.get("format_id"):
                track = "audio" if fmt.get("vcodec") == "none" else "video"
                return f"Downloading {track} track ({i} of {n})"
        return "Downloading"

    def __call__(self, d: dict) -> None:
        status = d.get("status")
        if status not in ("downloading", "finished"):
            return
        part = str(d.get("filename"))
        done = int(d.get("downloaded_bytes") or 0)
        self._done[part] = done
        if sum(self._done.values()) > self._max_bytes:
            raise _TooLarge
        first = part not in self._exact
        if status == "downloading":
            # Percent only from an exact total or fragment counts, never total_bytes_estimate.
            total, count, index = d.get("total_bytes"), d.get("fragment_count"), d.get("fragment_index")
            total = int(total) if isinstance(total, (int, float)) and total > 0 else None
            if total:
                percent = done * 100 / total
            elif count and index is not None:
                percent = index * 100 / count
            else:
                percent = None
            self._exact[part] = (total, percent is not None)
        else:
            # "finished" reports the byte count as total_bytes even for chunked
            # transfers, so it is exact only if it was known while downloading.
            total, known = self._exact.get(part, (None, False))
            percent = 100.0 if known else None
        now = self._clock()
        if not (first or status == "finished" or now - self._last >= _EMIT_INTERVAL):
            return
        self._last = now
        self._emit({"event": "progress", "detail": self._detail(d.get("info_dict") or {}),
                    "downloaded_bytes": done, "total_bytes": total,
                    "percent": None if percent is None else round(min(percent, 100.0), 1)})


def _ffmpeg_pp_keys() -> frozenset[str]:
    """pp_key() of every FFmpeg* postprocessor (pp_key drops the "FFmpeg" prefix)."""
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    keys, todo = set(), [FFmpegPostProcessor]
    while todo:
        cls = todo.pop()
        keys.add(cls.pp_key())
        todo += cls.__subclasses__()
    return frozenset(keys)


class _Phases:
    """yt-dlp postprocessor hook: a `converting` phase event when an FFmpeg*
    postprocessor starts. yt-dlp may call it twice per start, so repeats are dropped."""

    def __init__(self, emit: Callable[[dict], None], output: str):
        self._emit, self._output = emit, output
        self._ffmpeg = _ffmpeg_pp_keys()
        self._last: str | None = None

    def __call__(self, d: dict) -> None:
        key = d.get("postprocessor")
        if d.get("status") != "started" or key not in self._ffmpeg:
            return
        if key == "Merger":
            self.phase("Merging video and audio")
        else:
            self.phase("Converting to MP3" if self._output == "audio" else "Converting to MP4")

    def phase(self, detail: str) -> None:
        if detail != self._last:
            self._last = detail
            self._emit({"event": "phase", "phase": "converting", "detail": detail})


def _parts_recorder(progress: _Progress):
    """A postprocessor run just before download that hands the selected formats to
    the progress hook (each part's own info_dict no longer lists them)."""
    from yt_dlp.postprocessor.common import PostProcessor

    class RecordPartsPP(PostProcessor):
        def run(self, info):
            progress.parts = list(info.get("requested_formats") or [])
            return [], info

    return RecordPartsPP()


def _stderr_tail(data: bytes) -> str:
    return data.decode(errors="replace")[-2000:]


def _stream_kinds(path: Path) -> set[str] | None:
    """Stream types in a local file (cover art doesn't count as video), probed
    through the ffprobe wrapper; None when ffprobe can't read the file."""
    from app.ytdl import FFMPEG_WRAPPER_DIR

    cmd = [str(FFMPEG_WRAPPER_DIR / "ffprobe"), "-v", "error", "-show_entries",
           "stream=codec_type:stream_disposition=attached_pic", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=_PROBE_TIMEOUT)
        streams = json.loads(proc.stdout)["streams"] if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        print(f"worker: ffprobe failed: {exc!r}", file=sys.stderr)
        return None
    if not isinstance(streams, list):
        print(f"worker: ffprobe failed: {_stderr_tail(proc.stderr)}", file=sys.stderr)
        return None
    return {s.get("codec_type") for s in streams
            if isinstance(s, dict) and not (s.get("disposition") or {}).get("attached_pic")}


def _lacks_audio(workdir: Path) -> bool:
    """True if a completely downloaded media file in `workdir` probes with no audio stream."""
    probed = [_stream_kinds(p) for p in workdir.iterdir() if _MEDIA_NAME.fullmatch(p.name)]
    known = [kinds for kinds in probed if kinds is not None]
    return bool(known) and not any("audio" in kinds for kinds in known)


def _to_mp4(src: Path) -> tuple[Path, str | None]:
    """Remux `src` to media.mp4 next to it (stream copy). On failure keep `src` and
    return a notice, provided the server can deliver its container at all."""
    from app.ytdl import FFMPEG_WRAPPER_DIR

    dst = src.with_name("media.mp4")
    cmd = [str(FFMPEG_WRAPPER_DIR / "ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", str(src), "-c", "copy", "-sn", "-dn", str(dst)]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)
        ok = proc.returncode == 0 and dst.is_file()
        if not ok:
            print(f"worker: remux to MP4 failed: {_stderr_tail(proc.stderr)}", file=sys.stderr)
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


def _final_file(processed: dict, workdir: Path) -> Path:
    """The file yt-dlp reports as the finished download: workdir/media.<ext>."""
    downloads = processed.get("requested_downloads") or [{}]
    path = downloads[-1].get("filepath")
    final = Path(path) if isinstance(path, str) else None
    if (final is None or not _MEDIA_NAME.fullmatch(final.name)
            or final.resolve().parent != workdir.resolve() or not final.is_file()):
        print(f"worker: no finished media file (filepath={path!r})", file=sys.stderr)
        raise AppError("extraction_failed")
    return final


def _clear(workdir: Path, keep: str | None = None) -> None:
    """Delete everything in `workdir` except the file named `keep`."""
    try:
        entries = list(os.scandir(workdir))
    except OSError:
        return
    for entry in entries:
        if entry.name == keep:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError as exc:
            print(f"worker: cleanup failed: {exc!r}", file=sys.stderr)


def _is_missing_format(exc: BaseException) -> bool:
    """yt-dlp's own "Requested format is not available" (format selection found
    nothing), judged by the ExtractorError's original message, also when a
    DownloadError wraps it; never by str(exc), which echoes URL and id text."""
    from yt_dlp.utils import ExtractorError

    wrapped = getattr(exc, "exc_info", None)
    for err in (exc, wrapped[1] if isinstance(wrapped, tuple) and len(wrapped) > 1 else None):
        if isinstance(err, ExtractorError) and err.orig_msg.startswith("Requested format is not available"):
            return True
    return False


def _download_args(req: dict) -> tuple[str, str, Path, int]:
    from typing import get_args

    from app.schemas import Output, Quality

    output, quality = req.get("output"), req.get("quality")
    workdir, max_bytes = req.get("workdir"), req.get("max_bytes")
    if (output not in get_args(Output) or quality not in get_args(Quality)
            or not isinstance(workdir, str) or not os.path.isabs(workdir) or not os.path.isdir(workdir)
            or type(max_bytes) is not int or max_bytes <= 0):
        print("worker: malformed download request", file=sys.stderr)
        raise AppError("internal")
    return output, quality, Path(workdir), max_bytes


def _download(url: str, output: str, quality: str, workdir: Path, max_bytes: int,
              emit: Callable[[dict], None]) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, ExtractorError

    from app.ytdl import audio_opts, base_opts, chosen_formats, downgrade_notice, inspect_payload, video_opts

    progress, phases, logger = _Progress(emit, max_bytes), _Phases(emit, output), _DownloadLogger()
    params = _ydl_params(base_opts(workdir), video_opts(quality) if output == "video" else audio_opts(),
                         {"max_filesize": max_bytes, "progress_hooks": [progress],
                          "postprocessor_hooks": [phases]})
    params["logger"] = logger
    try:
        with YoutubeDL(params) as ydl:
            ydl.add_post_processor(_parts_recorder(progress), when="before_dl")
            info, playlist_notice = resolve(ydl, url)
            # Same availability rule as inspect: an unknown codec may hold the track.
            if not inspect_payload(info)[output]["available"]:
                raise AppError(_MISSING_TRACK[output])
            processed = ydl.process_ie_result(info, download=True)
    except _TooLarge:
        raise AppError("too_large", too_large_message(max_bytes)) from None
    except (DownloadError, ExtractorError) as exc:
        if BLOCKED_MARKER in str(exc).lower():
            raise  # a guard block anywhere in the text wins (classified as blocked_address)
        if _is_missing_format(exc):
            raise AppError(_MISSING_TRACK[output]) from exc
        if output == "audio" and _lacks_audio(workdir):
            raise AppError("no_audio") from exc
        raise
    if logger.oversize:
        raise AppError("too_large", too_large_message(max_bytes))
    final = _final_file(processed, workdir)
    notices = [playlist_notice]
    if output == "video":
        kinds = _stream_kinds(final)
        if kinds is not None and "video" not in kinds:
            raise AppError("no_video")
        notices.append(downgrade_notice(chosen_formats(processed), quality))
        if final.suffix != ".mp4":
            phases.phase("Converting to MP4")
            final, notice = _to_mp4(final)
            notices.append(notice)
    title = processed.get("title")
    return {"file": final.name, "ext": final.suffix[1:], "title": title if isinstance(title, str) else None,
            "notice": " ".join(n for n in notices if n) or None}


def run_download(req: dict, emit: Callable[[dict], None]) -> dict:
    """Download req["url"] into req["workdir"] as media.<ext> (spec §5.3–5.5),
    emitting progress/phase events. Returns {"file", "ext", "title", "notice"} with
    `file` relative to the workdir, which then holds only that file; on failure
    the workdir is left empty."""
    output, quality, workdir, max_bytes = _download_args(req)
    try:
        result = _download(req["url"], output, quality, workdir, max_bytes, emit)
    except BaseException:
        _clear(workdir)
        raise
    _clear(workdir, keep=result["file"])
    return result


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


def _writer(out: TextIO) -> Callable[[dict], None]:
    def emit(event: dict) -> None:
        out.write(json.dumps(event) + "\n")
        out.flush()

    return emit


def serve(stdin: TextIO, out: TextIO) -> int:
    """Handle one request from `stdin`, writing protocol events to `out`.
    Returns the exit code: 0 after a result, 1 after an error."""
    emit = _writer(out)
    try:
        req = _read_request(stdin)
        url = check_url_syntax(req["url"])
        mode = req.get("mode")
        if mode == "inspect":
            data = run_inspect(url)
        elif mode == "download":
            data = run_download({**req, "url": url}, emit)
        else:
            print(f"worker: unknown mode {mode!r}", file=sys.stderr)
            raise AppError("internal")
        event, code = {"event": "result", "data": data}, 0
    except Exception as exc:
        err = _to_app_error(exc)
        event, code = {"event": "error", "code": err.code, "message": err.message}, 1
    emit(event)
    return code


def main() -> None:
    install_connect_guard()
    with protocol_out() as out:
        code = serve(sys.stdin, out)
    sys.exit(code)


if __name__ == "__main__":
    main()
