"""Auto Shorts face analysis and tracked framing (docs/AUTO_SHORTS_DESIGN.md §5).

Runs in the renderer worker. `run_analyze` decodes a clip's range once through the hardened FFmpeg:
frames at RENDER_ANALYSIS_FPS letterboxed into 640×640 BGR for face detection, plus tiny grey frames
at the source's rate for scene cuts. It detects faces with YuNet (OpenCV Zoo's model, MIT, vendored)
on ONNX Runtime, links them into tracks, measures each face's mouth motion, and writes everything as
JSON. `tracker_for` turns that JSON into face, speaker or split layouts: target centres, a dead zone,
smoothing, a speed cap, jumps only at scene cuts, and clamping into the frame.

Speaker focus is a visual heuristic, the face whose mouth moves most while someone is speaking. It
never identifies who is talking.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.errors import AppError
from app.mediafile import FFMPEG, stderr_tail
from app.reframe import Path as CropPath
from app.reframe import bounded_simplify, centred_offset, clamp, crop_window, even

MODEL_PATH = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
ANALYSIS_VERSION = 1
SIZE = 640  # YuNet's input is 640×640
STRIDES = (8, 16, 32)
SCORE_THRESHOLD = 0.6
NMS_IOU = 0.3
MIN_FACE_SHARE = 0.02  # faces smaller than this share of the frame height are ignored
CUT_SIZE = (64, 36)
MAX_TRACK_GAP = 1.0  # seconds a track may go unseen
HOLD_SECONDS = 1.5  # with no face, hold the last framing this long before easing to the fallback
MIN_FACE_SHARE_OF_CLIP = 0.3  # face modes need faces in at least this share of the samples
_EMIT_INTERVAL = 0.25


# --- detection -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Face:
    x: float  # box in source (display) pixels
    y: float
    w: float
    h: float
    score: float
    landmarks: tuple[tuple[float, float], ...]  # right eye, left eye, nose, right mouth, left mouth

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def _session(threads: int):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, threads)
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    return ort.InferenceSession(str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"])


def model_ok() -> bool:
    try:
        return hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() == MODEL_SHA256
    except OSError:
        return False


def _iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def decode(outputs: dict, *, threshold: float = SCORE_THRESHOLD) -> list[tuple[float, list[float], list[float]]]:
    """YuNet's raw outputs (cls/obj/bbox/kps per stride) as (score, [x, y, w, h], [10 landmark coords]) in
    the 640×640 input, after non-maximum suppression. Same decoding as OpenCV's FaceDetectorYN."""
    import numpy as np

    found = []
    for stride in STRIDES:
        cols = rows = SIZE // stride
        cls = np.clip(outputs[f"cls_{stride}"].reshape(-1), 0, 1)
        obj = np.clip(outputs[f"obj_{stride}"].reshape(-1), 0, 1)
        scores = np.sqrt(cls * obj)
        keep = np.flatnonzero(scores >= threshold)
        if not len(keep):
            continue
        bbox = outputs[f"bbox_{stride}"].reshape(-1, 4)[keep]
        kps = outputs[f"kps_{stride}"].reshape(-1, 10)[keep]
        c = (keep % cols).astype(np.float64)
        r = (keep // cols).astype(np.float64)
        cx, cy = (c + bbox[:, 0]) * stride, (r + bbox[:, 1]) * stride
        w, h = np.exp(bbox[:, 2]) * stride, np.exp(bbox[:, 3]) * stride
        for i in range(len(keep)):
            points = []
            for n in range(5):
                points += [float((c[i] + kps[i, 2 * n]) * stride), float((r[i] + kps[i, 2 * n + 1]) * stride)]
            found.append((float(scores[keep[i]]), [float(cx[i] - w[i] / 2), float(cy[i] - h[i] / 2),
                                                   float(w[i]), float(h[i])], points))
    found.sort(key=lambda f: -f[0])
    kept: list = []
    for item in found:
        if all(_iou(item[1], other[1]) <= NMS_IOU for other in kept):
            kept.append(item)
        if len(kept) == 50:
            break
    return kept


class Detector:
    def __init__(self, threads: int = 1):
        self._session = _session(threads)
        self._input = self._session.get_inputs()[0].name
        self._names = [o.name for o in self._session.get_outputs()]

    def detect(self, image) -> list[tuple[float, list[float], list[float]]]:
        """Faces in a 640×640×3 uint8 BGR image (input coordinates)."""
        import numpy as np

        blob = image.astype(np.float32).transpose(2, 0, 1)[None]
        values = self._session.run(None, {self._input: blob})
        return decode(dict(zip(self._names, values)))


# --- sampling --------------------------------------------------------------------------------

def letterbox_size(width: int, height: int) -> tuple[int, int]:
    """The frame's size inside YuNet's 640×640 input (top-left aligned, padded below/right)."""
    if width >= height:
        return SIZE, max(2, even(SIZE * height / width))
    return max(2, even(SIZE * width / height)), SIZE


def _analysis_cmd(source: Path, start: float, length: float, *, width: int, height: int, fps: float,
                  cut_fps: float, threads: int, cuts_name: str) -> list[str]:
    sw, sh = letterbox_size(width, height)
    graph = (f"[0:V:0]setsar=1,split=2[a][b];"
             f"[a]fps={fps}:start_time=0,scale={sw}:{sh}:flags=bilinear,pad={SIZE}:{SIZE}:0:0,format=bgr24[det];"
             f"[b]fps={cut_fps}:start_time=0,scale={CUT_SIZE[0]}:{CUT_SIZE[1]}:flags=area,format=gray[cut]")
    return [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", str(threads),
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(source), "-filter_complex", graph,
            "-filter_complex_threads", str(threads), "-map", "[det]", "-f", "rawvideo", "pipe:1",
            "-map", "[cut]", "-f", "rawvideo", cuts_name]


def find_cuts(frames, fps: float) -> list[float]:
    """Scene cuts in a run of tiny grey frames: a frame whose mean absolute difference from the one
    before is large in itself (> 30 levels) and against its neighbourhood (> 3× the local median)."""
    import numpy as np

    if len(frames) < 3:
        return []
    diffs = np.abs(np.diff(frames.astype(np.int16), axis=0)).mean(axis=(1, 2))
    cuts = []
    for i, d in enumerate(diffs):
        lo, hi = max(0, i - 6), min(len(diffs), i + 7)
        local = np.median(np.concatenate([diffs[lo:i], diffs[i + 1:hi]])) if hi - lo > 1 else 0.0
        if d > 30 and d > 3 * max(local, 2.0):
            t = (i + 1) / fps
            if not cuts or t - cuts[-1] >= 0.5:
                cuts.append(round(t, 3))
    return cuts


def mouth_patch(gray, face: Face, scale: float):
    """A 16×24 grey patch of the mouth (between the mouth corners, from below the nose), sampled by
    nearest neighbour from the 640 frame. None when the landmarks are unusable."""
    import numpy as np

    (_rx, _ry), (_lx, _ly), (nx, ny), (mrx, mry), (mlx, mly) = [(x / scale, y / scale) for x, y in face.landmarks]
    x0, x1 = min(mrx, mlx), max(mrx, mlx)
    span = x1 - x0
    if span < 3:
        return None
    y0 = (ny + min(mry, mly)) / 2
    y1 = max(mry, mly) + span * 0.45
    x0, x1 = x0 - span * 0.15, x1 + span * 0.15
    h, w = gray.shape
    ys = np.clip(np.linspace(y0, y1, 16), 0, h - 1).astype(int)
    xs = np.clip(np.linspace(x0, x1, 24), 0, w - 1).astype(int)
    return gray[ys[:, None], xs[None, :]].astype(np.float32)


# --- tracks ----------------------------------------------------------------------------------

@dataclass
class _Track:
    id: int
    last_t: float
    box: list[float]
    count: int = 0
    area: float = 0.0
    patch: object = None


def _link(samples: list[dict], cuts: Sequence[float]) -> list[dict]:
    """Give each face a track id: matched to the best overlapping (or nearest) face seen within
    MAX_TRACK_GAP, never across a scene cut."""
    tracks: list[_Track] = []
    next_id = 0
    cut_iter = list(cuts)
    for sample in samples:
        t = sample["t"]
        while cut_iter and cut_iter[0] <= t:
            cut = cut_iter.pop(0)
            for tr in tracks:
                if tr.last_t < cut:
                    tr.last_t = -math.inf  # ended by the cut
        live = [tr for tr in tracks if t - tr.last_t <= MAX_TRACK_GAP]
        pairs = []
        for i, face in enumerate(sample["faces"]):
            box = [face["x"], face["y"], face["w"], face["h"]]
            for tr in live:
                iou = _iou(box, tr.box)
                dist = math.hypot(box[0] + box[2] / 2 - tr.box[0] - tr.box[2] / 2,
                                  box[1] + box[3] / 2 - tr.box[1] - tr.box[3] / 2) / max(box[2], tr.box[2], 1)
                if iou > 0.2 or dist < 0.6:
                    pairs.append((iou - dist, i, tr))
        pairs.sort(key=lambda p: -p[0])
        used_faces, used_tracks = set(), set()
        for _score, i, tr in pairs:
            if i in used_faces or tr.id in used_tracks:
                continue
            used_faces.add(i)
            used_tracks.add(tr.id)
            sample["faces"][i]["track"] = tr.id
        for i, face in enumerate(sample["faces"]):
            if "track" not in face:
                tracks.append(_Track(id=next_id, last_t=t, box=[face["x"], face["y"], face["w"], face["h"]]))
                face["track"] = next_id
                next_id += 1
            tr = next(tr for tr in tracks if tr.id == face["track"])
            tr.last_t, tr.box = t, [face["x"], face["y"], face["w"], face["h"]]
            tr.count += 1
            tr.area += face["w"] * face["h"]
    return [{"id": tr.id, "samples": tr.count, "mean_area": round(tr.area / max(tr.count, 1), 1)} for tr in tracks]


# --- the worker mode ---------------------------------------------------------------------------

def analyze(source: Path, workdir: Path, *, start: float, end: float, width: int, height: int,
            source_fps: float | None, fps: float, threads: int,
            on_progress: Callable[[float], None] = lambda p: None) -> dict:
    """Decode [start, end) once and return the analysis (faces per sample with tracks and mouth
    motion, scene cuts) in source display pixels and clip time."""
    import numpy as np

    length = end - start
    cut_fps = min(60.0, round(source_fps or 30.0, 3))
    sw, sh = letterbox_size(width, height)
    scale = sw / width  # input pixels per source pixel
    cuts_name = "cuts.gray"
    cmd = _analysis_cmd(source, start, length, width=width, height=height, fps=fps, cut_fps=cut_fps,
                        threads=threads, cuts_name=cuts_name)
    detector = Detector(threads)
    frame_bytes = SIZE * SIZE * 3
    expected = max(1, math.ceil(length * fps))
    samples: list[dict] = []
    patches: dict[int, object] = {}
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                    cwd=str(workdir))
        except OSError as exc:
            print(f"faces: ffmpeg failed to start: {exc!r}", file=sys.stderr)
            raise AppError("render_failed") from None
        with proc:
            k = 0
            while True:
                data = proc.stdout.read(frame_bytes)
                if len(data) < frame_bytes:
                    break
                image = np.frombuffer(data, np.uint8).reshape(SIZE, SIZE, 3)
                gray = image[:sh, :sw].mean(axis=2)
                faces = []
                for score, box, points in detector.detect(image):
                    x, y, w, h = (v / scale for v in box)
                    if h < MIN_FACE_SHARE * height or w <= 0:
                        continue
                    lm = tuple((points[2 * n] / scale, points[2 * n + 1] / scale) for n in range(5))
                    face = Face(x=x, y=y, w=w, h=h, score=score, landmarks=lm)
                    patch = mouth_patch(gray, Face(*box, score, tuple((points[2 * n], points[2 * n + 1])
                                                                       for n in range(5))), 1.0)
                    faces.append({"x": round(x, 1), "y": round(y, 1), "w": round(w, 1), "h": round(h, 1),
                                  "score": round(score, 3), "landmarks": [[round(a, 1), round(b, 1)] for a, b in lm],
                                  "_patch": patch, "cx": face.cx, "cy": face.cy})
                samples.append({"t": round(k / fps, 3), "faces": faces})
                k += 1
                on_progress(min(99.0, 100.0 * k / expected))
        errors.seek(0)
        tail = stderr_tail(errors.read())
    if proc.returncode != 0:
        print(f"faces: analysis decode failed (rc={proc.returncode}): {tail}", file=sys.stderr)
        raise AppError("render_failed")
    cut_path = workdir / cuts_name
    raw = cut_path.read_bytes() if cut_path.is_file() else b""
    cut_path.unlink(missing_ok=True)
    per = CUT_SIZE[0] * CUT_SIZE[1]
    grey = np.frombuffer(raw[:len(raw) // per * per], np.uint8).reshape(-1, CUT_SIZE[1], CUT_SIZE[0])
    cuts = find_cuts(grey, cut_fps)
    tracks = _link(samples, cuts)
    # Mouth motion: the change of each track's mouth patch since its previous sample, per face size.
    for sample in samples:
        for face in sample["faces"]:
            patch = face.pop("_patch")
            previous = patches.get(face["track"])
            motion = None
            if patch is not None and previous is not None:
                motion = float(np.abs(patch - previous).mean())
            if patch is not None:
                patches[face["track"]] = patch
            face["mouth"] = None if motion is None else round(motion, 2)
            face.pop("cx")
            face.pop("cy")
    on_progress(100.0)
    return {"version": ANALYSIS_VERSION, "width": width, "height": height, "fps": fps, "duration": round(length, 3),
            "samples": samples, "cuts": cuts, "tracks": tracks}


def run_analyze(req: dict, emit: Callable[[dict], None]) -> dict:
    """Worker mode `analyze`: {source, workdir, start, end, fps, threads, output}. Writes the analysis
    JSON into the workdir (output name) and returns a summary."""
    from app.renderer import _abs_dir, _abs_file, _number, _plain_name, probe_video
    from app.worker import _clear

    try:
        workdir = _abs_dir(req.get("workdir"))
        source = _abs_file(req.get("source"))
        start = _number(req.get("start"), 0, 1e7)
        end = _number(req.get("end"), start + 0.05, 1e7)
        fps = _number(req.get("fps"), 1, 15)
        threads = req.get("threads")
        if type(threads) is not int or not 1 <= threads <= 16:
            raise ValueError
        output = _plain_name(req.get("output"), ".json")
    except (ValueError, TypeError):
        print("faces: malformed analyze request", file=sys.stderr)
        raise AppError("internal") from None
    if not model_ok():
        print("faces: the YuNet model is missing or changed", file=sys.stderr)
        raise AppError("render_failed", "The face detection model is missing from this installation.")
    info = probe_video(source)
    if info is None:
        raise AppError("unreadable_media")
    width = info.width * info.sar[0] // info.sar[1]
    last = [0.0]

    def progress(percent: float) -> None:
        now = time.monotonic()
        if percent >= 100.0 or now - last[0] >= _EMIT_INTERVAL:
            last[0] = now
            emit({"event": "progress", "stage": "analyzing", "percent": round(percent, 1)})

    try:
        result = analyze(source, workdir, start=start, end=min(end, start + (info.duration or end) - start),
                         width=width, height=info.height, source_fps=info.fps, fps=fps, threads=threads,
                         on_progress=progress)
        tmp = workdir / (output + ".tmp")
        tmp.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, workdir / output)
    except BaseException:
        _clear(workdir)
        raise
    _clear(workdir, keep=output)
    faces = sum(len(s["faces"]) for s in result["samples"])
    return {"file": output, "samples": len(result["samples"]), "faces": faces, "tracks": len(result["tracks"]),
            "cuts": len(result["cuts"])}


def load_analysis(path: Path) -> dict | None:
    """An analysis file, or None if it is missing, too large or not one this version wrote."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != ANALYSIS_VERSION:
        return None
    return data


# --- framing ---------------------------------------------------------------------------------

MAX_SPEED = 0.6  # window widths (or heights) a second
TRANSITION_SECONDS = 0.6  # an eased move from one subject to another
GROUP = -1  # the subject id when every face is framed together


@dataclass
class _Target:
    t: float
    x: float | None  # the window centre wanted, in source pixels; None = no face here
    y: float | None
    subject: int | None = None  # the track followed, GROUP, or None
    box: tuple[float, float, float, float] | None = None  # what must stay in frame: x0, y0, x1, y1


def _segments(times: Sequence[float], cuts: Sequence[float]) -> list[list[int]]:
    """Sample indices split at scene cuts."""
    out, current, cut_list = [], [], list(cuts)
    for i, t in enumerate(times):
        while cut_list and cut_list[0] <= t:
            cut_list.pop(0)
            if current:
                out.append(current)
                current = []
        current.append(i)
    if current:
        out.append(current)
    return out


def _deadzone(values: list[float], zone: float) -> list[float]:
    """The camera only moves when the target leaves the central zone, and then just enough to keep
    it at the zone's edge."""
    out, cam = [], values[0]
    for v in values:
        if v - cam > zone:
            cam = v - zone
        elif cam - v > zone:
            cam = v + zone
        out.append(cam)
    return out


def _smooth(values: list[float], *, fps: float, window_s: float, max_speed: float, max_accel: float) -> list[float]:
    """Median (removes single-sample jumps), centred moving average, then speed and acceleration caps."""
    n = len(values)
    if n <= 2:
        return list(values)
    med = []
    for i in range(n):
        lo, hi = max(0, i - 2), min(n, i + 3)
        med.append(sorted(values[lo:hi])[(hi - lo) // 2])
    half = max(1, int(window_s * fps / 2))
    avg = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        avg.append(sum(med[lo:hi]) / (hi - lo))
    dt = 1.0 / fps
    out, velocity = [avg[0]], 0.0
    for v in avg[1:]:
        want = (v - out[-1]) / dt
        want = clamp(want, velocity - max_accel * dt, velocity + max_accel * dt)
        velocity = clamp(want, -max_speed, max_speed)
        out.append(out[-1] + velocity * dt)
    return out


def _fill(targets: list[_Target], fallback_x: float, fallback_y: float, fps: float) -> list[_Target]:
    """Targets with the gaps filled: hold the last framing for HOLD_SECONDS, then the fallback (the
    smoothing eases there); a gap at the start takes the first framing seen. Filled targets keep no box."""
    first = next((t for t in targets if t.x is not None), None)
    last = first or _Target(0.0, fallback_x, fallback_y)
    out, missing = [], 0
    for tgt in targets:
        if tgt.x is not None:
            last, missing = tgt, 0
            out.append(tgt)
            continue
        missing += 1
        if missing / fps <= HOLD_SECONDS:
            out.append(_Target(tgt.t, last.x, last.y, last.subject))
        else:
            out.append(_Target(tgt.t, fallback_x, fallback_y, None))
    return out


def _smoothstep(u: float) -> float:
    u = clamp(u, 0.0, 1.0)
    return u * u * (3 - 2 * u)


def _shot(indices, times, targets, *, src_w, src_h, win_w, win_h, fps, deadzone, margin) -> list[tuple[float, float, float]]:
    """One continuous shot (one scene, one subject): window top-left positions per sample."""
    xs = _deadzone([targets[i].x - win_w / 2 for i in indices], deadzone * win_w / 2)
    ys = _deadzone([targets[i].y - win_h / 2 for i in indices], deadzone * win_h / 2)
    xs = _smooth(xs, fps=fps, window_s=0.8, max_speed=MAX_SPEED * win_w, max_accel=1.5 * MAX_SPEED * win_w)
    ys = _smooth(ys, fps=fps, window_s=0.8, max_speed=MAX_SPEED * win_h, max_accel=1.5 * MAX_SPEED * win_h)
    out = []
    mx, my = margin * win_w, margin * win_h
    for j, i in enumerate(indices):
        x, y = xs[j], ys[j]
        box = targets[i].box
        if box is not None:  # keep what is followed inside the window, less the safe margin
            x0, y0, x1, y1 = box
            if x1 - x0 <= win_w - 2 * mx:
                x = clamp(x, x1 + mx - win_w, x0 - mx)
            if y1 - y0 <= win_h - 2 * my:
                y = clamp(y, y1 + my - win_h, y0 - my)
        out.append((times[i], round(clamp(x, 0, src_w - win_w), 2), round(clamp(y, 0, src_h - win_h), 2)))
    return out


def _at(points: list[tuple[float, float, float]], t: float) -> tuple[float, float]:
    for (t0, x0, y0), (t1, x1, y1) in zip(points, points[1:]):
        if t0 <= t <= t1 and t1 > t0:
            u = (t - t0) / (t1 - t0)
            return x0 + (x1 - x0) * u, y0 + (y1 - y0) * u
    if t <= points[0][0]:
        return points[0][1], points[0][2]
    return points[-1][1], points[-1][2]


def _window_paths(times, cuts, targets, *, src_w, src_h, win_w, win_h, fps, deadzone, margin) -> tuple[CropPath, CropPath]:
    """Turn targets into the window's top-left paths. Each scene is split into shots at subject
    changes; each shot gets a dead zone, smoothing, speed caps and the keep-in clamp; a change of
    subject inside a scene is an eased move over TRANSITION_SECONDS; a scene cut is a jump exactly at
    the cut. Then the paths are simplified."""
    keys: list[tuple[float, float, float]] = []
    cut_list = list(cuts)
    for scene in _segments(times, cut_list):
        # A new shot starts when the subject changes between two real subjects (a track or the group).
        # Losing every face (subject None) isn't a switch: the smoothing eases to the fallback.
        shots, current, subject = [], [scene[0]], targets[scene[0]].subject
        for i in scene[1:]:
            new = targets[i].subject
            if new is not None and subject is not None and new != subject:
                shots.append(current)
                current = []
            if new is not None:
                subject = new
            current.append(i)
        shots.append(current)
        scene_keys: list[tuple[float, float, float]] = []
        for shot in shots:
            points = _shot(shot, times, targets, src_w=src_w, src_h=src_h, win_w=win_w, win_h=win_h, fps=fps,
                           deadzone=deadzone, margin=margin)
            if scene_keys:
                start = points[0][0]
                end = min(start + TRANSITION_SECONDS, points[-1][0]) if len(points) > 1 else start
                tx, ty = _at(points, end)
                _t, fx, fy = scene_keys[-1]
                steps = max(1, round((end - start) * 10))
                eased = [(round(start + (end - start) * k / steps, 3),
                          round(fx + (tx - fx) * _smoothstep(k / steps), 2),
                          round(fy + (ty - fy) * _smoothstep(k / steps), 2)) for k in range(steps + 1)]
                points = eased + [p for p in points if p[0] > end]
            scene_keys += points
        if keys:  # a cut: the previous scene holds until the cut, then jumps
            cut = max((c for c in cut_list if c <= scene_keys[0][0]), default=scene_keys[0][0])
            keys.append((cut, keys[-1][1], keys[-1][2]))
            first_t, first_x, first_y = scene_keys[0]
            scene_keys[0] = (max(cut, first_t) if first_t < cut else first_t, first_x, first_y)
            keys.append((cut, first_x, first_y))
            scene_keys = [p for p in scene_keys if p[0] > cut]
        keys += scene_keys
    clean: list[tuple[float, float, float]] = []
    for t, x, y in keys:
        if clean and t < clean[-1][0]:
            t = clean[-1][0]
        clean.append((t, x, y))
    path_x = CropPath(tuple(bounded_simplify([(t, x) for t, x, _y in clean], 1.0)))
    path_y = CropPath(tuple(bounded_simplify([(t, y) for t, _x, y in clean], 1.0)))
    return path_x, path_y


@dataclass
class _Choice:
    """Hysteresis for picking one track: switch only when another leads clearly for long enough."""
    lead: float
    hold_s: float
    min_shot_s: float
    current: int | None = None
    since: float = -math.inf
    challenger: int | None = None
    challenger_since: float = 0.0

    def pick(self, t: float, scores: dict[int, float]) -> int | None:
        if not scores:
            return self.current
        best = max(scores, key=scores.get)
        if self.current is None or self.current not in scores:
            self.current, self.since, self.challenger = best, t, None
            return self.current
        if best != self.current and scores[best] > self.lead * max(scores[self.current], 1e-6):
            if self.challenger != best:
                self.challenger, self.challenger_since = best, t
            elif t - self.challenger_since >= self.hold_s and t - self.since >= self.min_shot_s:
                self.current, self.since, self.challenger = best, t, None
        else:
            self.challenger = None
        return self.current


def _in_speech(t: float, speech: Sequence[tuple[float, float]]) -> bool:
    return any(s <= t < e for s, e in speech)


def plan_face_layout(analysis: dict, *, mode: str, src_w: int, src_h: int, out_w: int, out_h: int,
                     margin: float, deadzone: float, speech: Sequence[tuple[float, float]] = ()):
    """A tracked CropLayout for `face` or `speaker`, or None when too few samples have faces.

    face: every face framed together while they fit inside the window less the margins (with 10 %
    hysteresis), else the primary face (size × persistence × centrality, switching only on a clear,
    lasting lead). speaker: while someone speaks and several faces are visible, the face whose mouth
    moves most (a 1.3× lead held 1 s, shots of at least 1.5 s); otherwise as face."""
    from app.render_graph import CropLayout, CropPlan

    samples, cuts, fps = analysis["samples"], analysis.get("cuts", []), float(analysis["fps"])
    if not samples or sum(1 for s in samples if s["faces"]) < MIN_FACE_SHARE_OF_CLIP * len(samples):
        return None
    win_w, win_h = crop_window(src_w, src_h, out_w, out_h)
    persistence = {t["id"]: t["samples"] for t in analysis.get("tracks", [])}
    chooser = _Choice(lead=1.5, hold_s=1.0, min_shot_s=1.5)
    speaker = _Choice(lead=1.3, hold_s=1.0, min_shot_s=1.5)
    activity: dict[int, float] = {}
    inner_w, inner_h = win_w * (1 - 2 * margin), win_h * (1 - 2 * margin)
    grouped = True
    headroom = 0.12 * win_h if win_h < src_h else 0.0
    targets = []
    for sample in samples:
        t, found = sample["t"], sample["faces"]
        if not found:
            targets.append(_Target(t, None, None))
            continue
        for face in found:  # mouth motion, smoothed over about a second
            motion = face.get("mouth")
            if motion is not None:
                prev = activity.get(face["track"], motion)
                activity[face["track"]] = 0.75 * prev + 0.25 * motion
        x0 = min(f["x"] for f in found)
        x1 = max(f["x"] + f["w"] for f in found)
        y0 = min(f["y"] for f in found)
        y1 = max(f["y"] + f["h"] for f in found)
        span_w, span_h = x1 - x0, y1 - y0
        if grouped and (span_w > inner_w * 1.1 or span_h > inner_h * 1.1):
            grouped = False
        elif not grouped and span_w <= inner_w and span_h <= inner_h:
            grouped = True
        picked = None
        if mode == "speaker" and len(found) > 1 and _in_speech(t, speech):
            picked = speaker.pick(t, {f["track"]: activity.get(f["track"], 0.0) for f in found})
        elif mode == "speaker" and len(found) > 1 and speaker.current in {f["track"] for f in found}:
            picked = speaker.current  # nobody speaking: stay on the last speaker
        if picked is None and not grouped:
            scores = {f["track"]: f["w"] * f["h"] * (1 + persistence.get(f["track"], 1) / len(samples))
                      * (1.2 - abs(f["x"] + f["w"] / 2 - src_w / 2) / src_w) for f in found}
            picked = chooser.pick(t, scores)
        face = next((f for f in found if f["track"] == picked), None)
        if face is not None:
            box = (face["x"], face["y"], face["x"] + face["w"], face["y"] + face["h"])
            targets.append(_Target(t, face["x"] + face["w"] / 2, face["y"] + face["h"] / 2 + headroom,
                                   picked, box))
        else:
            targets.append(_Target(t, (x0 + x1) / 2, (y0 + y1) / 2 + headroom, GROUP, (x0, y0, x1, y1)))
    times = [s["t"] for s in samples]
    filled = _fill(targets, src_w / 2, src_h / 2, fps)
    path_x, path_y = _window_paths(times, cuts, filled, src_w=src_w, src_h=src_h, win_w=win_w, win_h=win_h,
                                   fps=fps, deadzone=deadzone, margin=margin)
    return CropLayout(CropPlan(width=win_w, height=win_h, x=path_x, y=path_y))


def plan_split_layout(analysis: dict, *, src_w: int, src_h: int, out_w: int, out_h: int, ratio: float,
                      deadzone: float, margin: float = 0.06):
    """Two stacked panels following the two most persistent face tracks (the left one on top), or
    None with fewer than two."""
    from app.render_graph import CropPlan, SplitLayout

    samples, cuts, fps = analysis["samples"], analysis.get("cuts", []), float(analysis["fps"])
    ranked = sorted(analysis.get("tracks", []), key=lambda t: -t["samples"])
    if len(ranked) < 2 or ranked[1]["samples"] < MIN_FACE_SHARE_OF_CLIP * len(samples):
        return None
    chosen = [ranked[0]["id"], ranked[1]["id"]]
    mean_x = {}
    for tid in chosen:
        xs = [f["x"] + f["w"] / 2 for s in samples for f in s["faces"] if f["track"] == tid]
        mean_x[tid] = sum(xs) / len(xs)
    chosen.sort(key=lambda tid: mean_x[tid])
    top_h = even(out_h * ratio)
    panels = []
    times = [s["t"] for s in samples]
    for tid, panel_h in zip(chosen, (top_h, out_h - top_h)):
        win_w, win_h = crop_window(src_w, src_h, out_w, panel_h)
        headroom = 0.1 * win_h if win_h < src_h else 0.0
        targets = []
        for s in samples:
            face = next((f for f in s["faces"] if f["track"] == tid), None)
            if face is None:
                targets.append(_Target(s["t"], None, None))
            else:
                box = (face["x"], face["y"], face["x"] + face["w"], face["y"] + face["h"])
                targets.append(_Target(s["t"], face["x"] + face["w"] / 2, face["y"] + face["h"] / 2 + headroom,
                                       tid, box))
        filled = _fill(targets, mean_x[tid], src_h / 2, fps)
        px, py = _window_paths(times, cuts, filled, src_w=src_w, src_h=src_h, win_w=win_w, win_h=win_h, fps=fps,
                               deadzone=deadzone, margin=margin)
        panels.append(CropPlan(width=win_w, height=win_h, x=px, y=py))
    return SplitLayout(top=panels[0], bottom=panels[1], top_height=top_h)


def tracker_for(analysis_path: Path, speech: Sequence[tuple[float, float]] = ()):
    """A layouts.Tracker built from an analysis file (None-returning when it can't be read)."""
    analysis = load_analysis(analysis_path)

    def tracker(request, src_w: int, src_h: int, out_w: int, out_h: int):
        if analysis is None or (analysis.get("width"), analysis.get("height")) != (src_w, src_h):
            return None
        if request.mode == "split":
            return plan_split_layout(analysis, src_w=src_w, src_h=src_h, out_w=out_w, out_h=out_h,
                                     ratio=request.split_ratio, deadzone=request.deadzone, margin=request.margin)
        return plan_face_layout(analysis, mode=request.mode, src_w=src_w, src_h=src_h, out_w=out_w, out_h=out_h,
                                margin=request.margin, deadzone=request.deadzone, speech=speech)

    return tracker
