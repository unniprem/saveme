"""Burned-in captions with the real FFmpeg and libass: captions show inside their cue times and not
outside, Malayalam is shaped with a font that has its glyphs, and (opt-in) caption timing matches
real speech transcribed by Whisper."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app import renderer
from app.captions import AssPlan, CaptionCue, CaptionWord, clip_words, fit_chars, segment, to_ass
from app.mediafile import FFMPEG
from app.render_spec import RenderSpec, resolve
from app.transcript import load_transcript
from tests.support.media import real_ffmpeg
from tests.support.procs import run_worker_proc
from tests.support.render import as_source, frame_rgb, render_request
from tests.support.speech import can_speak, make_speech

ML = "ഇലക്ട്രിക് വാഹനങ്ങൾക്ക് വില കുറയുന്നു"


def _grey(path: Path, seconds: float = 4.0) -> Path:
    subprocess.run([real_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x303030:s=640x360:r=25", "-f", "lavfi", "-i", "sine=frequency=300", "-t", str(seconds),
                    "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25",
                    "-c:a", "aac", str(path)], check=True, timeout=60)
    return path


def _ink(frame: np.ndarray, top: float, bottom: float) -> int:
    """Pixels in the rows [top, bottom) (shares of the height) clearly brighter than the grey."""
    h = frame.shape[0]
    band = frame[int(top * h):int(bottom * h)].astype(int)
    return int((band.max(axis=2) > 150).sum())


def _style(style_id="clean_minimal"):
    return resolve(RenderSpec.model_validate({"captions": {"style": style_id}}))


def test_burned_captions_show_only_inside_their_cues(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    src = _grey(folder / "media.mp4")
    work = tmp_path / "work"
    work.mkdir()
    r = _style()
    cues = [CaptionCue(0.5, 1.5, (CaptionWord(0.5, 1.5, "HELLO"),), ((0, 1),)),
            CaptionCue(2.5, 3.5, (CaptionWord(2.5, 3.5, "WORLD"),), ((0, 1),))]
    (work / "subs.ass").write_text(to_ass(cues, AssPlan(270, 480, r.captions, r.safe, duration=4.0)),
                                   encoding="utf-8")
    renderer.run_render(render_request(src, work, end=4.0, subtitles=True), lambda e: None)
    out = work / "clip-01.mp4"
    inside = [_ink(frame_rgb(out, t), 0.55, 0.95) for t in (0.6, 1.0, 1.4, 2.6, 3.0, 3.4)]
    outside = [_ink(frame_rgb(out, t), 0.55, 0.95) for t in (0.2, 1.7, 2.0, 2.3, 3.8)]
    assert min(inside) > 30, inside
    assert max(outside) == 0, outside
    assert _ink(frame_rgb(out, 1.0), 0.0, 0.5) == 0  # bottom captions stay in the lower half


def test_captions_can_be_left_out(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    src = _grey(folder / "media.mp4", 2.0)
    work = tmp_path / "work"
    work.mkdir()
    renderer.run_render(render_request(src, work, subtitles=False), lambda e: None)
    assert _ink(frame_rgb(work / "clip-01.mp4", 1.0), 0.0, 1.0) == 0


def _malayalam_font_installed() -> bool:
    if shutil.which("fc-list"):
        out = subprocess.run(["fc-list", ":lang=ml", "family"], capture_output=True, text=True, timeout=30)
        if out.stdout.strip():
            return True
    return any(Path(p).exists() for p in ("/System/Library/Fonts/Supplemental/Malayalam Sangam MN.ttc",
                                          "/System/Library/Fonts/Supplemental/Malayalam MN.ttc"))


@pytest.mark.skipif(not _malayalam_font_installed(), reason="no Malayalam font installed")
def test_malayalam_is_rendered_with_a_font_that_has_its_glyphs(tmp_path):
    src = _grey(tmp_path / "grey.mp4", 1.0)
    r = _style("podcast_classic")
    words = tuple(CaptionWord(0.0, 1.0, tok) for tok in ML.split())
    cue = CaptionCue(0.0, 1.0, words, ((0, 2), (2, 4)))
    (tmp_path / "subs.ass").write_text(to_ass([cue], AssPlan(540, 960, r.captions, r.safe)), encoding="utf-8")
    proc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "debug", "-nostdin", "-y", "-i", str(src),
                           "-vf", "scale=540:960,setpts=PTS+0.5/TB,ass=filename=subs.ass:shaping=complex",
                           "-frames:v", "1", "-f", "image2", "-update", "1", "-c:v", "mjpeg", "-strict", "-1",
                           "ml.jpg"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    log = proc.stderr
    assert "failed to find any fallback" not in log, re.findall(r".*fallback.*", log)
    picked = re.findall(r"fontselect: \(([^,]+), \d+, \d+\) -> ([^,]+),", log)
    assert picked, "libass chose no font"
    # Every Malayalam glyph found a font: either the Malayalam family directly, or a fallback.
    missing = re.findall(r"Glyph 0x(D[0-7][0-9A-F]) not found", log)
    for code in missing:
        assert re.search(rf"Glyph 0x{code} not found, selecting one more font.*\n.*fontselect", log), code
    assert _ink(frame_rgb(tmp_path / "ml.jpg", 0), 0.5, 1.0) > 2000


# --- timing against real speech (opt-in: macOS `say` and Whisper `base`) ---------------------------

def _base_model_cached() -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model("base", local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not (can_speak() and _base_model_cached()),
                    reason="needs macOS `say` and the faster-whisper base model")
def test_word_and_caption_timing_match_real_speech(tmp_path, monkeypatch):
    words = ("Electric vehicles are getting cheaper every year because battery prices keep falling across "
             "the world").split()
    source_dir = tmp_path / "source"
    truth = make_speech(source_dir, words)
    work = tmp_path / "ai"
    work.mkdir()
    monkeypatch.setenv("SAVEME_TEST_REAL_WHISPER", "1")
    request = {"mode": "transcribe", "source": str(source_dir / "media.mp4"), "workdir": str(work),
               "model": "base", "language": "en", "cpu_threads": 2, "duration": None, "max_seconds": 3600,
               "word_timestamps": True}
    events, rc = run_worker_proc(request, entry="tests.support.transcriber_entry", timeout=600)
    assert rc == 0, events
    transcript = load_transcript(work / "transcript.json")
    found = [w for s in transcript.segments for w in s.words]

    def norm(text):
        return re.sub(r"[^a-z]", "", text.lower())

    errors, k = [], 0
    for item in truth:
        while k < len(found) and norm(found[k].text) != norm(item["text"]):
            k += 1
        assert k < len(found), f"Whisper missed {item['text']!r}"
        errors.append((found[k].start - item["start"], found[k].end - item["end"]))
        k += 1
    starts = sorted(abs(s) for s, _e in errors)
    ends = sorted(abs(e) for _s, e in errors)
    (tmp_path / "errors.json").write_text(json.dumps(errors))
    assert starts[len(starts) // 2] <= 0.1 and starts[-1] <= 0.25, starts
    assert ends[len(ends) // 2] <= 0.15 and ends[-1] <= 0.4, ends

    # The burned captions follow the speech: each word's caption is on screen while it is said,
    # and nothing is on screen in the longest pause.
    r = _style()
    duration = truth[-1]["end"] + 0.5
    clip, timing = clip_words(transcript, 0.0, duration)
    assert timing == "words"
    cues = segment(clip, max_words=3, max_chars=fit_chars(r.captions, 270, 480, r.safe), max_lines=2,
                   clip_seconds=duration)
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    (render_dir / "subs.ass").write_text(to_ass(cues, AssPlan(270, 480, r.captions, r.safe, duration=duration)),
                                         encoding="utf-8")
    src = as_source(source_dir / "media.mp4", tmp_path / "src")
    renderer.run_render(render_request(src, render_dir, end=duration, subtitles=True,
                                       layout={"mode": "blur"}), lambda e: None)
    out = render_dir / "clip-01.mp4"
    for item in truth[::3]:
        middle = (item["start"] + item["end"]) / 2
        assert _ink(frame_rgb(out, middle), 0.6, 0.85) > 50, item
    gaps = [(b["start"] - a["end"], a["end"], b["start"]) for a, b in zip(truth, truth[1:])]
    longest, gap_start, gap_end = max(gaps)
    covering = [c for c in cues if c.start < gap_end and c.end > gap_start]
    quiet = (max([gap_start, *(c.end for c in covering if c.start <= gap_start)]) + gap_end) / 2
    if not any(c.start <= quiet < c.end for c in cues):
        assert _ink(frame_rgb(out, quiet), 0.6, 0.85) == 0


# --- styles and options ------------------------------------------------------------------------------

def _style_still(tmp_path, spec: dict, text: str = "one two three", at: float = 0.3) -> np.ndarray:
    """A 540x960 still of a grey source with one cue burned in the given caption spec; the middle
    word is the active one at `at`."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    src = folder / "media.mp4" if (folder / "media.mp4").exists() else _grey(folder / "media.mp4", 2.0)
    work = tmp_path / f"work-{len(list(tmp_path.iterdir()))}"
    work.mkdir()
    r = resolve(RenderSpec.model_validate(spec))
    tokens = text.split()
    words = tuple(CaptionWord(0.1 * (i + 1) if i else 0.0, 0.1 * (i + 2), tok) for i, tok in enumerate(tokens))
    cue = CaptionCue(0.0, 1.0, words, ((0, len(words)),))
    (work / "subs.ass").write_text(to_ass([cue], AssPlan(540, 960, r.captions, r.safe, duration=2.0)),
                                   encoding="utf-8")
    renderer.run_render(render_request(src, work, kind="frame", output="f.jpg", at=at, width=540, height=960,
                                       subtitles=True, layout={"mode": "original"}), lambda e: None)
    return frame_rgb(work / "f.jpg", 0).astype(int)


