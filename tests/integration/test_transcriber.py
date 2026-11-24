"""The transcriber worker (`python -m app.transcriber`): audio extraction with the real FFmpeg,
windowed transcription with a fake Whisper model (tests/support/transcriber_entry.py), the
transcript file, errors, the network block, and being killed on cancel. An opt-in test runs the
real faster-whisper `base` model when it is already in the Hugging Face cache."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from app.errors import AppError
from app.mediafile import extract_audio
from app.runner import run_worker
from app.transcript import load_transcript
from tests.support.events import error_of, listing, result_of
from tests.support.procs import ROOT, run_worker_proc

ENTRY = "tests.support.transcriber_entry"
CMD = (sys.executable, "-P", "-m", ENTRY)


@pytest.fixture
def workdir(tmp_path) -> Path:
    path = tmp_path / "source" / "ai" / "run"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def source(media, tmp_path) -> Path:
    """long.mp4 (10 s of video with a 440 Hz tone) as a ready source's media file."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    return Path(shutil.copy(media["long.mp4"], folder / "media.mp4"))


def request(source: Path, workdir: Path, change: dict | None = None, **more) -> dict:
    return {"mode": "transcribe", "source": str(source), "workdir": str(workdir), "model": "base",
            "language": "auto", "cpu_threads": 1, "duration": 10.0, "max_seconds": 3600,
            **(change or {}), **more}


def progress(events: list[dict], stage: str) -> list[float]:
    return [e["percent"] for e in events
            if e["event"] == "progress" and e.get("stage") == stage and e.get("percent") is not None]


# --- audio extraction ------------------------------------------------------------------

def test_extract_audio_writes_16k_mono_pcm(source, tmp_path):
    seen = []
    dst = tmp_path / "audio.wav"
    extract_audio(source, dst, duration=10.0, max_bytes=10 * 1024 * 1024,
                  on_progress=lambda percent: seen.append(percent))
    with wave.open(str(dst)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert abs(wav.getnframes() / 16000 - 10.0) < 0.1
    assert seen and seen[-1] == 100.0
    assert seen == sorted(seen)


def test_extract_audio_refuses_audio_past_the_size_limit(source, tmp_path):
    with pytest.raises(AppError) as exc:
        extract_audio(source, tmp_path / "audio.wav", duration=10.0, max_bytes=100_000,
                      on_progress=lambda percent: None)
    assert exc.value.code == "video_too_long"


def test_extract_audio_fails_cleanly_on_a_broken_file(tmp_path):
    broken = tmp_path / "media.mp4"
    broken.write_bytes(b"\0" * 4096)
    with pytest.raises(AppError) as exc:
        extract_audio(broken, tmp_path / "audio.wav", duration=10.0, max_bytes=10_000_000,
                      on_progress=lambda percent: None)
    assert exc.value.code == "transcription_failed"


# --- the worker, with the fake model -----------------------------------------------------

def test_transcribe_writes_the_transcript_and_reports_real_progress(source, workdir, monkeypatch, tmp_path):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("SAVEME_TEST_WHISPER_LOG", str(log))
    events, rc = run_worker_proc(request(source, workdir), entry=ENTRY)
    data = result_of(events, rc)
    assert data == {"file": "transcript.json", "segments": 10, "language": "en", "duration": 10.0}
    assert listing(workdir) == ["transcript.json"]  # the WAV is gone
    transcript = load_transcript(workdir / "transcript.json")
    assert transcript is not None
    assert transcript.origin == "whisper" and transcript.model == "base"
    assert transcript.requested_language == "auto" and transcript.language == "en"
    assert [s.text for s in transcript.segments][:2] == ["second 0", "second 1"]
    assert transcript.segments[0].start == 0.1 and transcript.segments[-1].end == 9.9
    phases = [e["phase"] for e in events if e["event"] == "phase"]
    assert phases == ["extracting", "transcribing"]
    for stage in ("extracting", "transcribing"):
        values = progress(events, stage)
        assert values and values == sorted(values) and values[-1] <= 100.0, stage
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0]["vad_filter"] is True and calls[0]["condition_on_previous_text"] is False
    assert calls[0]["language"] is None and calls[0]["dtype"] == "float32"


