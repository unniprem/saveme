"""Face analysis and tracked framing with the real FFmpeg and YuNet: detections, tracks, scene cuts and
mouth motion on videos of real (public-domain) faces; face, speaker and split renders checked by
finding the faces again in the rendered frames; and the planner's smoothing, bounds and fallbacks."""
import hashlib
import importlib.util
import json
import math

import numpy as np
import pytest

from app import faces, renderer
from app.errors import AppError
from app.faces import (MODEL_PATH, MODEL_SHA256, _deadzone, _smooth, decode, find_cuts, load_analysis,
                       plan_face_layout, plan_split_layout, tracker_for)
from app.layouts import LayoutRequest
from app.render_graph import CropLayout, SplitLayout
from tests.support.events import listing
from tests.support.faces import FIXTURES, faces_in
from tests.support.procs import run_worker_proc
from tests.support.render import as_source, frame_rgb, render_request


def analyze(face_media, tmp_path, name: str, end: float, *, fps: float = 5) -> tuple[dict, object]:
    src = as_source(face_media[name], tmp_path / "source")
    work = tmp_path / "analysis"
    work.mkdir(exist_ok=True)
    events: list[dict] = []
    faces.run_analyze({"source": str(src), "workdir": str(work), "start": 0.0, "end": end, "fps": fps,
                       "threads": 2, "output": "a.json"}, events.append)
    return json.loads((work / "a.json").read_text()), src


# --- the model -------------------------------------------------------------------------------------

def test_the_vendored_model_is_the_pinned_one():
    assert hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() == MODEL_SHA256
    assert (MODEL_PATH.parent / "LICENSE-yunet.txt").read_text().startswith("MIT License")
    assert faces.model_ok()


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV isn't installed (dev-only cross-check)")
def test_decoding_matches_opencv_facedetectoryn():
    import cv2

    image = cv2.imread(str(FIXTURES / "jemison.jpg"))
    padded = np.zeros((640, 640, 3), np.uint8)
    small = cv2.resize(image, (512, 640))
    padded[:, :512] = small
    det = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (640, 640), 0.6, 0.3, 50)
    _, theirs = det.detect(padded)
    ours = faces.Detector(1).detect(padded)
    assert len(ours) == len(theirs) == 1
    assert np.allclose(ours[0][1], theirs[0][:4], atol=1.0) and abs(ours[0][0] - theirs[0][-1]) < 0.01


