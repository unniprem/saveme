"""Worker `batch` mode: cut several ranges out of a source into one ZIP (batch export design
§6.3). Most tests run the real worker subprocess on a copy of a generated file."""
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app import mediafile
from app.errors import AppError
from app.mediafile import probe
from tests.support.events import error_of, listing, result_of
from tests.support.procs import run_worker_proc

MiB = 1024 * 1024
NAMES = ["01 Long (clip 0m00.5s-0m02.5s).mp4", "02 Long (clip 0m03.3s-0m07.3s).mp4",
         "03 Long (clip 0m08s-0m10s).mp4"]
CLIPS = [{"start": 0.5, "end": 2.5, "name": NAMES[0]}, {"start": 3.3, "end": 7.3, "name": NAMES[1]},
         {"start": 8.0, "end": 10.0, "name": NAMES[2]}]


@pytest.fixture
def job(tmp_path) -> Path:
    workdir = tmp_path / "job"
    workdir.mkdir()
    return workdir


@pytest.fixture
def source(media, tmp_path) -> Path:
    """long.mp4 (10 s at 25 fps, a keyframe every second, title "Long Clip") as a ready source."""
    folder = tmp_path / "source"
    folder.mkdir()
    return Path(shutil.copy(media["long.mp4"], folder / "media.mp4"))


def batch(source: Path, job: Path, **change) -> tuple[list[dict], int]:
    request = {"mode": "batch", "source": str(source), "workdir": str(job), "clips": CLIPS,
               "keep_tags": True, "max_bytes": 50 * MiB, **change}
    return run_worker_proc(request)


def ffprobe_json(path: Path, *args: str) -> dict:
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", *args, "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


def unpack(archive: Path, into: Path) -> list[Path]:
    into.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(into)
    return [into / name for name in NAMES]


def test_batch_cuts_every_range_into_one_zip(source, job, tmp_path):
    data = result_of(*batch(source, job))
    assert data == {"file": "media.zip", "ext": "zip", "count": 3}
    assert listing(job) == ["media.zip"]
    with zipfile.ZipFile(job / "media.zip") as bundle:
        assert bundle.testzip() is None
        infos = bundle.infolist()
    assert [i.filename for i in infos] == NAMES
    assert all(i.compress_type == zipfile.ZIP_STORED for i in infos)
    for path, seconds in zip(unpack(job / "media.zip", tmp_path / "out"), (2.0, 4.0, 2.0)):
        assert probe(path).codecs == ("h264", "aac")
        shown = ffprobe_json(path, "-select_streams", "v", "-count_frames",
                             "-show_entries", "stream=nb_read_frames")["streams"][0]["nb_read_frames"]
        assert abs(int(shown) - seconds * 25) <= 3, path.name
        frames = ffprobe_json(path, "-select_streams", "v", "-read_intervals", "%+#30",
                              "-show_entries", "frame=pts_time")["frames"]
        assert abs(float(frames[0]["pts_time"])) < 0.05, path.name  # starts at the chosen time
    assert source.is_file()


def test_batch_reports_progress_over_the_whole_batch(source, job):
    events, rc = batch(source, job)
    result_of(events, rc)
    progress = [e for e in events if e["event"] == "progress"]
    details = [e["detail"] for e in progress]
    assert details == sorted(details) and set(details) == {f"Cutting clip {n} of 3" for n in (1, 2, 3)}
    percents = [e["percent"] for e in progress if e["percent"] is not None]
    assert percents == sorted(percents) and percents[-1] >= 99
    written = [e["downloaded_bytes"] for e in progress if e["downloaded_bytes"] is not None]
    assert written == sorted(written) and written[-1] > 0
    assert all(e["total_bytes"] is None for e in progress)


def test_batch_keeps_or_drops_tags(source, job, tmp_path):
    result_of(*batch(source, job, clips=CLIPS[:1], keep_tags=True))
    kept = unpack(job / "media.zip", tmp_path / "kept")[0]
    out = ffprobe_json(kept, "-show_chapters", "-show_entries", "format_tags=title")
    assert out["chapters"] == [] and out["format"]["tags"]["title"] == "Long Clip"
    (job / "media.zip").unlink()
    result_of(*batch(source, job, clips=CLIPS[:1], keep_tags=False))
    dropped = unpack(job / "media.zip", tmp_path / "dropped")[0]
    assert "title" not in ffprobe_json(dropped, "-show_entries", "format_tags").get("format", {}).get("tags", {})