def test_transcribe_offsets_segments_across_windows_and_keeps_languages(source, workdir, monkeypatch):
    # 10 s in windows of about 4 s (a cut searched in the last 1 s; tails under 1 s merged).
    monkeypatch.setenv("SAVEME_TEST_WINDOW_SECONDS", "4")
    monkeypatch.setenv("SAVEME_TEST_SEARCH_SECONDS", "1")
    monkeypatch.setenv("SAVEME_TEST_MERGE_SECONDS", "1")
    monkeypatch.setenv("SAVEME_TEST_WHISPER_LANGS", "ml,ml,en")
    events, rc = run_worker_proc(request(source, workdir), entry=ENTRY)
    result_of(events, rc)
    transcript = load_transcript(workdir / "transcript.json")
    starts = [s.start for s in transcript.segments]
    assert starts == sorted(starts)
    assert len({s.language for s in transcript.segments}) == 2
    assert transcript.language == "ml"  # two of the three windows
    assert transcript.segments[0].language == "ml" and transcript.segments[-1].language == "en"
    # Times are absolute: segments of a later window start after the earlier windows.
    assert starts[-1] > 6.0 and transcript.segments[-1].end <= 10.0


def test_transcribe_passes_a_chosen_language(source, workdir, monkeypatch, tmp_path):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("SAVEME_TEST_WHISPER_LOG", str(log))
    result_of(*run_worker_proc(request(source, workdir, language="ml"), entry=ENTRY))
    assert json.loads(log.read_text().splitlines()[0])["language"] == "ml"
    transcript = load_transcript(workdir / "transcript.json")
    assert transcript.requested_language == "ml" and transcript.language == "ml"


def test_transcribe_without_audio_track_says_to_import_subtitles(media, workdir, tmp_path):
    silent = Path(shutil.copy(media["video_only.mp4"], tmp_path / "source" / "media.mp4"))
    err = error_of(*run_worker_proc(request(silent, workdir, duration=2.0), entry=ENTRY))
    assert err["code"] == "no_audio" and "subtitles" in err["message"]
    assert listing(workdir) == []


def test_transcribe_refuses_a_video_over_the_length_limit(source, workdir):
    err = error_of(*run_worker_proc(request(source, workdir, max_seconds=300, duration=301.0), entry=ENTRY))
    assert err["code"] == "video_too_long" and "5-minute limit" in err["message"]
    # Without a known duration the audio's own length decides.
    err = error_of(*run_worker_proc(request(source, workdir, max_seconds=5, duration=None), entry=ENTRY))
    assert err["code"] == "video_too_long"
    assert listing(workdir) == []


def test_transcribe_names_a_missing_model(source, workdir, monkeypatch):
    monkeypatch.setenv("SAVEME_TEST_WHISPER_MISSING", "1")
    err = error_of(*run_worker_proc(request(source, workdir), entry=ENTRY))
    assert err["code"] == "whisper_model_missing"
    assert "base" in err["message"] and "app.whisper_models" in err["message"]
    assert listing(workdir) == []


@pytest.mark.parametrize("change", [
    {"model": "huge"}, {"language": "xx"}, {"language": None}, {"cpu_threads": 0},
    {"cpu_threads": True}, {"duration": -1}, {"duration": "10"}, {"max_seconds": 0},
    {"source": "relative/media.mp4"}, {"workdir": "relative"},
])
def test_transcribe_refuses_malformed_requests(source, workdir, change):
    err = error_of(*run_worker_proc(request(source, workdir, change), entry=ENTRY))
    assert err["code"] == "internal"


def test_transcribe_refuses_a_workdir_holding_the_source(source):
    err = error_of(*run_worker_proc(request(source, source.parent), entry=ENTRY))
    assert err["code"] == "internal"


def test_transcriber_blocks_every_network_connection():
    code = ("from app.transcriber import block_network; block_network(); import socket\n"
            "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
            "except ConnectionError as exc:\n    print('blocked', exc)\n")
    out = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True,
                         text=True, timeout=30, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.stdout.startswith("blocked"), out.stderr


