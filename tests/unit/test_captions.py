"""Auto Shorts captions: words for a clip (timed or estimated), segmentation, line balancing,
SRT and ASS output, styles, placement, escaping, and the transcriber's word-edge refinement."""
import re

import numpy as np
import pytest

from app.caption_styles import STYLES
from app.captions import (AssPlan, CaptionCue, CaptionWord, balance_lines, clip_words, cues_from_edits,
                          escape_ass, fit_chars, graphemes, is_malayalam, segment, tag_color, to_ass, to_srt)
from app.render_spec import Cue, RenderSpec, resolve
from app.transcriber import Voicing, refine_words
from app.transcript import Segment, Transcript, Word

ML = "ഇലക്ട്രിക് വാഹനങ്ങൾക്ക് വില കുറയുന്നു"


def transcript(segments) -> Transcript:
    return Transcript(origin="whisper", model="base", language="en", language_probability=0.9,
                      requested_language="auto", duration=100.0, segments=segments)


def timed_segment(start, words):
    ws = [Word(start=s, end=e, text=t) for s, e, t in words]
    return Segment(start=start, end=max(e for _s, e, _t in words), text=" ".join(t for *_r, t in words),
                   language="en", words=ws)


def W(start, end, text="w"):
    return CaptionWord(start, end, text)


# --- text measures -----------------------------------------------------------------------------

def test_graphemes_count_combining_marks_with_their_letter():
    assert graphemes("hello") == 5
    assert graphemes("കു") == 1  # ka + vowel sign u
    assert graphemes("ക്ട്രി") == 3  # ka virama ṭa virama ra vowel-sign-i: three base letters
    assert graphemes("e\u0301") == 1


def test_malayalam_detection():
    assert is_malayalam(ML) and not is_malayalam("Electric vehicles") and not is_malayalam("123 !")
    assert is_malayalam("EV " + ML)


@pytest.mark.parametrize("raw,safe", [
    ("{\\b1}bold", "(/b1)bold"), ("line\\Nbreak", "line/Nbreak"), ("hard\\hspace", "hard/hspace"),
    ("a\nb", "a b"), ("x\u202ey", "xy"), ("{\\p1}m 0 0 l 100 0{\\p0}", "(/p1)m 0 0 l 100 0(/p0)"),
])
def test_escape_ass_neutralises_override_blocks_and_escapes(raw, safe):
    assert escape_ass(raw) == safe


# --- words for a clip ----------------------------------------------------------------------------

def test_clip_words_use_word_timestamps_shifted_to_the_clip():
    t = transcript([timed_segment(10.0, [(10.0, 10.4, "one"), (10.5, 10.9, "two"), (11.0, 11.5, "three")]),
                    timed_segment(20.0, [(20.0, 20.5, "later")])])
    words, timing = clip_words(t, 10.45, 15.0)
    assert timing == "words"
    assert [(w.start, w.end, w.text) for w in words] == [(0.05, 0.45, "two"), (0.55, 1.05, "three")]


def test_clip_words_estimate_inside_segments_without_words():
    t = transcript([Segment(start=2.0, end=4.0, text="aa bbbb", language="en")])
    words, timing = clip_words(t, 0.0, 10.0)
    assert timing == "estimated" and [w.text for w in words] == ["aa", "bbbb"]
    # shared by length: "aa"+1 = 3 parts, "bbbb"+1 = 5 parts of 2 s
    assert (words[0].start, words[0].end, words[1].end) == (2.0, 2.75, 4.0)


def test_clip_words_report_mixed_and_none():
    t = transcript([timed_segment(0.0, [(0.0, 0.5, "a")]), Segment(start=1.0, end=2.0, text="b", language="en")])
    assert clip_words(t, 0.0, 3.0)[1] == "mixed"
    assert clip_words(t, 50.0, 60.0) == ([], "none")


def test_a_word_belongs_to_the_clip_holding_its_middle():
    t = transcript([timed_segment(0.0, [(4.6, 5.4, "edge")])])
    assert [w.text for w in clip_words(t, 0.0, 5.1)[0]] == ["edge"]  # middle 5.0 is inside
    assert clip_words(t, 0.0, 4.9)[0] == []
    assert clip_words(t, 0.0, 5.1)[0][0].end == 5.1  # clipped to the clip


# --- cues --------------------------------------------------------------------------------------------

