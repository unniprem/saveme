"""Worker subprocess (`python -m app.worker`): every media network request
happens here, behind the connect-time guard (spec §4.3).

Protocol: the parent writes one JSON request to stdin and closes it. The worker
writes only JSON lines to stdout and ends with exactly one `result` event
(exit 0) or `error` event (exit 1). Modes: `inspect` (run_inspect), `download`
(run_download, which also emits `progress` and `phase` events), and for Clip Studio
`probe` (run_probe, an uploaded file), `clip` (run_clip) and `batch` (run_batch, several
clips in one ZIP). Only inspect and download take a URL; the others work on local files
and never touch the network.

yt-dlp (and app.ytdl, which imports it) is imported lazily inside the functions
that run after the guard is installed, never at module import time.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from urllib.parse import urlsplit

from app.cover import cover_opener, embed_cover, fetch_cover
from app.errors import BLOCKED_MARKER, AppError, classify_ytdlp_error, too_large_message
from app.filenames import is_plain_filename
from app.mediafile import MAX_BATCH_CLIPS, cut, cut_batch, prepare_upload, probe, remux_to_mp4
from app.netguard import BlockedAddressError, check_url_syntax, install_connect_guard

if TYPE_CHECKING:
    from yt_dlp import YoutubeDL

# url / url_transparent hops resolve() follows before giving up.
_MAX_HOPS = 5
# At most 4 progress events per second (the first and last of each part always go out).
_EMIT_INTERVAL = 0.25
_MEDIA_NAME = re.compile(r"media\.[A-Za-z0-9]+")
# Largest decoded body read whole (pages, manifests, API responses). Media is read in
# blocks and isn't affected. Large enough for real pages (YouTube's are a few MB).
MAX_PAGE_BYTES = 16 * 1024 * 1024
_READ_BLOCK = 1024 * 1024
# Data-segment cap for the worker and everything it starts, which inherit it (deno,
# ffmpeg). Measured on a YouTube job (10 Nov 2026): deno peaks near 570 MiB, the worker
# near 85 MiB, ffmpeg near 2 MiB.
WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
_MISSING_TRACK = {"video": "no_video", "audio": "no_audio", "m4a": "no_audio"}
# Containers a clip can be cut from and delivered in (a source is always one of these).
_CLIP_EXTS = frozenset({"mp4", "mkv", "webm"})


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


def limit_memory(limit: int = WORKER_MEMORY_BYTES) -> None:
    """Cap this process's data segment (heap and other private writable memory), soft
    and hard, so a hostile link can exhaust only its own worker, not the memory every
    worker and the API share. Children inherit the cap. Linux (the Docker image)
    enforces RLIMIT_DATA on every such allocation; macOS refuses to set it, so a native
    macOS run goes without and says so on stderr."""
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    try:
        resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    except (ValueError, OSError) as exc:
        print(f"worker: memory limit not applied: {exc}", file=sys.stderr)


def _cap_whole_reads(response, limit: int):
    """Make `response.read()` with no size stop at `limit` decoded bytes, raising
    source_too_large. yt-dlp reads pages this way (after a small first read), with no
    bound of its own; sized reads (media downloads) pass straight through."""
    read = response.read

    def capped_read(amt: int | None = None) -> bytes:
        if amt is not None and amt >= 0:
            return read(amt)
        chunks, total = [], 0
        while chunk := read(_READ_BLOCK):
            total += len(chunk)
            if total > limit:
                response.close()
                raise AppError("source_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    response.read = capped_read
    return response


def _youtube_dl(params: dict) -> YoutubeDL:
    """A YoutubeDL whose every response (extractors and downloaders alike go through
    urlopen) has its whole-body reads capped at MAX_PAGE_BYTES."""
    from yt_dlp import YoutubeDL

    class CappedYoutubeDL(YoutubeDL):
        def urlopen(self, req):
            return _cap_whole_reads(super().urlopen(req), MAX_PAGE_BYTES)

    return CappedYoutubeDL(params)


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
    from app.ytdl import base_opts, inspect_payload

    with _youtube_dl(_ydl_params(base_opts(None))) as ydl:
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
        done = d.get("downloaded_bytes")
        if done is None and status == "finished":  # a track kept from a cancelled run
            done = d.get("total_bytes")
        done = int(done or 0)
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
        elif key == "Metadata":
            self.phase("Adding tags")
        else:
            self.phase({"audio": "Converting to MP3", "m4a": "Preparing M4A"}.get(self._output, "Converting to MP4"))

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


def _lacks_audio(workdir: Path) -> bool:
    """True if a completely downloaded media file in `workdir` probes with no audio stream."""
    probed = [probe(p) for p in workdir.iterdir() if _MEDIA_NAME.fullmatch(p.name)]
    known = [found.kinds for found in probed if found is not None]
    return bool(known) and not any("audio" in kinds for kinds in known)


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
    from yt_dlp.utils import DownloadError, ExtractorError

    from app.runner import _replace_lone_surrogates
    from app.ytdl import (
        base_opts, chosen_formats, downgrade_notice, inspect_payload, output_opts, track_available,
        with_capped_description, without_source_link,
    )

    progress, phases, logger = _Progress(emit, max_bytes), _Phases(emit, output), _DownloadLogger()
    params = _ydl_params(base_opts(workdir), output_opts(output, quality),
                         {"max_filesize": max_bytes, "progress_hooks": [progress],
                          "postprocessor_hooks": [phases]})
    params["logger"] = logger
    try:
        with _youtube_dl(params) as ydl:
            ydl.add_post_processor(_parts_recorder(progress), when="before_dl")
            info, playlist_notice = resolve(ydl, url)
            # Tag values travel as ffmpeg arguments, which can't hold lone surrogates.
            info = _replace_lone_surrogates(with_capped_description(without_source_link(info)))
            # Same availability rule as inspect: an unknown codec may hold the track.
            if not track_available(inspect_payload(info), output):
                raise AppError(_MISSING_TRACK[output])
            processed = ydl.process_ie_result(info, download=True)
            cover = fetch_cover(cover_opener(ydl.urlopen), processed)
    except _TooLarge:
        raise AppError("too_large", too_large_message(max_bytes)) from None
    except (DownloadError, ExtractorError) as exc:
        if BLOCKED_MARKER in str(exc).lower():
            raise  # a guard block anywhere in the text wins (classified as blocked_address)
        if _is_missing_format(exc):
            raise AppError(_MISSING_TRACK[output]) from exc
        if output in ("audio", "m4a") and _lacks_audio(workdir):
            raise AppError("no_audio") from exc
        raise
    if logger.oversize:
        raise AppError("too_large", too_large_message(max_bytes))
    final = _final_file(processed, workdir)
    notices = [playlist_notice]
    if output == "video":
        found = probe(final)
        if found is not None and "video" not in found.kinds:
            raise AppError("no_video")
        notices.append(downgrade_notice(chosen_formats(processed), quality))
        if final.suffix != ".mp4":
            phases.phase("Converting to MP4")
            final, notice = remux_to_mp4(final)
            notices.append(notice)
    if cover:
        embed_cover(final, *cover)
    found = probe(final)
    title = processed.get("title")
    return {"file": final.name, "ext": final.suffix[1:], "title": title if isinstance(title, str) else None,
            "duration": None if found is None else found.duration,
            "notice": " ".join(n for n in notices if n) or None}


def run_download(req: dict, emit: Callable[[dict], None]) -> dict:
    """Download req["url"] into req["workdir"] as media.<ext> (spec §5.3–5.5),
    emitting progress/phase events. Returns {"file", "ext", "title", "duration", "notice"} with
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


