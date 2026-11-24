"""Transcriber subprocess (`python -m app.transcriber`): a ready source's speech as a transcript,
entirely on this machine.

Protocol (as app.worker): the parent writes one JSON request to stdin and closes it; the worker
writes only JSON lines to stdout and ends with exactly one `result` event (exit 0) or `error`
event (exit 1). The one mode, `transcribe`:

    {"mode": "transcribe", "source": "<abs path>/media.mp4", "workdir": "<abs dir>",
     "model": "base", "language": "auto" | "<whisper code>", "cpu_threads": 2,
     "duration": 123.4 | null, "max_seconds": 10800}

1. ffprobe the source (no audio stream: no_audio), check the length limit;
2. `phase` extracting: FFmpeg (through the app/bin wrapper) writes workdir/audio.wav, 16 kHz
   mono PCM, with `progress` events from FFmpeg's own clock;
3. `phase` transcribing: faster-whisper reads the WAV in windows of about WINDOW_SECONDS (each
   cut at the quietest moment of its last SEARCH_SECONDS, so words are rarely split), with
   `progress` from the end time of the last segment found;
4. workdir/transcript.json is written, audio.wav deleted; the result is
   {"file": "transcript.json", "segments", "language", "duration"}.

On failure the workdir is left empty. This process is separate from app.worker so that it can
have a memory cap sized for the model (app.worker's hard cap is 1 GiB and can't be raised), and
so that it can refuse every network connection: models are loaded from the local Hugging Face
cache only (`python -m app.whisper_models` installs them). It runs at a lower CPU priority than
downloads. Audio is decoded by the hardened FFmpeg, never by PyAV (faster-whisper's own decoder):
the model gets a NumPy array.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

from app.errors import AppError
from app.mediafile import WAV_BYTES_PER_SECOND, WAV_RATE, extract_audio, probe
from app.netguard import install_connect_guard
from app.transcript import (AUTO, UNKNOWN_LANGUAGE, WHISPER_LANGUAGES, WHISPER_MODELS, Segment,
                            Transcript, clean_text, dominant_language, is_language, save_transcript)
from app.worker import _clear, _local_file, _read_request, _workdir, _writer, limit_memory, protocol_out

WINDOW_SECONDS = 300.0
SEARCH_SECONDS = 20.0
MERGE_TAIL_SECONDS = 30.0  # a tail this short joins the window before it
TRANSCRIPT_NAME = "transcript.json"
AUDIO_NAME = "audio.wav"
MAX_CPU_THREADS = 16
_FRAME_SECONDS = 0.1
_EMIT_INTERVAL = 0.25
_GIB = 1024 ** 3
_NO_AUDIO = "This video has no audio track to transcribe. Import its subtitles instead."


def memory_limit(model: str) -> int:
    """Data-segment cap for a model: int8 weights plus 300 s windows, with room to spare
    (measured: `base` peaks near 0.75 GB RSS on 300 s windows)."""
    if model.startswith(("tiny", "base")):
        return 3 * _GIB // 2
    if model.startswith(("small", "distil-small")):
        return 5 * _GIB // 2
    if model.startswith(("medium", "distil-medium")):
        return 5 * _GIB
    return 8 * _GIB


def missing_model(name: str) -> AppError:
    return AppError("whisper_model_missing",
                    f"The transcription model “{name}” isn't installed on the server. "
                    f"Install it with: python -m app.whisper_models {name}")


# Files a faster-whisper model folder must hold. Without tokenizer.json, faster-whisper would
# fetch one through the Rust tokenizers library, whose connections the Python guard can't see.
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")


def check_model_files(path: str, name: str) -> None:
    """Raise whisper_model_missing unless the model folder at `path` is complete (an interrupted
    install can leave a folder the hub cache still returns)."""
    missing = [f for f in MODEL_FILES if not os.path.isfile(os.path.join(path, f))]
    if missing:
        print(f"transcriber: model {name!r} is missing {missing}", file=sys.stderr)
        raise missing_model(name)


def load_model(name: str, cpu_threads: int):
    """faster-whisper's model `name` from the local Hugging Face cache (never downloaded here)."""
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    try:
        path = download_model(name, local_files_only=True)
    except Exception as exc:  # huggingface_hub's "not in the cache" errors vary by version
        print(f"transcriber: model {name!r} not found locally: {exc!r}", file=sys.stderr)
        raise missing_model(name) from None
    check_model_files(path, name)
    try:
        return WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=cpu_threads,
                            num_workers=1)
    except Exception as exc:
        print(f"transcriber: can't load model {name!r}: {exc!r}", file=sys.stderr)
        raise AppError("transcription_failed") from None