def _count(frame: np.ndarray, rgb: tuple[int, int, int], tol: int = 60) -> int:
    return int((np.abs(frame - np.array(rgb)).max(axis=2) < tol).sum())


@pytest.mark.parametrize("style", ["clean_minimal", "bold_creator", "karaoke", "podcast_classic", "cinematic",
                                   "high_contrast"])
def test_every_style_renders(tmp_path, style):
    frame = _style_still(tmp_path, {"captions": {"style": style}})
    band = frame[int(0.55 * 960):int(0.85 * 960)]
    assert int((band.max(axis=2) > 150).sum()) > 200, style  # text (or a highlight) is drawn
    dark = _count(band, (0, 0, 0), tol=25)
    if style in ("podcast_classic", "high_contrast"):
        assert dark > 3000, style  # an opaque box behind the text
    if style == "high_contrast":
        assert _count(band, (255, 230, 0), tol=70) > 300  # yellow text
    if style == "bold_creator":
        assert _count(band, (255, 212, 0), tol=70) > 100  # the active word in yellow
    if style == "karaoke":
        assert _count(band, (124, 58, 237), tol=60) > 300  # the active word's purple box


def test_style_options_change_the_output(tmp_path):
    base = _style_still(tmp_path, {"captions": {"style": "clean_minimal"}})
    red = _style_still(tmp_path, {"captions": {"style": "clean_minimal", "color": "#FF0000"}})
    big = _style_still(tmp_path, {"captions": {"style": "clean_minimal", "size": 120}})
    top = _style_still(tmp_path, {"captions": {"style": "clean_minimal", "position": "top"}})

    def ink(frame, lo=0.0, hi=1.0):
        return int((frame[int(lo * 960):int(hi * 960)].max(axis=2) > 150).sum())

    assert _count(red, (255, 0, 0), tol=70) > 200 and _count(base, (255, 0, 0), tol=70) == 0
    assert ink(big) > 2 * ink(base)
    assert ink(top, 0.0, 0.4) > 100 and ink(top, 0.5, 1.0) == 0 and ink(base, 0.0, 0.4) == 0