async def test_cancelling_a_transcription_kills_the_worker(source, workdir, monkeypatch):
    monkeypatch.setenv("SAVEME_TEST_WHISPER_DELAY", "5")
    task = asyncio.create_task(run_worker(request(source, workdir), cmd=CMD, timeout=60,
                                          on_event=lambda event: None, workdir=workdir))
    for _ in range(300):  # wait for the transcribing phase (the WAV exists)
        if (workdir / "audio.wav").exists():
            break
        await asyncio.sleep(0.05)
    pids = subprocess.run(["pgrep", "-f", ENTRY], capture_output=True, text=True).stdout.split()
    assert pids
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    left = subprocess.run(["pgrep", "-f", ENTRY], capture_output=True, text=True).stdout.split()
    assert not set(pids) & set(left)


# --- the real model (opt-in: needs faster-whisper's `base` in the Hugging Face cache) ------------

def _base_model_cached() -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model("base", local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _base_model_cached(), reason="faster-whisper base model not cached")
def test_real_whisper_transcribes_speech(workdir, tmp_path, monkeypatch):
    monkeypatch.setenv("SAVEME_TEST_REAL_WHISPER", "1")
    if shutil.which("say") is None:
        pytest.skip("needs macOS `say` to make speech")
    speech = tmp_path / "speech.aiff"
    subprocess.run(["say", "-o", str(speech), "Artificial intelligence will change how we write "
                    "software. Electric vehicles are getting cheaper every year."], check=True)
    folder = tmp_path / "source"
    subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=25", "-i", str(speech),
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(folder / "media.mp4")], check=True)
    data = result_of(*run_worker_proc(request(folder / "media.mp4", workdir, duration=None),
                                      entry=ENTRY, timeout=300))
    transcript = load_transcript(workdir / "transcript.json")
    text = " ".join(s.text for s in transcript.segments).lower()
    assert data["language"] == "en" and "intelligence" in text and "vehicles" in text
    assert transcript.language_probability is not None and transcript.language_probability > 0.5


# --- windows (pure functions) ------------------------------------------------------------

def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.astype(np.int16).tobytes())


def test_quiet_cut_lands_in_the_silent_gap():
    from app.transcriber import quiet_cut

    rng = np.random.default_rng(1)
    chunk = rng.normal(0, 0.3, 16000 * 10).astype(np.float32)
    chunk[16000 * 8 + 4000:16000 * 8 + 9000] = 0  # silence at 8.25-8.56 s
    cut = quiet_cut(chunk, rate=16000, search_seconds=4)
    assert 16000 * 8 + 4000 <= cut <= 16000 * 8 + 9000


def test_iter_windows_covers_every_sample_once(tmp_path):
    from app.transcriber import iter_windows

    rng = np.random.default_rng(2)
    samples = (rng.normal(0, 3000, 16000 * 23)).clip(-32768, 32767)
    path = tmp_path / "audio.wav"
    _write_wav(path, samples)
    windows = list(iter_windows(path, window_seconds=5, search_seconds=2, merge_seconds=3))
    offsets = [offset for offset, _audio in windows]
    lengths = [len(audio) for _offset, audio in windows]
    assert offsets[0] == 0.0 and sum(lengths) == 16000 * 23
    for (offset, audio), nxt in zip(windows, offsets[1:]):
        assert abs(offset + len(audio) / 16000 - nxt) < 1e-9
    assert all(3 * 16000 <= n <= 5 * 16000 for n in lengths[:-1])
    assert lengths[-1] <= 8 * 16000  # a short tail joins the last window
    assert all(audio.dtype == np.float32 and np.abs(audio).max() <= 1.0 for _o, audio in windows)


def test_iter_windows_refuses_a_wav_in_the_wrong_format(tmp_path):
    from app.transcriber import iter_windows

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\0" * 1000)
    with pytest.raises(AppError) as exc:
        list(iter_windows(path))
    assert exc.value.code == "transcription_failed"