def test_decode_keeps_confident_boxes_and_suppresses_overlaps():
    outputs = {}
    for stride in (8, 16, 32):
        n = (640 // stride) ** 2
        outputs[f"cls_{stride}"] = np.zeros((1, n, 1), np.float32)
        outputs[f"obj_{stride}"] = np.zeros((1, n, 1), np.float32)
        outputs[f"bbox_{stride}"] = np.zeros((1, n, 4), np.float32)
        outputs[f"kps_{stride}"] = np.zeros((1, n, 10), np.float32)
    cols = 640 // 32
    for idx, score in ((cols * 5 + 7, 0.9), (cols * 5 + 8, 0.8), (cols * 12 + 2, 0.3)):
        outputs["cls_32"][0, idx, 0] = score
        outputs["obj_32"][0, idx, 0] = 1.0
        outputs["bbox_32"][0, idx] = [0.5, 0.5, math.log(2), math.log(2)]  # a 64-px box centred in the cell
    found = decode(outputs)
    assert len(found) == 1  # the 0.8 overlaps the 0.9 (IoU 0.33); the 0.3 scores 0.55, under 0.6
    score, box, points = found[0]
    assert score == pytest.approx(math.sqrt(0.9)) and box == pytest.approx([7.5 * 32 - 32, 5.5 * 32 - 32, 64, 64])
    assert len(points) == 10


# --- analysis --------------------------------------------------------------------------------------

def test_one_moving_face_is_one_track(face_media, tmp_path):
    data, _src = analyze(face_media, tmp_path, "moving_face.mp4", 5.0)
    assert (data["width"], data["height"], data["fps"]) == (1280, 720, 5)
    assert len(data["samples"]) == 25 and data["cuts"] == []
    assert all(len(s["faces"]) == 1 for s in data["samples"])
    assert len(data["tracks"]) == 1 and data["tracks"][0]["samples"] == 25
    xs = [s["faces"][0]["x"] for s in data["samples"]]
    assert xs == sorted(xs) and xs[-1] - xs[0] > 600  # it crosses the frame
    face = data["samples"][0]["faces"][0]
    assert 0.8 <= face["score"] <= 1 and 30 <= face["w"] <= 160 and len(face["landmarks"]) == 5


def test_a_video_without_faces_has_none(render_media, tmp_path):
    src = as_source(render_media["thirds.mp4"], tmp_path / "source")
    work = tmp_path / "a"
    work.mkdir()
    result = faces.run_analyze({"source": str(src), "workdir": str(work), "start": 1.0, "end": 3.0, "fps": 5,
                                "threads": 1, "output": "a.json"}, lambda e: None)
    assert result == {"file": "a.json", "samples": 10, "faces": 0, "tracks": 0, "cuts": 0}
    assert listing(work) == ["a.json"]


def test_a_scene_cut_is_found_and_ends_tracks(face_media, tmp_path):
    data, _src = analyze(face_media, tmp_path, "cut.mp4", 4.0)
    assert data["cuts"] == [2.0]
    before = {f["track"] for s in data["samples"] if s["t"] < 2 for f in s["faces"]}
    after = {f["track"] for s in data["samples"] if s["t"] >= 2 for f in s["faces"]}
    assert before and after and not before & after


def test_mouth_motion_follows_the_moving_mouth(face_media, tmp_path):
    data, _src = analyze(face_media, tmp_path, "speakers.mp4", 7.0)
    assert len(data["tracks"]) == 2

    def motion(side: str, lo: float, hi: float) -> float:
        values = [f["mouth"] for s in data["samples"] if lo <= s["t"] < hi for f in s["faces"]
                  if f["mouth"] is not None and (f["x"] < 640) == (side == "left")]
        return sum(values) / len(values)

    assert motion("left", 0.4, 3.4) > 5 * motion("right", 0.4, 3.4)
    assert motion("right", 3.8, 7.0) > 5 * motion("left", 3.8, 7.0)


def test_analysis_progress_is_measured_and_requests_are_checked(face_media, tmp_path):
    src = as_source(face_media["moving_face.mp4"], tmp_path / "source")
    work = tmp_path / "a"
    work.mkdir()
    events: list[dict] = []
    faces.run_analyze({"source": str(src), "workdir": str(work), "start": 0.0, "end": 2.0, "fps": 5, "threads": 1,
                       "output": "a.json"}, events.append)
    percents = [e["percent"] for e in events]
    assert percents == sorted(percents) and percents[-1] == 100.0 and all(e["stage"] == "analyzing" for e in events)
    for bad in ({"fps": 30}, {"output": "../a.json"}, {"threads": 0}, {"start": -1}):
        req = {"source": str(src), "workdir": str(work), "start": 0.0, "end": 2.0, "fps": 5, "threads": 1,
               "output": "b.json", **bad}
        with pytest.raises(AppError):
            faces.run_analyze(req, lambda e: None)


def test_the_worker_runs_analyses(face_media, tmp_path):
    src = as_source(face_media["two_people.mp4"], tmp_path / "source")
    work = tmp_path / "a"
    work.mkdir()
    events, rc = run_worker_proc({"mode": "analyze", "source": str(src), "workdir": str(work), "start": 0.0,
                                  "end": 2.0, "fps": 5, "threads": 1, "output": "a.json"},
                                 entry="app.renderer", timeout=120)
    assert rc == 0 and events[-1]["data"]["tracks"] == 2 and events[-1]["data"]["faces"] == 20


# --- tracked renders ---------------------------------------------------------------------------------

def _render(src, tmp_path, analysis_dir, mode: str, *, end: float, speech=(), name="clip.mp4", **layout):
    work = tmp_path / f"render-{mode}-{name[:-4]}"
    work.mkdir()
    result = renderer.run_render(render_request(src, work, output=name, end=end, width=270, height=480,
                                                layout={"mode": mode, "fallback": "center", **layout},
                                                analysis=str(analysis_dir / "a.json"), speech=list(speech)),
                                 lambda e: None)
    return work / name, result


def _face_x(frame) -> float | None:
    found = faces_in(frame)
    if not found:
        return None
    x, _y, w, _h = max(found, key=lambda b: b[2] * b[3])
    return (x + w / 2) / frame.shape[1]


def test_face_mode_keeps_a_moving_face_in_frame(face_media, tmp_path):
    data, src = analyze(face_media, tmp_path, "moving_face.mp4", 5.0)
    out, result = _render(src, tmp_path, tmp_path / "analysis", "face", end=5.0)
    assert result["mode"] == "face"
    for t in (0.3, 1.5, 2.5, 3.5, 4.7):
        x = _face_x(frame_rgb(out, t))
        assert x is not None and 0.2 <= x <= 0.8, (t, x)  # inside the window, clear of the edges


def test_face_mode_paths_are_smooth_and_inside_the_frame(face_media, tmp_path):
    data, _src = analyze(face_media, tmp_path, "moving_face.mp4", 5.0)
    layout = plan_face_layout(data, mode="face", src_w=1280, src_h=720, out_w=1080, out_h=1920, margin=0.08,
                              deadzone=0.3)
    plan = layout.crop
    assert (plan.width, plan.height) == (404, 720) and plan.y.is_still
    values = [plan.x.at(t / 25) for t in range(0, 126)]
    assert min(values) >= 0 and max(values) <= 1280 - 404
    steps = [abs(b - a) for a, b in zip(values, values[1:])]
    assert max(steps) <= 0.6 * 404 / 25 * 1.5  # about the speed cap, per frame (the keep-in clamp may add a little)
    assert values[-1] > values[0] + 400  # it followed the face across


def test_a_scene_cut_jumps_the_window_exactly_at_the_cut(face_media, tmp_path):
    data, src = analyze(face_media, tmp_path, "cut.mp4", 4.0)
    layout = plan_face_layout(data, mode="face", src_w=1280, src_h=720, out_w=1080, out_h=1920, margin=0.08,
                              deadzone=0.3)
    path = layout.crop.x
    assert path.at(1.99) < 300 and path.at(2.0) > 700  # left scene, then the right one, no glide between
    out, _result = _render(src, tmp_path, tmp_path / "analysis", "face", end=4.0)
    for t in (1.5, 2.1, 3.5):
        x = _face_x(frame_rgb(out, t))
        assert x is not None and 0.2 <= x <= 0.8, (t, x)


def test_speaker_focus_follows_the_moving_mouth(face_media, tmp_path):
    data, src = analyze(face_media, tmp_path, "speakers.mp4", 7.0)
    out, result = _render(src, tmp_path, tmp_path / "analysis", "speaker", end=7.0, speech=[(0.0, 7.0)])
    assert result["mode"] == "speaker"
    layout = plan_face_layout(data, mode="speaker", src_w=1280, src_h=720, out_w=1080, out_h=1920, margin=0.08,
                              deadzone=0.25, speech=[(0.0, 7.0)])
    left_centre = layout.crop.x.at(2.5) + 202
    right_centre = layout.crop.x.at(6.5) + 202
    assert left_centre < 560 and right_centre > 720  # the left person first, the right one after 3.5 s
    assert _face_x(frame_rgb(out, 2.5)) is not None and _face_x(frame_rgb(out, 6.5)) is not None


def test_without_speech_speaker_focus_keeps_one_framing(face_media, tmp_path):
    data, _src = analyze(face_media, tmp_path, "speakers.mp4", 7.0)
    layout = plan_face_layout(data, mode="speaker", src_w=1280, src_h=720, out_w=1080, out_h=1920, margin=0.08,
                              deadzone=0.25, speech=[])
    xs = [layout.crop.x.at(t / 5) for t in range(35)]
    assert max(xs) - min(xs) < 5  # no switching when nobody is speaking


def test_split_puts_each_person_in_their_own_panel(face_media, tmp_path):
    data, src = analyze(face_media, tmp_path, "two_people.mp4", 4.0)
    out, result = _render(src, tmp_path, tmp_path / "analysis", "split", end=4.0)
    assert result["mode"] == "split"
    frame = frame_rgb(out, 2.0)
    top, bottom = frame[:240], frame[240:]
    for panel in (top, bottom):
        x = _face_x(panel)
        assert x is not None and 0.25 <= x <= 0.75, x
    layout = plan_split_layout(data, src_w=1280, src_h=720, out_w=1080, out_h=1920, ratio=0.5, deadzone=0.3)
    assert layout.top.x.at(1) < layout.bottom.x.at(1)  # the left person on top


def test_split_with_one_person_falls_back_to_halves(face_media, tmp_path):
    data, src = analyze(face_media, tmp_path, "moving_face.mp4", 2.0)
    assert plan_split_layout(data, src_w=1280, src_h=720, out_w=1080, out_h=1920, ratio=0.5, deadzone=0.3) is None
    _out, result = _render(src, tmp_path, tmp_path / "analysis", "split", end=2.0)
    assert result["mode"] == "split"  # the halves layout, from layouts.halves_split


def test_face_mode_without_faces_uses_the_fallback(render_media, tmp_path):
    src = as_source(render_media["thirds.mp4"], tmp_path / "source")
    work = tmp_path / "analysis"
    work.mkdir()
    faces.run_analyze({"source": str(src), "workdir": str(work), "start": 0.0, "end": 2.0, "fps": 5, "threads": 1,
                       "output": "a.json"}, lambda e: None)
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    result = renderer.run_render(render_request(src, render_dir, layout={"mode": "face", "fallback": "blur"},
                                                analysis=str(work / "a.json")), lambda e: None)
    assert result["mode"] == "blur"


# --- the planner on synthetic analyses ----------------------------------------------------------------

def _analysis(boxes_per_sample, *, cuts=(), fps=5.0, width=1920, height=1080):
    samples = []
    for i, boxes in enumerate(boxes_per_sample):
        samples.append({"t": round(i / fps, 3), "faces": [
            {"x": x, "y": y, "w": w, "h": h, "score": 0.9, "track": tid, "mouth": None, "landmarks": []}
            for tid, (x, y, w, h) in boxes]})
    counts: dict[int, int] = {}
    for boxes in boxes_per_sample:
        for tid, _b in boxes:
            counts[tid] = counts.get(tid, 0) + 1
    return {"version": 1, "width": width, "height": height, "fps": fps, "duration": len(samples) / fps,
            "samples": samples, "cuts": list(cuts),
            "tracks": [{"id": k, "samples": v, "mean_area": 1.0} for k, v in counts.items()]}


def test_a_still_face_gives_a_still_window_centred_on_it():
    data = _analysis([[(0, (1200, 300, 200, 260))]] * 20)
    plan = plan_face_layout(data, mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920, margin=0.08,
                            deadzone=0.3).crop
    assert plan.x.is_still and abs(plan.x.at(0) + plan.width / 2 - 1300) < 2


def test_small_jitter_stays_inside_the_dead_zone():
    jitter = [[(0, (1000 + (15 if i % 2 else -15), 300, 200, 260))] for i in range(30)]
    plan = plan_face_layout(_analysis(jitter), mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920,
                            margin=0.08, deadzone=0.3).crop
    xs = [plan.x.at(t / 5) for t in range(30)]
    assert max(xs) - min(xs) < 1


def test_faces_that_fit_are_framed_together_and_wide_ones_pick_one():
    near = [[(0, (800, 300, 100, 130)), (1, (1000, 300, 100, 130))]] * 10  # 300 px wide group: fits 606
    plan = plan_face_layout(_analysis(near), mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920,
                            margin=0.08, deadzone=0.3).crop
    assert abs(plan.x.at(0) + plan.width / 2 - 950) < 2
    far = [[(0, (100, 300, 220, 280)), (1, (1500, 300, 120, 150))]] * 10  # can't both fit: the bigger one
    plan = plan_face_layout(_analysis(far), mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920,
                            margin=0.08, deadzone=0.3).crop
    assert plan.x.at(1) + plan.width / 2 < 600


def test_too_few_faces_means_no_tracked_layout():
    data = _analysis([[(0, (800, 300, 100, 130))]] * 2 + [[]] * 18)
    assert plan_face_layout(data, mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920, margin=0.08,
                            deadzone=0.3) is None
    assert plan_face_layout(_analysis([]), mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920,
                            margin=0.08, deadzone=0.3) is None


def test_a_lost_face_is_held_then_eased_to_the_centre():
    boxes = [[(0, (100, 300, 200, 260))]] * 10 + [[]] * 15
    plan = plan_face_layout(_analysis(boxes), mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920,
                            margin=0.08, deadzone=0.0).crop
    assert plan.x.at(2.5) == plan.x.at(1.0)  # held for 1.5 s after the face was lost at 2.0
    assert plan.x.at(4.8) + plan.width / 2 > 600  # then eased towards the middle (960), not a jump
    steps = [abs(plan.x.at((t + 1) / 25) - plan.x.at(t / 25)) for t in range(125)]
    assert max(steps) <= 0.6 * plan.width / 25 + 1.0


def test_smoothing_helpers():
    assert _deadzone([0, 5, 9, 20, 20, 0], 10) == [0, 0, 0, 10, 10, 10]
    out = _smooth([0.0] * 5 + [1000.0] * 20, fps=5, window_s=0.8, max_speed=100, max_accel=200)
    assert all(abs(b - a) <= 100 / 5 + 1e-6 for a, b in zip(out, out[1:]))
    assert _smooth([1.0, 2.0], fps=5, window_s=0.8, max_speed=1, max_accel=1) == [1.0, 2.0]


def test_find_cuts_on_tiny_frames():
    frames = np.zeros((20, 36, 64), np.uint8)
    frames[10:] = 200
    frames[:, :, :8] = np.arange(20)[:, None, None]  # slow drift elsewhere is not a cut
    assert find_cuts(frames, 25.0) == [0.4]
    assert find_cuts(frames[:2], 25.0) == []


def test_tracker_refuses_a_stale_or_mismatched_analysis(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps(_analysis([[(0, (800, 300, 100, 130))]] * 10)))
    req = LayoutRequest(mode="face")
    assert isinstance(tracker_for(path)(req, 1920, 1080, 1080, 1920), CropLayout)
    assert tracker_for(path)(req, 1280, 720, 1080, 1920) is None  # analysed at another size
    path.write_text(json.dumps({"version": 99}))
    assert load_analysis(path) is None and tracker_for(path)(req, 1920, 1080, 1080, 1920) is None
    two = tmp_path / "two.json"
    two.write_text(json.dumps(_analysis([[(0, (300, 300, 100, 130)), (1, (1400, 300, 100, 130))]] * 10)))
    assert isinstance(tracker_for(two)(LayoutRequest(mode="split"), 1920, 1080, 1080, 1920), SplitLayout)


def test_a_one_sample_subject_flip_doesnt_crash():
    # Two people too far apart to frame together; the detector misses one of them for one sample, so the
    # framing switches subject for a single sample (it used to raise TypeError in _at).
    far = [(0, (100, 300, 220, 280)), (1, (1500, 300, 220, 280))]
    boxes = [far] * 10 + [[far[1]]] + [far] * 10
    for mode in ("face", "speaker"):
        layout = plan_face_layout(_analysis(boxes), mode=mode, src_w=1920, src_h=1080, out_w=1080, out_h=1920,
                                  margin=0.08, deadzone=0.3, speech=[(0.0, 5.0)])
        values = [layout.crop.x.at(t / 10) for t in range(0, 45)]
        assert all(0 <= v <= 1920 - 606 for v in values)
    last = [far] * 10 + [[far[1]]]  # a switch on the scene's very last sample
    assert plan_face_layout(_analysis(last), mode="face", src_w=1920, src_h=1080, out_w=1080, out_h=1920,
                            margin=0.08, deadzone=0.3) is not None