def block_network() -> None:
    """Refuse every connection this process (and the libraries it loads) tries to open."""
    install_connect_guard(lambda _ip: False)


# --- windows -------------------------------------------------------------------------------

def quiet_cut(chunk, *, rate: int = WAV_RATE, search_seconds: float = SEARCH_SECONDS) -> int:
    """The sample index, inside the last `search_seconds` of `chunk`, at the middle of its
    quietest 100 ms frame: where a window can end with the least chance of splitting a word."""
    import numpy as np

    frame = max(1, int(rate * _FRAME_SECONDS))
    first = max(0, len(chunk) - int(rate * search_seconds))
    usable = (len(chunk) - first) // frame * frame
    if usable < frame:
        return len(chunk)
    region = chunk[first:first + usable].reshape(-1, frame)
    quietest = int(np.argmin(np.mean(np.square(region, dtype=np.float64), axis=1)))
    return first + quietest * frame + frame // 2


def iter_windows(path: Path, *, window_seconds: float | None = None,
                 search_seconds: float | None = None,
                 merge_seconds: float | None = None) -> Iterator[tuple[float, object]]:
    """(offset in seconds, float32 samples in [-1, 1]) for consecutive windows of the 16 kHz
    mono WAV at `path`, together covering every sample once. Only one window is in memory."""
    import numpy as np

    window = int((window_seconds or WINDOW_SECONDS) * WAV_RATE)
    search = search_seconds or SEARCH_SECONDS
    merge = int((merge_seconds or MERGE_TAIL_SECONDS) * WAV_RATE)
    try:
        wav = wave.open(str(path), "rb")
    except (wave.Error, EOFError, OSError) as exc:
        print(f"transcriber: unreadable WAV: {exc!r}", file=sys.stderr)
        raise AppError("transcription_failed") from None
    with wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, WAV_RATE):
            print("transcriber: the WAV isn't 16 kHz mono 16-bit", file=sys.stderr)
            raise AppError("transcription_failed")
        total = wav.getnframes()
        start = 0
        while start < total:
            last = total - start <= window + merge
            wav.setpos(start)
            data = wav.readframes(total - start if last else window)
            chunk = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            if len(chunk) == 0:
                return
            cut = len(chunk) if last else quiet_cut(chunk, search_seconds=search)
            yield start / WAV_RATE, chunk[:cut]
            start += cut


# --- the run -------------------------------------------------------------------------------

class _Progress:
    """`progress` events for one stage, at most four a second (the last always goes out)."""

    def __init__(self, emit: Callable[[dict], None], stage: str):
        self._emit, self._stage = emit, stage
        self._last = float("-inf")
        self._top = 0.0

    def __call__(self, percent: float | None, detail: str | None = None, *, force: bool = False) -> None:
        if percent is not None:
            percent = self._top = max(self._top, min(100.0, round(percent, 1)))
        now = time.monotonic()
        if not force and now - self._last < _EMIT_INTERVAL:
            return
        self._last = now
        event = {"event": "progress", "stage": self._stage, "percent": percent}
        if detail:
            event["detail"] = detail
        self._emit(event)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value) if 0 < value < float("inf") else None


def _args(req: dict) -> tuple[Path, Path, str, str, int, float | None, int]:
    workdir, source = _workdir(req), _local_file(req, "source")
    model, language = req.get("model"), req.get("language")
    threads, duration, max_seconds = req.get("cpu_threads"), req.get("duration"), req.get("max_seconds")
    if (model not in WHISPER_MODELS or not (language == AUTO or is_language(language))
            or type(threads) is not int or not 1 <= threads <= MAX_CPU_THREADS
            or (duration is not None and _number(duration) is None)
            or type(max_seconds) is not int or max_seconds <= 0
            or os.path.realpath(source.parent) == os.path.realpath(workdir)):
        print("transcriber: malformed request", file=sys.stderr)
        raise AppError("internal")
    return source, workdir, model, language, threads, _number(duration), max_seconds