def test_batch_from_mkv_holds_mkv_clips(media, job, tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    src = Path(shutil.copy(media["sample.mkv"], folder / "media.mkv"))
    clips = [{"start": 0.0, "end": 1.0, "name": "01 a.mkv"}, {"start": 1.0, "end": 2.0, "name": "02 b.mkv"}]
    assert result_of(*batch(src, job, clips=clips))["count"] == 2
    with zipfile.ZipFile(job / "media.zip") as bundle:
        assert bundle.namelist() == ["01 a.mkv", "02 b.mkv"]


def test_batch_too_large_counts_the_whole_zip(source, job):
    result_of(*batch(source, job, clips=CLIPS[:1]))
    one = (job / "media.zip").stat().st_size
    (job / "media.zip").unlink()
    # Room for the first clip but not for both: each fits, the ZIP doesn't.
    error = error_of(*batch(source, job, clips=[CLIPS[0], {**CLIPS[2], "start": 0.5, "end": 2.5}],
                            max_bytes=int(one * 1.5)))
    assert error["code"] == "too_large"
    assert error["message"].startswith("These clips add up to more than the ")
    assert listing(job) == []
    error = error_of(*batch(source, job, max_bytes=1000))
    assert error["code"] == "too_large" and listing(job) == []


def bad(**clip):
    return [{**CLIPS[0], **clip}]


@pytest.mark.parametrize("change", [
    {"clips": []}, {"clips": CLIPS * 7}, {"clips": "x"}, {"clips": ["x"]},
    {"clips": bad(end=0.5)}, {"clips": bad(start=-1)}, {"clips": bad(start=True)}, {"clips": bad(end="2")},
    {"clips": bad(name="a/b.mp4")}, {"clips": bad(name="..\\b.mp4")}, {"clips": bad(name=".mp4")},
    {"clips": bad(name=".hidden.mp4")}, {"clips": bad(name="clip.mkv")}, {"clips": bad(name="a\x00.mp4")},
    {"clips": bad(name="a:b.mp4")}, {"clips": bad(name=None)}, {"clips": bad(name="x" * 300 + ".mp4")},
    {"clips": [CLIPS[0], {**CLIPS[1], "name": NAMES[0].upper()}]},
    {"keep_tags": "yes"}, {"max_bytes": 0}, {"workdir": "relative"},
], ids=["empty", "too_many", "not_list", "not_object", "reversed", "negative", "bool_start", "str_end",
        "slash", "backslash", "dot_ext_only", "leading_dot", "wrong_ext", "nul", "colon", "no_name",
        "long_name", "repeated_name", "str_tags", "zero_limit", "relative_workdir"])
def test_batch_bad_request_is_internal(source, job, change):
    assert error_of(*batch(source, job, **change))["code"] == "internal"
    assert listing(job) == []


def test_batch_source_must_be_a_media_file_outside_the_workdir(media, source, job, tmp_path):
    odd = Path(shutil.copy(media["long.mp4"], tmp_path / "upload.bin"))
    assert error_of(*batch(odd, job))["code"] == "internal"
    link = tmp_path / "media.mp4"
    os.symlink(media["long.mp4"], link)
    assert error_of(*batch(link, job))["code"] == "internal"
    assert listing(job) == []
    inside = Path(shutil.copy(media["long.mp4"], job / "media.mp4"))
    assert error_of(*batch(inside, job))["code"] == "internal"


def test_a_clip_that_fails_names_its_number(tmp_path, monkeypatch):
    """In process: the second cut fails, so the batch fails naming clip 2."""
    calls = []

    def fake_cut(src, dst, *, start, end, keep_tags, max_bytes, on_progress):
        calls.append((start, end))
        if len(calls) == 2:
            raise AppError("clip_failed")
        on_progress(100.0, 10)
        dst.write_bytes(b"clip")

    monkeypatch.setattr(mediafile, "cut", fake_cut)
    src = tmp_path / "media.mp4"
    src.write_bytes(b"source")
    with pytest.raises(AppError) as ei:
        mediafile.cut_batch(src, tmp_path, [(0, 1, "01 a.mp4"), (1, 2, "02 b.mp4"), (2, 3, "03 c.mp4")],
                            keep_tags=False, max_bytes=MiB, on_progress=lambda *a: None)
    assert (ei.value.code, ei.value.message) == ("clip_failed",
                                                 "Couldn't cut clip 2. Try a different start or end.")
    assert len(calls) == 2  # it stops at the first failure
