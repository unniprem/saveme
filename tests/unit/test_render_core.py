"""Pure parts of the Auto Shorts renderer: settings, FFmpeg capability parsing, encoder choice and
arguments, geometry, crop paths and their FFmpeg expressions, layouts and the filtergraph."""
import math

import pytest

from app.config import ConfigError, RenderSettings, load_render_settings
from app.encoders import (HARDWARE, QUALITIES, SOFTWARE, Capabilities, audio_args, bitrate_cap_bytes_per_second,
                          choose_encoder, parse_encoders, parse_filters, scaled_size, video_args)
from app.layouts import LayoutRequest, Manual, plan_layout, request_from
from app.reframe import (Path, centred_offset, crop_window, even, fit_size, nearly_same_aspect, path_expr,
                         simplify, source_canvas)
from app.render_graph import (Bumper, CropLayout, CropPlan, FitLayout, GraphPlan, LogoPlan, SplitLayout,
                              build)

# --- settings --------------------------------------------------------------------------------


def test_render_settings_defaults():
    assert load_render_settings({}) == RenderSettings(
        render_max_concurrent_jobs=1, render_max_queued_jobs=4, render_cpu_threads=2, render_max_memory_mb=1536,
        render_encoder="auto", render_max_clip_seconds=300, render_max_job_seconds=3600,
        render_analysis_fps=5, render_fonts_dir=None)


def test_render_settings_are_read():
    s = load_render_settings({"RENDER_MAX_CONCURRENT_JOBS": "2", "RENDER_ENCODER": "LIBX264",
                              "RENDER_FONTS_DIR": "/usr/share/fonts/saveme", "RENDER_CPU_THREADS": "4"})
    assert (s.render_max_concurrent_jobs, s.render_encoder, s.render_cpu_threads) == (2, "libx264", 4)
    assert str(s.render_fonts_dir) == "/usr/share/fonts/saveme" and s.max_memory_bytes == 1536 * 1024 * 1024


@pytest.mark.parametrize("var,value", [
    ("RENDER_MAX_CONCURRENT_JOBS", "0"), ("RENDER_MAX_CONCURRENT_JOBS", "5"), ("RENDER_MAX_QUEUED_JOBS", "21"),
    ("RENDER_CPU_THREADS", "17"), ("RENDER_MAX_MEMORY_MB", "100"), ("RENDER_ENCODER", "nvenc"),
    ("RENDER_MAX_CLIP_SECONDS", "4"), ("RENDER_MAX_JOB_SECONDS", "59"), ("RENDER_ANALYSIS_FPS", "16"),
    ("RENDER_FONTS_DIR", "fonts"), ("RENDER_FONTS_DIR", "/a b"), ("RENDER_FONTS_DIR", "/a:b"),
    ("RENDER_FONTS_DIR", "/x/../etc"), ("RENDER_FONTS_DIR", "/a'b"),
])
def test_invalid_render_settings_name_the_variable(var, value):
    with pytest.raises(ConfigError) as exc:
        load_render_settings({var: value})
    assert exc.value.var == var


def test_word_timestamps_setting():
    from app.config import load_ai_settings
    assert load_ai_settings({}).whisper_word_timestamps is True
    assert load_ai_settings({"WHISPER_WORD_TIMESTAMPS": "false"}).whisper_word_timestamps is False
    with pytest.raises(ConfigError):
        load_ai_settings({"WHISPER_WORD_TIMESTAMPS": "sometimes"})


# --- capabilities and encoders ---------------------------------------------------------------

