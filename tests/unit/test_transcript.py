"""Transcript schema, subtitle import (SRT/WebVTT), language codes and the on-disk cache."""
import json

import pytest

from app.errors import AppError
from app.transcript import (MAX_SEGMENTS, WHISPER_LANGUAGES, Segment, Transcript, clean_text,
                            dominant_language, is_language, load_transcript, parse_subtitles,
                            save_transcript)

SRT = """1
00:00:01,000 --> 00:00:04,500
Hello and <i>welcome</i>
to the show.

2
00:00:05,000 --> 00:00:07,250
{\\an8}Today: artificial intelligence.

"""

VTT = """WEBVTT - talk

NOTE this is a comment
that spans lines

STYLE
::cue { color: lime }

intro
00:01.000 --> 00:04.500 align:start position:10%
<v Speaker>Hello and <c.loud>welcome</c></v>

00:00:05.000 --> 00:00:07.250
Today: artificial intelligence.
"""


def _transcript(**overrides) -> Transcript:
    fields = {"origin": "whisper", "model": "base", "language": "en", "language_probability": 0.9,
              "requested_language": "auto", "duration": 10.0,
              "segments": [Segment(start=0.0, end=2.0, text="One.", language="en"),
                           Segment(start=2.0, end=5.5, text="Two.", language="en")]}
    fields.update(overrides)
    return Transcript(**fields)


# --- languages -------------------------------------------------------------------------

def test_whisper_languages_cover_english_and_malayalam():
    assert WHISPER_LANGUAGES["en"] == "English"
    assert WHISPER_LANGUAGES["ml"] == "Malayalam"
    assert is_language("ml") and is_language("yue")
    assert not is_language("auto") and not is_language("xx") and not is_language("EN")


def test_whisper_languages_match_faster_whisper():
    tokenizer = pytest.importorskip("faster_whisper.tokenizer")
    assert set(WHISPER_LANGUAGES) == set(tokenizer._LANGUAGE_CODES)


def test_dominant_language_is_the_one_covering_most_speech():
    segments = [Segment(start=0, end=10, text="a", language="ml"),
                Segment(start=10, end=12, text="b", language="en"),
                Segment(start=12, end=13, text="c", language="en")]
    assert dominant_language(segments) == "ml"
    assert dominant_language([Segment(start=0, end=1, text="x")]) == "und"
    assert dominant_language([]) == "und"


# --- text cleaning ---------------------------------------------------------------------

def test_clean_text_joins_lines_and_drops_control_characters():
    assert clean_text("  Hello\n  world\t!\x00\x1b[31m ") == "Hello world ![31m"
    assert clean_text("‮evil​") == "evil"
    assert len(clean_text("a" * 5000)) == 2000


# --- schema ----------------------------------------------------------------------------

def test_transcript_round_trips_through_json(tmp_path):
    path = tmp_path / "transcript.json"
    original = _transcript()
    save_transcript(path, original)
    assert json.loads(path.read_text())["version"] == 1
    assert load_transcript(path) == original