def _local_file(req: dict, key: str) -> Path:
    """req[key] as a regular, non-symlink file with an absolute path, else internal."""
    value = req.get(key)
    if (not isinstance(value, str) or not os.path.isabs(value) or os.path.islink(value)
            or not os.path.isfile(value)):
        print(f"worker: malformed request ({key})", file=sys.stderr)
        raise AppError("internal")
    return Path(value)


def _workdir(req: dict) -> Path:
    value = req.get("workdir")
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isdir(value):
        print("worker: malformed request (workdir)", file=sys.stderr)
        raise AppError("internal")
    return Path(value)


def run_probe(req: dict, emit: Callable[[dict], None]) -> dict:
    """Check the upload req["source"], which must sit directly in req["workdir"], and
    leave only workdir/media.<ext> (spec §6.5). On failure the workdir is left empty."""
    workdir, source = _workdir(req), _local_file(req, "source")
    if os.path.realpath(source.parent) != os.path.realpath(workdir):
        print("worker: probe source is outside the workdir", file=sys.stderr)
        raise AppError("internal")
    try:
        result = prepare_upload(source, workdir)
    except BaseException:
        _clear(workdir)
        raise
    _clear(workdir, keep=result["file"])
    return result


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _clip_args(req: dict) -> tuple[Path, Path, float, float, bool, int]:
    workdir, source = _workdir(req), _local_file(req, "source")
    start, end = _seconds(req.get("start")), _seconds(req.get("end"))
    keep_tags, max_bytes = req.get("keep_tags"), req.get("max_bytes")
    ext = source.suffix[1:]
    if (not _MEDIA_NAME.fullmatch(source.name) or ext not in _CLIP_EXTS
            or os.path.realpath(source.parent) == os.path.realpath(workdir)
            or start is None or end is None or not 0 <= start < end
            or type(keep_tags) is not bool or type(max_bytes) is not int or max_bytes <= 0):
        print("worker: malformed clip request", file=sys.stderr)
        raise AppError("internal")
    return source, workdir / f"media.{ext}", start, end, keep_tags, max_bytes


