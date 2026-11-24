"""The renderer worker (`python -m app.renderer`, mode `render`) with the real FFmpeg: every output
is read back with ffprobe (codecs, size, pixel format, frame rate, duration) and, where the layout
matters, by looking at the rendered pixels of colour-coded sources."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from app import renderer
from app.encoders import HARDWARE, SOFTWARE
from app.errors import AppError
from app.runner import run_worker
from tests.support.events import error_of, listing, result_of
from tests.support.procs import ROOT, run_worker_proc
from tests.support.render import as_source, colour_at, ffprobe, frame_rgb, render_request

CMD = (sys.executable, "-P", "-m", "app.renderer")


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "source" / "render" / "r1" / "work-1"
    path.mkdir(parents=True)
    return path


def run(req: dict) -> tuple[dict, list[dict]]:
    """Run a render in this process (as the worker does after its setup); returns (result, events)."""
    events: list[dict] = []
    result = renderer.run_render(req, events.append)
    return result, events


def source_of(render_media, tmp_path, name="thirds.mp4"):
    return as_source(render_media[name], tmp_path / "source")


# --- formats ---------------------------------------------------------------------------------

def test_a_standard_9_16_short_is_h264_high_and_aac_at_1080x1920(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    result, events = run(render_request(src, workdir, width=None, height=None, start=1.0, end=4.0))
    out = workdir / "clip-01.mp4"
    probe = ffprobe(out)
    assert (probe["video"]["codec_name"], probe["video"]["profile"], probe["video"]["pix_fmt"]) == \
        ("h264", "High", "yuv420p")
    assert (probe["video"]["width"], probe["video"]["height"]) == (1080, 1920)
    assert probe["video"]["r_frame_rate"] == "25/1"  # the source's
    assert (probe["audio"]["codec_name"], probe["audio"]["sample_rate"], probe["audio"]["channels"]) == \
        ("aac", "48000", 2)
    assert abs(probe["duration"] - 3.0) <= 0.05
    assert "mp4" in probe["format"]
    assert result["file"] == "clip-01.mp4" and result["encoder"] == SOFTWARE and result["mode"] == "center"
    assert listing(workdir) == ["clip-01.mp4"]  # graph and part file gone
    percents = [e["percent"] for e in events if e["event"] == "progress"]
    assert percents[0] == 0.0 and percents[-1] == 100.0 and percents == sorted(percents)


@pytest.mark.parametrize("aspect,size", [("9:16", (540, 960)), ("16:9", (960, 540)), ("1:1", (540, 540)),
                                         ("4:5", (540, 674)), ("source", (480, 270))])
def test_each_aspect_has_its_default_size(render_media, tmp_path, workdir, aspect, size):
    src = source_of(render_media, tmp_path)
    run(render_request(src, workdir, aspect=aspect, width=None, height=None, quality="draft"))
    probe = ffprobe(workdir / "clip-01.mp4")  # draft renders at half size; source keeps 960x540 / 2
    assert (probe["video"]["width"], probe["video"]["height"]) == size


def test_fps_can_be_chosen(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    run(render_request(src, workdir, fps=30))
    probe = ffprobe(workdir / "clip-01.mp4")
    assert probe["video"]["r_frame_rate"] == "30/1" and abs(probe["duration"] - 2.0) <= 0.05


def test_a_clip_is_cut_at_its_own_times(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    run(render_request(src, workdir, start=2.5, end=4.0))
    probe = ffprobe(workdir / "clip-01.mp4")
    assert abs(probe["duration"] - 1.5) <= 0.04 + 0.03  # 1.5 s at 25 fps is 37.5 frames: rounded up
    assert probe["video_start"] == 0.0 and probe["audio_start"] == 0.0


def test_a_source_without_audio_gets_a_silent_aac_track(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path, "silent.mp4")
    run(render_request(src, workdir))
    probe = ffprobe(workdir / "clip-01.mp4")
    assert probe["audio"]["codec_name"] == "aac" and abs(probe["duration"] - 2.0) <= 0.05


def test_a_rotated_source_is_framed_as_it_is_displayed(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path, "rotated.mp4")
    result, _ = run(render_request(src, workdir, aspect="source", width=None, height=None))
    probe = ffprobe(workdir / "clip-01.mp4")
    assert (probe["video"]["width"], probe["video"]["height"]) == (360, 640)


def test_non_square_pixels_are_corrected_first(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path, "anamorphic.mp4")  # displays as the 960x540 thirds
    run(render_request(src, workdir, end=1.0))
    frame = frame_rgb(workdir / "clip-01.mp4", 0.5)
    assert colour_at(frame, 0.5, 0.5) == "green"
    assert colour_at(frame, 0.02, 0.5) == "green"  # a 9:16 window of 960 wide stays in the middle third


# --- layouts ---------------------------------------------------------------------------------

def test_center_crop_takes_the_middle(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    run(render_request(src, workdir))
    frame = frame_rgb(workdir / "clip-01.mp4", 1.0)
    assert {colour_at(frame, x, y) for x in (0.1, 0.5, 0.9) for y in (0.1, 0.5, 0.9)} == {"green"}


def test_a_manual_position_moves_the_window(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    run(render_request(src, workdir, layout={"mode": "center", "manual": {"x": 0.0, "y": 0.5, "zoom": 1.0}}))
    frame = frame_rgb(workdir / "clip-01.mp4", 1.0)
    assert colour_at(frame, 0.5, 0.5) == "red"
    result, _ = run(render_request(src, workdir, output="clip-02.mp4",
                                   layout={"mode": "face", "manual": {"x": 1.0, "y": 0.5, "zoom": 2.0}}))
    assert result["mode"] == "manual"
    assert colour_at(frame_rgb(workdir / "clip-02.mp4", 1.0), 0.5, 0.5) == "blue"


def test_blur_keeps_the_whole_frame_over_a_blurred_copy(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    result, _ = run(render_request(src, workdir, layout={"mode": "blur"}))
    frame = frame_rgb(workdir / "clip-01.mp4", 1.0)
    # the 16:9 frame is 270 wide, 152 high, centred: all three thirds are visible across the middle
    assert [colour_at(frame, x, 0.5) for x in (0.1, 0.5, 0.9)] == ["red", "green", "blue"]
    # above it, a blurred, darkened copy: never black, never a hard colour edge
    assert colour_at(frame, 0.5, 0.1) not in ("black", "white")
    assert result["mode"] == "blur"


def test_original_pads_with_the_chosen_colour(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    run(render_request(src, workdir, layout={"mode": "original"}))
    frame = frame_rgb(workdir / "clip-01.mp4", 1.0)
    assert colour_at(frame, 0.5, 0.05) == "black" and colour_at(frame, 0.5, 0.5) == "green"
    run(render_request(src, workdir, output="clip-02.mp4", layout={"mode": "original", "pad_color": "#FFFF00",
                                                                     "fg_position": 0.3}))
    frame = frame_rgb(workdir / "clip-02.mp4", 1.0)
    assert colour_at(frame, 0.5, 0.95) == "yellow" and colour_at(frame, 0.5, 0.3) == "green"


def test_split_without_faces_stacks_the_left_and_right_halves(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    result, _ = run(render_request(src, workdir, layout={"mode": "split"}))
    frame = frame_rgb(workdir / "clip-01.mp4", 1.0)
    assert colour_at(frame, 0.5, 0.25) == "red" and colour_at(frame, 0.5, 0.75) == "blue"
    assert result["mode"] == "split"


def test_face_modes_without_an_analysis_use_their_fallback(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    result, _ = run(render_request(src, workdir, layout={"mode": "face", "fallback": "blur"}))
    assert result["mode"] == "blur"


def test_a_source_of_the_same_aspect_is_scaled_not_cropped(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path, "portrait.mp4")
    run(render_request(src, workdir))
    probe = ffprobe(workdir / "clip-01.mp4")
    assert (probe["video"]["width"], probe["video"]["height"]) == (270, 480)


# --- stills, branding, intro/outro -----------------------------------------------------------

def test_a_preview_still_is_a_jpeg_of_the_canvas(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    result, _ = run(render_request(src, workdir, kind="frame", output="frame.jpg", at=1.0,
                                   layout={"mode": "original"}))
    out = workdir / "frame.jpg"
    assert out.read_bytes()[:3] == b"\xff\xd8\xff"
    probe = ffprobe(out)
    assert (probe["video"]["width"], probe["video"]["height"]) == (270, 480)
    assert colour_at(frame_rgb(out, 0), 0.5, 0.5) == "green" and listing(workdir) == ["frame.jpg"]
    assert result["mode"] == "original"


def test_a_logo_is_overlaid_in_its_corner(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    logo = {"path": str(render_media["logo.rgba"]), "scale": 0.4, "opacity": 1.0, "position": "top-left",
            "width": 200, "height": 100}
    run(render_request(src, workdir, logo=logo))
    frame = frame_rgb(workdir / "clip-01.mp4", 1.0)
    # the logo is 108×54 px (0.4 of 270) at a 10 px margin: its left half white, its right half clear
    assert colour_at(frame, 35 / 269, 37 / 479, size=2) == "white"
    assert colour_at(frame, 95 / 269, 37 / 479, size=2) == "green"
    assert colour_at(frame, 0.5, 0.8) == "green"


def test_intro_and_outro_are_joined_around_the_clip(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    result, _ = run(render_request(src, workdir, intro=str(render_media["intro.mp4"]),
                                   outro=str(render_media["outro.mp4"])))
    out = workdir / "clip-01.mp4"
    probe = ffprobe(out)
    assert abs(probe["duration"] - 4.0) <= 0.1 and result["duration"] == pytest.approx(4.0, abs=0.1)
    assert colour_at(frame_rgb(out, 0.5), 0.5, 0.5) == "yellow"
    assert colour_at(frame_rgb(out, 2.0), 0.5, 0.5) == "green"
    assert colour_at(frame_rgb(out, 3.5), 0.5, 0.5) == "magenta"


# --- limits and failures -----------------------------------------------------------------------

def test_a_short_over_the_size_limit_fails_and_leaves_nothing(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    with pytest.raises(AppError) as exc:
        run(render_request(src, workdir, max_bytes=20_000, width=1080, height=1920))
    assert exc.value.code == "too_large" and listing(workdir) == []


def test_a_failed_hardware_encode_falls_back_to_libx264(render_media, tmp_path, workdir, monkeypatch):
    real = renderer.video_args

    def broken_hardware(encoder, quality, **kw):
        if encoder == HARDWARE:
            return ["-c:v", "no_such_hardware_encoder"]
        return real(encoder, quality, **kw)

    monkeypatch.setattr(renderer, "video_args", broken_hardware)
    src = source_of(render_media, tmp_path)
    result, events = run(render_request(src, workdir, encoder=HARDWARE))
    assert result["encoder"] == SOFTWARE and "software" in result["notice"]
    assert ffprobe(workdir / "clip-01.mp4")["video"]["codec_name"] == "h264"
    assert [e["encoder"] for e in events if e["event"] == "progress"][-1] == SOFTWARE


def test_an_unreadable_source_is_refused(tmp_path, workdir):
    folder = tmp_path / "source"
    (folder / "media.mp4").write_bytes(b"not a video")
    with pytest.raises(AppError) as exc:
        run(render_request(folder / "media.mp4", workdir))
    assert exc.value.code == "unreadable_media" and listing(workdir) == []


@pytest.mark.parametrize("change", [
    {"kind": "gif"}, {"output": "../x.mp4"}, {"output": "x.mkv"}, {"start": -1.0}, {"end": 0.0},
    {"aspect": "2:1"}, {"width": 271}, {"width": 0}, {"fps": 240}, {"quality": "ultra"},
    {"encoder": "nvenc"}, {"threads": 0}, {"threads": True}, {"layout": {"mode": "zoom"}},
    {"layout": {"mode": "center", "margin": 2}}, {"subtitles": True},  # no subs.ass in the workdir
    {"fonts_dir": "fonts; rm"},
    {"logo": {"path": "/nope.rgba", "scale": 0.1, "opacity": 1, "position": "top-left", "width": 2, "height": 2}},
    {"max_bytes": 0}, {"kind": "frame", "output": "f.jpg", "at": 99.0},
])
def test_malformed_requests_are_refused(render_media, tmp_path, workdir, change):
    src = source_of(render_media, tmp_path)
    with pytest.raises(AppError) as exc:
        renderer.parse_render(render_request(src, workdir, **change))
    assert exc.value.code == "internal"


def test_the_workdir_must_not_hold_the_source(render_media, tmp_path):
    src = source_of(render_media, tmp_path)
    with pytest.raises(AppError):
        renderer.parse_render(render_request(src, src.parent))


# --- the process -----------------------------------------------------------------------------

def test_the_worker_speaks_the_protocol(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    events, rc = run_worker_proc(render_request(src, workdir), entry="app.renderer", timeout=120)
    data = result_of(events, rc)
    assert data["width"] == 270 and data["height"] == 480 and data["encoder"] == SOFTWARE
    assert all(e["event"] in ("progress", "result") for e in events)


def test_the_worker_reports_errors_as_events(tmp_path, workdir):
    events, rc = run_worker_proc({"mode": "render", "kind": "video"}, entry="app.renderer")
    assert error_of(events, rc)["code"] == "internal"
    events, rc = run_worker_proc({"mode": "paint"}, entry="app.renderer")
    assert error_of(events, rc)["code"] == "internal"


def test_the_renderer_blocks_every_network_connection(render_media, tmp_path, workdir):
    code = ("import sys, json, io\nfrom app import renderer\nimport app.netguard as g\n"
            "g.install_connect_guard(lambda ip: False)\nimport socket\n"
            "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
            "except ConnectionError as exc:\n    print('blocked', exc)\n")
    out = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True, text=True,
                         timeout=30, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.stdout.startswith("blocked"), out.stderr
    source_text = (ROOT / "app" / "renderer.py").read_text()
    assert "install_connect_guard(lambda _ip: False)" in source_text


async def test_cancelling_a_render_kills_ffmpeg(render_media, tmp_path, workdir):
    src = source_of(render_media, tmp_path)
    started = asyncio.Event()

    def on_event(event):
        if event.get("event") == "progress" and (event.get("percent") or 0) > 0:
            started.set()

    req = render_request(src, workdir, end=6.0, width=1080, height=1920, quality="high")
    task = asyncio.create_task(run_worker(req, cmd=CMD, timeout=120, on_event=on_event, workdir=workdir))
    await asyncio.wait_for(started.wait(), 60)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    part = workdir / "clip-01.part.mp4"
    size = part.stat().st_size if part.exists() else 0
    await asyncio.sleep(1.0)
    assert (part.stat().st_size if part.exists() else 0) == size  # nothing writes any more
    assert not (workdir / "clip-01.mp4").exists()