def test_segment_respects_word_limits():
    words = [W(i * 0.3, i * 0.3 + 0.25, f"w{i}") for i in range(10)]
    cues = segment(words, max_words=3, max_chars=40, max_lines=1)
    assert [len(c.words) for c in cues] == [3, 3, 3, 1]


def test_segment_breaks_at_pauses_sentences_and_length():
    words = [W(0, 0.3, "Hi."), W(0.4, 0.7, "This"), W(0.8, 1.0, "is"), W(2.0, 2.3, "after"),
             W(2.4, 2.6, "a"), W(2.7, 3.0, "pause")]
    cues = segment(words, max_words=10, max_chars=40, max_lines=2)
    assert [c.text for c in cues] == ["Hi.", "This is", "after a pause"]
    long = [W(i * 0.5, i * 0.5 + 0.4, "x") for i in range(12)]
    assert all(c.words[-1].end - c.start <= 3.5 for c in segment(long, max_words=20, max_chars=60, max_lines=2))


def test_segment_caps_characters_per_cue():
    words = [W(i * 0.3, i * 0.3 + 0.2, "abcdefghij") for i in range(6)]  # 10 letters each
    cues = segment(words, max_words=10, max_chars=21, max_lines=2)  # at most 42: three words + spaces
    assert all(graphemes(c.text) <= 42 for c in cues) and len(cues) == 2


def test_cue_times_never_overlap_and_hold_briefly():
    words = [W(0.0, 0.2, "a."), W(0.3, 0.5, "b."), W(3.0, 3.2, "c.")]
    cues = segment(words, max_words=5, max_chars=30, max_lines=2, clip_seconds=3.3)
    assert [(c.start, c.end) for c in cues] == [(0.0, 0.3), (0.3, 0.9), (3.0, 3.3)]
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start


def test_balance_lines():
    words = [W(0, 0, t) for t in "one two three four five six".split()]
    assert balance_lines(words, 2, 40) == ((0, 6),)  # fits on one line
    assert balance_lines(words, 2, 12) == ((0, 3), (3, 6))  # "one two three" / "four five six"
    assert len(balance_lines(words, 3, 8)) == 3
    assert balance_lines(words, 1, 5) == ((0, 6),)


def test_malayalam_breaks_only_between_words():
    words = [W(i, i + 0.5, tok) for i, tok in enumerate(ML.split())]
    cues = segment(words, max_words=8, max_chars=12, max_lines=2)
    rebuilt = " ".join(c.text for c in cues)
    assert rebuilt == ML  # every word intact, no split cluster
    for cue in cues:
        for line in cue.line_texts():
            assert all(tok in ML.split() for tok in line.split())


def test_edited_cues_keep_their_text_and_times():
    cues = cues_from_edits([Cue(start=0.5, end=2.0, text="Fixed text here", speaker="Ann")], max_lines=2,
                           max_chars=30)
    assert cues[0].start == 0.5 and cues[0].end == 2.0 and cues[0].text == "Fixed text here"
    assert cues[0].speaker == "Ann" and cues[0].words[0].start == 0.5 and cues[0].words[-1].end == 2.0


def test_fit_chars_shrinks_lines_for_big_fonts_and_malayalam():
    style = dict(STYLES["bold_creator"])
    safe = {"left": 0.05, "right": 0.12}
    small = fit_chars({**style, "size": 40}, 1080, 1920, safe)
    big = fit_chars({**style, "size": 160}, 1080, 1920, safe)
    assert small == style["max_chars"] and big < small
    assert fit_chars(dict(STYLES["clean_minimal"]), 1080, 1920, safe, malayalam=True) < \
        fit_chars(dict(STYLES["clean_minimal"]), 1080, 1920, safe)


# --- SRT -------------------------------------------------------------------------------------------

def test_srt_output():
    cue = CaptionCue(1.5, 3.25, (W(1.5, 2, "Hello"), W(2.1, 3, "there")), ((0, 1), (1, 2)), "Ann")
    late = CaptionCue(3725.0, 3726.0, (W(3725, 3726, "late"),), ((0, 1),))
    assert to_srt([cue, late]) == ("1\n00:00:01,500 --> 00:00:03,250\nAnn: Hello\nthere\n\n"
                                   "2\n01:02:05,000 --> 01:02:06,000\nlate\n")
    assert "HELLO" in to_srt([cue], uppercase=True)


# --- ASS -------------------------------------------------------------------------------------------