def run_clip(req: dict, emit: Callable[[dict], None]) -> dict:
    """Cut req["start"]..req["end"] of the ready source req["source"] (another job's
    media.<ext>) into req["workdir"] as media.<same ext>, emitting progress events
    (spec §6.5). On failure the workdir is left empty."""
    source, dst, start, end, keep_tags, max_bytes = _clip_args(req)

    def progress(percent: float | None, written: int | None) -> None:
        emit({"event": "progress", "detail": "Cutting the clip",
              "percent": None if percent is None else round(percent, 1),
              "downloaded_bytes": written, "total_bytes": None})

    try:
        cut(source, dst, start=start, end=end, keep_tags=keep_tags, max_bytes=max_bytes,
            on_progress=progress)
    except BaseException:
        _clear(dst.parent)
        raise
    _clear(dst.parent, keep=dst.name)
    return {"file": dst.name, "ext": dst.suffix[1:]}


def _batch_args(req: dict) -> tuple[Path, Path, list[tuple[float, float, str]], bool, int]:
    workdir, source = _workdir(req), _local_file(req, "source")
    clips, keep_tags, max_bytes = req.get("clips"), req.get("keep_tags"), req.get("max_bytes")
    ext = source.suffix[1:]
    if (not _MEDIA_NAME.fullmatch(source.name) or ext not in _CLIP_EXTS
            or os.path.realpath(source.parent) == os.path.realpath(workdir)
            or not isinstance(clips, list) or not 1 <= len(clips) <= MAX_BATCH_CLIPS
            or type(keep_tags) is not bool or type(max_bytes) is not int or max_bytes <= 0):
        print("worker: malformed batch request", file=sys.stderr)
        raise AppError("internal")
    ranges = []
    for clip in clips:
        start = _seconds(clip.get("start")) if isinstance(clip, dict) else None
        end = _seconds(clip.get("end")) if isinstance(clip, dict) else None
        name = clip.get("name") if isinstance(clip, dict) else None
        if start is None or end is None or not 0 <= start < end or not is_plain_filename(name, ext):
            print("worker: malformed batch request (clips)", file=sys.stderr)
            raise AppError("internal")
        ranges.append((start, end, name))
    if len({name.casefold() for _s, _e, name in ranges}) != len(ranges):
        print("worker: malformed batch request (repeated names)", file=sys.stderr)
        raise AppError("internal")
    return source, workdir, ranges, keep_tags, max_bytes


def run_batch(req: dict, emit: Callable[[dict], None]) -> dict:
    """Cut each of req["clips"] ({"start", "end", "name"}) out of the ready source
    req["source"] and store them in req["workdir"] as media.zip, emitting progress events
    over the whole batch. On failure the workdir is left empty."""
    source, workdir, clips, keep_tags, max_bytes = _batch_args(req)
    count = len(clips)

    def progress(number: int, percent: float | None, written: int | None) -> None:
        emit({"event": "progress", "detail": f"Cutting clip {number} of {count}",
              "percent": None if percent is None else round(percent, 1),
              "downloaded_bytes": written, "total_bytes": None})

    try:
        archive = cut_batch(source, workdir, clips, keep_tags=keep_tags, max_bytes=max_bytes,
                            on_progress=progress)
    except BaseException:
        _clear(workdir)
        raise
    _clear(workdir, keep=archive.name)
    return {"file": archive.name, "ext": "zip", "count": count}


def _read_request(stdin: TextIO) -> dict:
    try:
        req = json.loads(stdin.read())
    except ValueError:
        req = None
    if not isinstance(req, dict) or not isinstance(req.get("mode"), str):
        print("worker: malformed request", file=sys.stderr)
        raise AppError("internal")
    return req


def _url(req: dict) -> str:
    url = req.get("url")
    if not isinstance(url, str):
        print("worker: malformed request (url)", file=sys.stderr)
        raise AppError("internal")
    return check_url_syntax(url)


def _to_app_error(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        err = exc
    elif isinstance(exc, MemoryError):  # the worker hit limit_memory's cap
        err = AppError("source_too_large")
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
        mode = req["mode"]
        if mode == "inspect":
            data = run_inspect(_url(req))
        elif mode == "download":
            data = run_download({**req, "url": _url(req)}, emit)
        elif mode == "probe":
            data = run_probe(req, emit)
        elif mode == "clip":
            data = run_clip(req, emit)
        elif mode == "batch":
            data = run_batch(req, emit)
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
    limit_memory()
    install_connect_guard()
    with protocol_out() as out:
        code = serve(sys.stdin, out)
    sys.exit(code)


if __name__ == "__main__":
    main()