def test_load_transcript_returns_none_for_missing_or_bad_files(tmp_path):
    assert load_transcript(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_transcript(bad) is None
    bad.write_text(json.dumps({"version": 1, "origin": "whisper"}))
    assert load_transcript(bad) is None
    link = tmp_path / "link.json"
    good = tmp_path / "good.json"
    save_transcript(good, _transcript())
    link.symlink_to(good)
    assert load_transcript(link) is None


@pytest.mark.parametrize("segments", [
    [Segment(start=2.0, end=3.0, text="b"), Segment(start=0.0, end=1.0, text="a")],  # unsorted
    [Segment(start=0.0, end=11.0, text="past the end")],
])
def test_transcript_refuses_bad_segments(segments):
    with pytest.raises(ValueError):
        _transcript(segments=segments)


@pytest.mark.parametrize("fields", [
    {"start": 1.0, "end": 1.0, "text": "empty range"},
    {"start": -1.0, "end": 1.0, "text": "before zero"},
    {"start": 0.0, "end": float("inf"), "text": "endless"},
    {"start": 0.0, "end": 1.0, "text": ""},
    {"start": 0.0, "end": 1.0, "text": "x", "language": "xx"},
])
def test_segment_refuses_bad_fields(fields):
    with pytest.raises(ValueError):
        Segment(**fields)


def test_transcript_refuses_unknown_languages_and_origins():
    with pytest.raises(ValueError):
        _transcript(language="klingon")
    with pytest.raises(ValueError):
        _transcript(origin="cloud")
    with pytest.raises(ValueError):
        _transcript(requested_language="xx")
    assert _transcript(language="und").language == "und"


def test_transcript_segment_cap():
    many = [Segment(start=i, end=i + 0.5, text="x") for i in range(MAX_SEGMENTS + 1)]
    with pytest.raises(ValueError):
        _transcript(segments=many, duration=MAX_SEGMENTS + 1.0)


# --- subtitles -------------------------------------------------------------------------

def test_parse_srt_strips_tags_and_joins_lines():
    t = parse_subtitles(SRT.encode(), filename="talk.srt", duration=60.0, language="auto")
    assert t.origin == "subtitles" and t.model is None and t.language_probability is None
    assert [(s.start, s.end, s.text) for s in t.segments] == [
        (1.0, 4.5, "Hello and welcome to the show."),
        (5.0, 7.25, "Today: artificial intelligence."),
    ]
    assert t.language == "und" and t.duration == 60.0


def test_parse_vtt_skips_header_notes_styles_and_settings():
    t = parse_subtitles(VTT.encode(), filename=None, duration=60.0, language="en")
    assert [(s.start, s.end, s.text) for s in t.segments] == [
        (1.0, 4.5, "Hello and welcome"),
        (5.0, 7.25, "Today: artificial intelligence."),
    ]
    assert t.language == "en" and all(s.language == "en" for s in t.segments)
    assert t.requested_language == "en"


def test_parse_subtitles_handles_bom_crlf_and_windows_1252():
    data = ("﻿" + SRT.replace("\n", "\r\n")).encode("utf-8")
    assert len(parse_subtitles(data, filename="a.srt", duration=None, language="auto").segments) == 2
    latin = "1\n00:00:01,000 --> 00:00:02,000\nCafé ½\n".encode("cp1252")
    t = parse_subtitles(latin, filename="a.srt", duration=None, language="auto")
    assert t.segments[0].text == "Café ½"
    assert t.duration == 2.0  # no known length: the last cue's end


def test_parse_subtitles_sorts_and_drops_cues_past_the_end():
    text = ("1\n00:00:09,000 --> 00:00:12,000\nlate\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nearly\n\n"
            "3\n00:00:20,000 --> 00:00:21,000\ngone\n\n"
            "4\n00:00:05,000 --> 00:00:05,000\nzero length\n\n"
            "5\n00:00:06,000 --> 00:00:07,000\n   \n")
    t = parse_subtitles(text.encode(), filename="a.srt", duration=10.0, language="auto")
    assert [(s.start, s.end, s.text) for s in t.segments] == [(3.0, 4.0, "early"), (9.0, 10.0, "late")]


def test_parse_subtitles_accepts_hours_and_short_vtt_times():
    text = "WEBVTT\n\n1:02:03.500 --> 1:02:05.000\nlate in the video\n"
    t = parse_subtitles(text.encode(), filename="x.vtt", duration=None, language="auto")
    assert t.segments[0].start == 3723.5 and t.segments[0].end == 3725.0


@pytest.mark.parametrize("data", [
    b"",
    b"just some text\nwith no cues\n",
    b"\x00\x01\x02binary",
    b"WEBVTT\n\nNOTE only a note\n",
    b"1\n00:00:05,000 --> 00:00:01,000\nbackwards\n",
])
def test_parse_subtitles_refuses_files_without_valid_cues(data):
    with pytest.raises(AppError) as exc:
        parse_subtitles(data, filename="x.srt", duration=None, language="auto")
    assert exc.value.code == "invalid_subtitles"


def test_parse_subtitles_refuses_unknown_language():
    with pytest.raises(AppError) as exc:
        parse_subtitles(SRT.encode(), filename="x.srt", duration=None, language="xx")
    assert exc.value.code == "invalid_request"


# --- cache reuse -----------------------------------------------------------------------

def test_cached_transcript_is_reused_unless_asked_otherwise():
    from app.transcript import reusable

    auto_en = _transcript(requested_language="auto", language="en")
    assert reusable(auto_en, language="auto", retranscribe=False)
    assert reusable(auto_en, language="en", retranscribe=False)  # auto already found English
    assert not reusable(auto_en, language="ml", retranscribe=False)
    assert not reusable(auto_en, language="auto", retranscribe=True)
    assert not reusable(None, language="auto", retranscribe=False)
    forced_ml = _transcript(requested_language="ml", language="ml",
                            segments=[Segment(start=0, end=1, text="x", language="ml")])
    assert reusable(forced_ml, language="ml", retranscribe=False)
    assert reusable(forced_ml, language="auto", retranscribe=False)
    assert not reusable(forced_ml, language="en", retranscribe=False)


def test_imported_subtitles_are_reused_whatever_the_language():
    from app.transcript import reusable

    subs = parse_subtitles(SRT.encode(), filename="a.srt", duration=60.0, language="auto")
    assert reusable(subs, language="ml", retranscribe=False)
    assert not reusable(subs, language="auto", retranscribe=True)


def test_absurd_hours_are_not_a_time():
    huge = "9" * 5000
    data = f"1\n{huge}:00:01,000 --> {huge}:00:02,000\nx\n\n2\n00:00:01,000 --> 00:00:02,000\nok\n".encode()
    t = parse_subtitles(data, filename="a.srt", duration=None, language="auto")
    assert [s.text for s in t.segments] == ["ok"]


def test_a_cue_that_rounds_to_nothing_at_the_end_is_dropped():
    data = ("1\n00:00:10,000 --> 00:00:20,000\nkept\n\n"
            "2\n00:00:59,999 --> 00:01:05,000\nrounds away\n").encode()
    t = parse_subtitles(data, filename="a.srt", duration=59.9994, language="auto")
    assert [s.text for s in t.segments] == ["kept"]