def _plan(style_id="clean_minimal", **change):
    r = resolve(RenderSpec.model_validate({"captions": {"style": style_id}}))
    base = dict(width=1080, height=1920, style=r.captions, safe=r.safe, duration=5.0)
    base.update(change)
    return AssPlan(**base)


def _events(ass: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def test_ass_header_matches_the_canvas():
    ass = to_ass([], _plan(width=1080, height=1350))
    assert "PlayResX: 1080" in ass and "PlayResY: 1350" in ass and "WrapStyle: 2" in ass
    assert "ScaledBorderAndShadow: yes" in ass and "YCbCr Matrix: TV.709" in ass
    assert _events(ass) == []


def test_ass_style_line_follows_the_style():
    ass = to_ass([], _plan("high_contrast"))
    style = next(line for line in ass.splitlines() if line.startswith("Style: Caption,"))
    fields = style.split(",")
    assert fields[1] == "Roboto" and fields[2] == "62" and fields[3] == "&H0000E6FF"  # yellow text
    assert fields[15] == "3"  # an opaque box
    assert fields[5] == "&H00000000"  # the box: black, fully opaque


def test_ass_sizes_scale_with_the_short_side():
    half = to_ass([], _plan("clean_minimal", width=540, height=960))
    assert "Style: Caption,Roboto,27," in half
    landscape = to_ass([], _plan("clean_minimal", width=1920, height=1080))
    assert "Style: Caption,Roboto,54," in landscape


def test_ass_events_carry_position_and_escaped_text():
    cue = CaptionCue(0.5, 1.5, (W(0.5, 1.0, "{\\b1}x"), W(1.0, 1.5, "y")), ((0, 2),))
    events = _events(to_ass([cue], _plan("clean_minimal")))
    assert len(events) == 1
    assert events[0].startswith("Dialogue: 2,0:00:00.50,0:00:01.50,Caption,,0,0,0,,{\\an2\\pos(")
    assert "(/b1)x y" in events[0] and "{\\b1}" not in events[0]


def test_word_highlight_makes_one_event_per_word_in_order():
    cue = CaptionCue(0.0, 2.0, (W(0.0, 0.5, "one"), W(0.6, 1.0, "two"), W(1.2, 1.8, "three")), ((0, 3),))
    events = _events(to_ass([cue], _plan("bold_creator")))
    times = [tuple(e.split(",")[1:3]) for e in events]
    assert times == [("0:00:00.00", "0:00:00.60"), ("0:00:00.60", "0:00:01.20"), ("0:00:01.20", "0:00:02.00")]
    yellow = tag_color("#FFD400")
    for event, word in zip(events, ("ONE", "TWO", "THREE")):  # uppercase style
        assert f"{{\\c{yellow}}}{word}{{\\c&HFFFFFF&}}" in event
        assert event.count(f"\\c{yellow}") == 1


def test_box_highlight_uses_a_thick_coloured_border():
    cue = CaptionCue(0.0, 1.0, (W(0.0, 0.5, "a"), W(0.5, 1.0, "b")), ((0, 2),))
    events = _events(to_ass([cue], _plan("karaoke")))
    assert "\\3c&HED3A7C&\\bord" in events[0] and len(events) == 2


def test_malayalam_cues_choose_the_malayalam_family():
    cue = CaptionCue(0.0, 1.0, tuple(W(0, 1, t) for t in ML.split()), ((0, 4),))
    assert "{\\fnManjari}" in _events(to_ass([cue], _plan()))[0]


@pytest.mark.parametrize("position,align", [("bottom", 2), ("middle", 5), ("top", 8)])
def test_positions_stay_inside_the_safe_area(position, align):
    r = resolve(RenderSpec.model_validate({"captions": {"position": position}}))
    cue = CaptionCue(0.0, 1.0, (W(0, 1, "x"),), ((0, 1),))
    event = _events(to_ass([cue], AssPlan(1080, 1920, r.captions, r.safe)))[0]
    m = re.search(r"\\an(\d)\\pos\(([\d.]+),([\d.]+)\)", event)
    assert int(m.group(1)) == align
    y = float(m.group(3))
    if position == "bottom":
        assert y <= 1920 * (1 - r.safe["bottom"]) + 1
    if position == "top":
        assert y >= 1920 * r.safe["top"] - 1


def test_split_layouts_put_middle_captions_on_the_seam():
    r = resolve(RenderSpec.model_validate({"template": "podcast_split"}))
    cue = CaptionCue(0.0, 1.0, (W(0, 1, "x"),), ((0, 1),))
    event = _events(to_ass([cue], AssPlan(1080, 1920, r.captions, r.safe, split_seam=0.4)))[0]
    assert "\\an5" in event and ",768)" in event


def test_title_bar_and_watermark_without_captions():
    ass = to_ass([], _plan(enabled=False, title="Big {\\fs200}news", brand_color="#112233",
                           watermark="@me\\N", duration=7.0))
    events = _events(ass)
    assert len(events) == 3
    assert all(",0:00:00.00,0:00:07.00," in e for e in events)
    assert "\\c&H332211&" in events[0] and "\\p1" in events[0]
    assert "Big (/fs200)news" in events[1] and "@me/N" in events[2]
    assert to_ass([CaptionCue(0, 1, (W(0, 1, "x"),), ((0, 1),))], _plan(enabled=False)).count("Dialogue:") == 0


def test_speaker_labels_are_prefixed():
    cue = CaptionCue(0.0, 1.0, (W(0, 1, "hello"),), ((0, 1),), speaker="Ann")
    assert "Ann:" in _events(to_ass([cue], _plan()))[0]


# --- word-edge refinement (transcriber) --------------------------------------------------------------

def _tone_with_gaps(spans, total, rate=16000):
    t = np.arange(int(total * rate)) / rate
    audio = np.random.default_rng(1).normal(0, 0.002, len(t)).astype(np.float32)  # a little noise
    for s, e in spans:
        mask = (t >= s) & (t < e)
        audio[mask] += (0.4 * np.sin(2 * np.pi * 220 * t[mask])).astype(np.float32)
    return audio


def test_refinement_moves_starts_past_pauses_and_ends_through_speech():
    audio = _tone_with_gaps([(0.5, 1.0), (1.8, 2.4)], 3.0)
    voicing = Voicing(audio, offset=10.0)
    # Whisper-like: the second word starts inside the first's tail and ends early.
    words = [Word(start=10.45, end=10.9, text="one"), Word(start=10.9, end=12.2, text="two")]
    out = refine_words(words, voicing, segment_end=12.6)
    assert out[0].start == pytest.approx(10.47, abs=0.02) and out[0].end == pytest.approx(11.06, abs=0.03)
    assert out[1].start == pytest.approx(11.77, abs=0.02)  # after the 0.8 s pause
    assert out[1].end == pytest.approx(12.46, abs=0.03)  # pushed to the end of the tone
    assert out[0].end <= out[1].start


def test_refinement_leaves_unvoiced_spans_and_order_alone():
    audio = np.zeros(16000 * 2, dtype=np.float32)
    voicing = Voicing(audio, offset=0.0)
    words = [Word(start=0.2, end=0.5, text="a"), Word(start=0.6, end=0.9, text="b")]
    assert refine_words(words, voicing, segment_end=1.0) == words
    assert refine_words(None, voicing, segment_end=1.0) is None


def test_refinement_never_moves_a_start_past_its_end_or_an_end_past_the_next_start():
    audio = _tone_with_gaps([(0.0, 3.0)], 3.0)  # voiced throughout
    voicing = Voicing(audio, offset=0.0)
    words = [Word(start=0.0, end=1.0, text="a"), Word(start=1.0, end=1.2, text="b"),
             Word(start=1.3, end=2.0, text="c")]
    out = refine_words(words, voicing, segment_end=2.0)
    assert [w.start for w in out] == [0.0, 1.0, 1.3]
    for a, b in zip(out, out[1:]):
        assert a.end <= b.start + 1e-9 and a.start <= a.end
    assert out[-1].end <= 2.0


def test_balancing_long_cues_is_fast():
    import time

    words = [W(0, 0, "a")] * 100
    began = time.perf_counter()
    for _ in range(50):
        assert len(balance_lines(words, 3, 20)) == 3
    assert time.perf_counter() - began < 1.0  # O(n²) with prefix sums (was O(n³): 2.3 s for 500 cues)


def test_a_cue_too_quick_to_highlight_still_shows():
    words = tuple(W(0.001 * i, 0.001 * i, f"w{i}") for i in range(4))
    cue = CaptionCue(0.0, 0.004, words, ((0, 4),))
    events = _events(to_ass([cue], _plan("bold_creator")))
    assert len(events) == 1 and "W0 W1 W2 W3" in events[0]