ENCODERS = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_videotoolbox    VideoToolbox H.264 Encoder (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
 V....D mjpeg                MJPEG (Motion JPEG)
"""
FILTERS = """Filters:
  T.. = Timeline support
  A = Audio input/output
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
 TSC crop             V->V       Crop the input video.
 .. anullsrc          |->A       Null audio source, return empty audio frames.
 .S vstack            N->V       Stack video inputs vertically.
"""


def test_listings_are_parsed():
    assert parse_encoders(ENCODERS) == {"libx264", "h264_videotoolbox", "aac", "mjpeg"}
    assert parse_filters(FILTERS) == {"ass", "crop", "anullsrc", "vstack"}


def test_missing_components_are_named():
    caps = Capabilities(encoders=frozenset({"aac"}), filters=frozenset({"crop"}))
    assert "an H.264 encoder (libx264)" in caps.missing and "the scale filter" in caps.missing
    assert not caps.captions


def test_the_local_ffmpeg_can_render():
    from app.encoders import detect
    caps = detect()
    assert caps.missing == [] and caps.captions and caps.jpeg


@pytest.mark.parametrize("setting,encoders,want", [
    ("auto", {SOFTWARE, HARDWARE}, HARDWARE), ("auto", {SOFTWARE}, SOFTWARE), ("auto", set(), None),
    ("libx264", {SOFTWARE, HARDWARE}, SOFTWARE), (HARDWARE, {SOFTWARE}, None), (HARDWARE, {HARDWARE}, HARDWARE),
])
def test_encoder_choice(setting, encoders, want):
    assert choose_encoder(setting, Capabilities(frozenset(encoders), frozenset())) == want


def test_x264_arguments_follow_the_preset():
    args = video_args(SOFTWARE, "standard", width=1080, height=1920, fps=30, threads=2)
    joined = " ".join(args)
    assert "-preset veryfast" in joined and "-crf 21" in joined and "-profile:v high" in joined
    assert "-pix_fmt yuv420p" in joined and "-g 60" in joined and "-threads 2" in joined
    assert "-maxrate 12441k" in joined and "-bufsize 24882k" in joined  # 0.2 bits a pixel a frame
    assert "-colorspace bt709" in joined
    assert "-preset medium -crf 18" in " ".join(video_args(SOFTWARE, "high", width=1080, height=1920, fps=30,
                                                            threads=2))


def test_videotoolbox_arguments_use_a_bitrate():
    joined = " ".join(video_args(HARDWARE, "standard", width=1080, height=1920, fps=30, threads=2))
    assert "-c:v h264_videotoolbox" in joined and "-b:v 9953k" in joined and "-crf" not in joined


def test_audio_and_size_helpers():
    assert audio_args("high") == ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    assert scaled_size(1080, 1920, "draft") == (540, 960) and scaled_size(1080, 1350, "draft") == (540, 674)
    assert scaled_size(1080, 1920, "high") == (1080, 1920)
    assert bitrate_cap_bytes_per_second("standard", 1080, 1920, 30) == (12441 + 160) * 1000 // 8
    assert set(QUALITIES) == {"draft", "standard", "high"}


# --- geometry --------------------------------------------------------------------------------

@pytest.mark.parametrize("src,out,want", [
    ((1920, 1080), (1080, 1920), (606, 1080)), ((1920, 1080), (1080, 1080), (1080, 1080)),
    ((1920, 1080), (1080, 1350), (864, 1080)), ((1920, 1080), (1920, 1080), (1920, 1080)),
    ((1080, 1920), (1920, 1080), (1080, 606)), ((1280, 720), (1080, 1920), (404, 720)),
    ((1081, 721), (1080, 1920), (404, 720)),
])
def test_crop_window_is_the_largest_even_window_of_the_aspect(src, out, want):
    w, h = crop_window(*src, *out)
    assert (w, h) == want and w % 2 == 0 and h % 2 == 0 and w <= src[0] and h <= src[1]
    assert abs(w / h - out[0] / out[1]) < 0.01


def test_zoom_shrinks_the_window_but_never_grows_it():
    assert crop_window(1920, 1080, 1080, 1920, 2.0) == (302, 540)
    assert crop_window(1920, 1080, 1080, 1920, 0.5) == (606, 1080)


def test_fit_and_canvas_sizes():
    assert fit_size(1920, 1080, 1080, 1920) == (1080, 606)
    assert fit_size(1080, 1920, 1920, 1080) == (606, 1080)
    assert source_canvas(3840, 2160) == (1920, 1080) and source_canvas(641, 361) == (640, 360)
    assert even(3.9) == 2 and even(0) == 2 and even(7) == 6


def test_nearly_same_aspect():
    assert nearly_same_aspect(1080, 1920, 1080, 1920) and nearly_same_aspect(1000, 1920, 1080, 1920)
    assert not nearly_same_aspect(1080, 1350, 1080, 1920) and not nearly_same_aspect(1920, 1080, 1080, 1920)


def test_centred_offset_stays_inside():
    assert centred_offset(1920, 606) == 657.0
    assert centred_offset(1920, 606, 0.0) == 0.0 and centred_offset(1920, 606, 1.0) == 1314.0


# --- paths -----------------------------------------------------------------------------------

def _eval(expr: str, t: float) -> float:
    """Evaluate an FFmpeg expression made by path_expr, in Python."""
    def clip(x, lo, hi):
        return min(max(x, lo), hi)

    def gte(a, b):
        return 1.0 if a >= b else 0.0

    return eval(expr, {"clip": clip, "gte": gte, "t": t})  # noqa: S307 - our own generated text


@pytest.mark.parametrize("points", [
    ((0.0, 100.0),),
    ((0.0, 100.0), (2.0, 300.0)),
    ((0.0, 100.0), (1.0, 100.0), (3.0, 400.5), (5.0, 50.25)),
    ((0.0, 10.0), (1.0, 20.0), (1.0, 500.0), (2.0, 520.0)),  # a cut at 1.0
])
def test_the_expression_equals_the_path(points):
    path = Path(points)
    expr = path_expr(path)
    for k in range(0, 61):
        t = k / 10
        assert _eval(expr, t) == pytest.approx(path.at(t), abs=1e-3), (t, expr)


def test_a_path_holds_outside_its_keyframes_and_jumps_at_cuts():
    path = Path(((1.0, 10.0), (2.0, 20.0), (2.0, 90.0), (3.0, 100.0)))
    assert path.at(0.0) == 10.0 and path.at(1.5) == 15.0 and path.at(1.999) == pytest.approx(19.99)
    assert path.at(2.0) == 90.0 and path.at(9.0) == 100.0
    assert Path.still(5).is_still and not path.is_still


def _depth(expr: str) -> int:
    depth = top = 0
    for ch in expr:
        depth += ch == "("
        top = max(top, depth)
        depth -= ch == ")"
    return top


def test_long_paths_nest_only_logarithmically():
    # FFmpeg evaluates one level per "+" of a flat sum, and a 4,000-term flat sum crashed it (SIGBUS).
    points = tuple((i * 0.2, 500 + 100 * math.sin(i / 7)) for i in range(4000))
    expr = path_expr(Path(points))
    assert "clip(" in expr and expr.count("(") == expr.count(")")
    assert _depth(expr) <= 16  # about log2(4000) levels of sums, plus clip()
    for k in range(0, 800, 37):
        assert _eval(expr, k) == pytest.approx(Path(points).at(k), abs=1e-3)


def test_bounded_simplify_caps_keyframes():
    from app.reframe import MAX_PATH_POINTS, bounded_simplify

    busy = [(i * 0.2, 500 + 300 * math.sin(i)) for i in range(20000)]
    out = bounded_simplify(busy, 1.0)
    assert len(out) <= MAX_PATH_POINTS and out[0] == busy[0] and out[-1] == busy[-1]
    calm = [(i * 0.2, 100.0) for i in range(1000)]
    assert bounded_simplify(calm) == [calm[0], calm[-1]]


def test_simplify_keeps_shape_within_tolerance_and_keeps_cuts():
    pts = [(i / 10, 100 + 0.2 * (i % 2)) for i in range(50)] + [(5.0, 900.0), (6.0, 950.0)]
    pts.insert(50, (5.0, 100.0))
    out = simplify(pts, tolerance=1.0)
    assert len(out) <= 5 and (5.0, 100.0) in out and (5.0, 900.0) in out
    full, short = Path(tuple(pts)), Path(tuple(out))
    assert all(abs(full.at(t / 10) - short.at(t / 10)) <= 1.0 for t in range(60) if t != 50)


# --- layouts ---------------------------------------------------------------------------------

def test_layout_requests_are_checked():
    assert request_from({"mode": "face", "fallback": "blur", "manual": {"x": 0.2, "y": 0.5, "zoom": 1.5}}) == \
        LayoutRequest(mode="face", fallback="blur", manual=Manual(0.2, 0.5, 1.5))
    for bad in ({"mode": "zoom"}, {"mode": "center", "fallback": "split"}, {"mode": "center", "margin": -1},
                {"mode": "center", "manual": {"zoom": 4}}, {"mode": "center", "deadzone": True},
                {"mode": "center", "split_ratio": 0.9}):
        with pytest.raises(ValueError):
            request_from(bad)


def test_plan_layout_modes():
    req = LayoutRequest
    layout, mode = plan_layout(req("center"), 1920, 1080, 1080, 1920)
    assert isinstance(layout, CropLayout) and mode == "center"
    assert (layout.crop.width, layout.crop.height, layout.crop.x.at(0)) == (606, 1080, 657)
    layout, mode = plan_layout(req("blur", fg_position=0.4), 1920, 1080, 1080, 1920)
    assert isinstance(layout, FitLayout) and layout.background == "blur"
    assert (layout.fg_width, layout.fg_height, layout.fg_x, layout.fg_y) == (1080, 606, 0, 464)
    layout, mode = plan_layout(req("original", pad_color="#112233"), 1920, 1080, 1080, 1920)
    assert layout.background == "color" and layout.color == "#112233" and mode == "original"
    layout, mode = plan_layout(req("split"), 1920, 1080, 1080, 1920)
    assert isinstance(layout, SplitLayout) and layout.top_height == 960
    assert layout.top.x.at(0) < 960 <= layout.bottom.x.at(0)
    layout, mode = plan_layout(req("speaker", fallback="original"), 1920, 1080, 1080, 1920)
    assert mode == "original"
    layout, mode = plan_layout(req("face", manual=Manual(0.0, 0.5, 1.0)), 1920, 1080, 1080, 1920)
    assert mode == "manual" and layout.crop.x.at(0) == 0
    layout, mode = plan_layout(req("center"), 1080, 1920, 1080, 1920)  # already 9:16: scaled whole
    assert isinstance(layout, FitLayout) and (layout.fg_width, layout.fg_height) == (1080, 1920)


def test_a_tracker_supplies_face_layouts():
    tracked = CropLayout(CropPlan(606, 1080, Path(((0.0, 0.0), (1.0, 100.0))), Path.still(0)))
    layout, mode = plan_layout(LayoutRequest("face"), 1920, 1080, 1080, 1920, tracker=lambda *a: tracked)
    assert layout is tracked and mode == "face"
    layout, mode = plan_layout(LayoutRequest("face"), 1920, 1080, 1080, 1920, tracker=lambda *a: None)
    assert mode == "center"


# --- filtergraph -----------------------------------------------------------------------------

def _plan(**change) -> GraphPlan:
    base = dict(canvas_width=1080, canvas_height=1920, fps=30.0, duration=10.0,
                layout=CropLayout(CropPlan(606, 1080, Path.still(657), Path.still(0))), source_has_audio=True)
    base.update(change)
    return GraphPlan(**base)


def test_a_plain_graph():
    graph = build(_plan())
    assert graph.splitlines()[0] == "[0:V:0]setsar=1,fps=30:start_time=0[src];"
    assert "crop=w=606:h=1080:x=657:y=0,scale=1080:1920:flags=lanczos,setsar=1[base]" in graph
    assert "[0:a:0]aresample=48000" in graph and "atrim=duration=10" in graph
    assert graph.endswith("[amain]anull[aout]")


def test_a_moving_crop_is_an_expression():
    graph = build(_plan(layout=CropLayout(CropPlan(606, 1080, Path(((0.0, 0.0), (2.0, 200.0))), Path.still(0)))))
    assert "x='(0+100*clip(t-0,0,2))'" in graph


def test_graph_parts():
    graph = build(_plan(subtitles=True, fonts_dir="/usr/share/fonts", source_has_audio=False,
                        logo=LogoPlan(width=160, x=900, y=20, opacity=0.8),
                        intro=Bumper(2.0, True), outro=Bumper(1.5, False), source_sar=(4, 3)))
    assert "scale=trunc(iw*4/3/2)*2:ih,setsar=1" in graph
    assert "ass=filename=subs.ass:shaping=complex:fontsdir=/usr/share/fonts" in graph
    assert "[1:v]format=rgba,scale=160:-2" in graph and "colorchannelmixer=aa=0.8" in graph
    assert "anullsrc=r=48000:cl=stereo,atrim=duration=10" in graph
    assert "[2:V:0]" in graph and "[3:V:0]" in graph and "[2:a:0]" in graph and "[3:a:0]" not in graph
    assert "[introv][introa][vmain][amain][outrov][outroa]concat=n=3:v=1:a=1[vout][aout]" in graph
    assert _plan(intro=Bumper(2.0, True)).total_duration == 12.0


def test_still_graphs_and_layout_graphs():
    still = build(_plan(frame_at=3.5))
    assert "setpts=PTS-STARTPTS+3.5/TB" in still and "fps=" not in still and "aout" not in still
    blur = build(_plan(layout=FitLayout(1080, 606, 0, 656, "blur")))
    assert "boxblur" in blur and "overlay=x=0:y=656[base]" in blur
    pad = build(_plan(layout=FitLayout(1080, 606, 0, 656, "color", "#ff8800")))
    assert "pad=1080:1920:0:656:color=0xFF8800" in pad
    split = build(_plan(layout=SplitLayout(CropPlan(960, 852, Path.still(0), Path.still(0)),
                                           CropPlan(960, 852, Path.still(960), Path.still(0)), 960)))
    assert "vstack=inputs=2[base]" in split and "scale=1080:960" in split


def test_graph_refuses_odd_canvases_and_bad_colours():
    with pytest.raises(ValueError):
        build(_plan(canvas_width=1081))
    with pytest.raises(ValueError):
        build(_plan(layout=FitLayout(1080, 606, 0, 656, "color", "red;x")))
