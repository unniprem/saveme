"""Worker `clip` mode: cut a range out of a source by stream copy (spec §6.5).
Each test runs the real worker subprocess; the source is a copy of a generated file."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.mediafile import probe
from tests.support.events import error_of, listing, result_of
from tests.support.procs import run_worker_proc

MiB = 1024 * 1024


@pytest.fixture
def job(tmp_path) -> Path:
    workdir = tmp_path / "job"
    workdir.mkdir()
    return workdir


@pytest.fixture
def source(media, tmp_path) -> Path:
    """long.mp4 (10 s, a keyframe every second, title "Long Clip", two chapters) in its own
    source folder, named like a ready source."""
    folder = tmp_path / "source"
    folder.mkdir()
    return Path(shutil.copy(media["long.mp4"], folder / "media.mp4"))


def cut(source: Path, job: Path, **change) -> tuple[list[dict], int]:
    request = {"mode": "clip", "source": str(source), "workdir": str(job), "start": 3.3, "end": 7.3,
               "keep_tags": True, "max_bytes": 50 * MiB, **change}
    return run_worker_proc(request)


def ffprobe_json(path: Path, *args: str) -> dict:
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", *args, "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


def test_clip_cuts_the_range(source, job):
    data = result_of(*cut(source, job))
    assert data == {"file": "media.mp4", "ext": "mp4"}
    assert probe(job / "media.mp4").codecs == ("h264", "aac")
    # What a viewer gets: about 4 s of shown video at 25 fps, and 4 s of audio. (The track
    # and container durations also count the hidden keyframe lead-in, so they read longer.)
    shown = ffprobe_json(job / "media.mp4", "-select_streams", "v", "-count_frames",
                         "-show_entries", "stream=nb_read_frames")["streams"][0]["nb_read_frames"]
    assert abs(int(shown) - 100) <= 3
    audio = ffprobe_json(job / "media.mp4", "-select_streams", "a", "-show_entries", "stream=duration")
    assert abs(float(audio["streams"][0]["duration"]) - 4.0) < 0.1
    assert listing(job) == ["media.mp4"]
    # The keyframe lead-in is hidden by the edit list: the first shown frame is at 0.
    frames = ffprobe_json(job / "media.mp4", "-select_streams", "v", "-read_intervals", "%+#30",
                          "-show_entries", "frame=pts_time")["frames"]
    assert abs(float(frames[0]["pts_time"])) < 0.05
    assert source.is_file()


def test_clip_reports_progress(source, job):
    events, rc = cut(source, job)
    result_of(events, rc)
    progress = [e for e in events if e["event"] == "progress"]
    assert progress
    assert all(e["detail"] == "Cutting the clip" and e["total_bytes"] is None for e in progress)
    percents = [e["percent"] for e in progress if e["percent"] is not None]
    assert percents == sorted(percents) and percents[-1] >= 99
    assert progress[-1]["downloaded_bytes"] > 0


def test_clip_drops_chapters_keeps_title_when_asked(source, job):
    result_of(*cut(source, job, keep_tags=True))
    out = ffprobe_json(job / "media.mp4", "-show_chapters", "-show_entries", "format_tags=title")
    assert out["chapters"] == []
    assert out["format"]["tags"]["title"] == "Long Clip"


def test_clip_drops_all_tags_for_uploads(source, job):
    result_of(*cut(source, job, keep_tags=False))
    out = ffprobe_json(job / "media.mp4", "-show_chapters", "-show_entries", "format_tags")
    assert out["chapters"] == []
    assert "title" not in out.get("format", {}).get("tags", {})


def test_clip_too_large(source, job):
    error = error_of(*cut(source, job, max_bytes=1000))
    assert error["code"] == "too_large"
    assert listing(job) == []


@pytest.mark.parametrize("change", [
    {"end": 3.3}, {"end": 2.0}, {"start": -1}, {"start": True}, {"end": "7"},
    {"keep_tags": "yes"}, {"max_bytes": 0}, {"max_bytes": True}, {"workdir": "relative"},
], ids=["empty_range", "reversed", "negative", "bool_start", "str_end", "str_tags", "zero_limit",
        "bool_limit", "relative_workdir"])
def test_clip_bad_request_is_internal(source, job, change):
    assert error_of(*cut(source, job, **change))["code"] == "internal"
    assert listing(job) == []


def test_clip_source_must_be_a_media_file(media, job, tmp_path):
    odd = Path(shutil.copy(media["long.mp4"], tmp_path / "upload.bin"))
    assert error_of(*cut(odd, job))["code"] == "internal"
    link = tmp_path / "media.mp4"
    os.symlink(media["long.mp4"], link)
    assert error_of(*cut(link, job))["code"] == "internal"
    assert listing(job) == []


def test_clip_from_mkv_stays_mkv(media, job, tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    src = Path(shutil.copy(media["sample.mkv"], folder / "media.mkv"))
    data = result_of(*cut(src, job, start=0.5, end=1.5))
    assert data == {"file": "media.mkv", "ext": "mkv"}
    assert listing(job) == ["media.mkv"]