def _transcribe(source: Path, workdir: Path, model_name: str, language: str, threads: int,
                duration: float | None, max_seconds: int, emit: Callable[[dict], None]) -> dict:
    too_long = AppError("video_too_long", f"This video is longer than the {max_seconds // 60}-minute "
                        "limit for AI Highlights.")
    found = probe(source)
    if found is None:
        raise AppError("unreadable_media")
    if "audio" not in found.kinds:
        raise AppError("no_audio", _NO_AUDIO)
    length = duration or found.duration
    if length is not None and length > max_seconds:
        raise too_long

    emit({"event": "phase", "phase": "extracting", "detail": "Extracting audio"})
    extracting = _Progress(emit, "extracting")
    wav = workdir / AUDIO_NAME
    try:
        extract_audio(source, wav, duration=length, max_bytes=WAV_BYTES_PER_SECOND * max_seconds + 65536,
                      on_progress=lambda percent: extracting(percent))
    except AppError as exc:
        raise too_long if exc.code == "video_too_long" else exc
    extracting(100.0, force=True)
    with wave.open(str(wav), "rb") as audio:
        audio_seconds = audio.getnframes() / WAV_RATE
    length = length or audio_seconds
    if length > max_seconds:
        raise too_long

    emit({"event": "phase", "phase": "transcribing", "detail": "Loading the transcription model"})
    transcribing = _Progress(emit, "transcribing")
    transcribing(0.0, force=True)
    model = load_model(model_name, threads)
    segments: list[Segment] = []
    windows: list[tuple[str, float]] = []  # (language, probability) of each window
    for offset, samples in iter_windows(wav):
        found_segments, info = model.transcribe(
            samples, language=None if language == AUTO else language, beam_size=5,
            vad_filter=True, condition_on_previous_text=False)
        detected = info.language if is_language(info.language) else None
        if detected:
            windows.append((detected, float(info.language_probability)))
        detail = f"Transcribing ({WHISPER_LANGUAGES[detected]})" if detected else "Transcribing"
        window_end = offset + len(samples) / WAV_RATE
        for piece in found_segments:
            text = clean_text(str(piece.text))
            start = round(max(offset + max(0.0, piece.start), segments[-1].start if segments else 0.0), 3)
            end = round(min(offset + piece.end, window_end, length), 3)
            if text and end > start:
                segments.append(Segment(start=start, end=end, text=text, language=detected))
            transcribing(100 * min(offset + piece.end, audio_seconds) / audio_seconds, detail)
        transcribing(100 * window_end / audio_seconds, detail, force=True)

    if language == AUTO:
        main = dominant_language(segments)
        probabilities = [p for lang, p in windows if lang == main]
        probability = round(max(probabilities), 3) if probabilities else None
    else:
        main, probability = language, None
    transcript = Transcript(origin="whisper", model=model_name, language=main,
                            language_probability=probability, requested_language=language,
                            duration=round(length, 3), segments=segments)
    save_transcript(workdir / TRANSCRIPT_NAME, transcript)
    wav.unlink()
    return {"file": TRANSCRIPT_NAME, "segments": len(segments), "language": main,
            "duration": transcript.duration}


def run_transcribe(req: dict, emit: Callable[[dict], None]) -> dict:
    """Transcribe req["source"] into req["workdir"]/transcript.json; on failure the workdir
    is left empty."""
    args = _args(req)
    workdir = args[1]
    try:
        result = _transcribe(*args, emit)
    except BaseException:
        _clear(workdir)
        raise
    _clear(workdir, keep=TRANSCRIPT_NAME)
    return result


def _lower_priority() -> None:
    try:
        os.nice(10)
    except OSError:
        pass


def _to_app_error(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        err = exc
    elif isinstance(exc, MemoryError):
        err = AppError("transcription_failed", "Transcription ran out of memory. "
                       "Try a smaller WHISPER_MODEL or a shorter video.")
    else:
        traceback.print_exception(exc, file=sys.stderr)
        return AppError("internal")
    print(f"transcriber: {err.code}: {exc}", file=sys.stderr)
    return err


def main() -> int:
    """Run one request; returns the exit code (0 after a result, 1 after an error)."""
    with protocol_out() as out:
        emit = _writer(out)
        try:
            req = _read_request(sys.stdin)
            if req["mode"] != "transcribe":
                print(f"transcriber: unknown mode {req['mode']!r}", file=sys.stderr)
                raise AppError("internal")
            model = req.get("model")
            limit_memory(memory_limit(model if model in WHISPER_MODELS else "large-v3"))
            _lower_priority()
            block_network()
            threads = req.get("cpu_threads")
            os.environ.update({"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                               "OMP_NUM_THREADS": str(threads if type(threads) is int and threads > 0 else 1)})
            event, code = {"event": "result", "data": run_transcribe(req, emit)}, 0
        except Exception as exc:
            err = _to_app_error(exc)
            event, code = {"event": "error", "code": err.code, "message": err.message}, 1
        emit(event)
    return code


if __name__ == "__main__":
    sys.exit(main())
