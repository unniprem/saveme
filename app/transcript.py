"""Transcripts for AI Highlights: the schema, subtitle import (SRT, WebVTT), Whisper's language
codes and the per-source cache file.

A transcript is a list of timed, single-line text segments. It comes from the transcriber
worker (origin "whisper") or from a subtitle file (origin "subtitles"), and is cached as JSON in
the source's folder (`<source workdir>/ai/transcript.json`), so it ends with the source. All of
its text is untrusted: it is cleaned of control and formatting characters, capped in length,
and only ever shown as text.

This module is imported by the API process and the transcriber worker; it must not import
faster_whisper (heavy) or yt_dlp.
"""
from __future__ import annotations

import html
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.errors import AppError

TRANSCRIPT_VERSION = 1
MAX_SEGMENTS = 20_000
MAX_SEGMENT_CHARS = 2000
MAX_TRANSCRIPT_FILE_BYTES = 64 * 1024 * 1024
UNKNOWN_LANGUAGE = "und"
AUTO = "auto"
# A segment may end this far past the stated duration (rounding between probes and decoders).
_END_TOLERANCE = 0.001

# faster-whisper's model names (faster_whisper.utils.available_models()).
WHISPER_MODELS = ("tiny.en", "tiny", "base.en", "base", "small.en", "small", "medium.en", "medium",
                  "large-v1", "large-v2", "large-v3", "large", "distil-large-v2", "distil-medium.en",
                  "distil-small.en", "distil-large-v3", "distil-large-v3.5", "large-v3-turbo", "turbo")

# Whisper's languages (faster_whisper.tokenizer._LANGUAGE_CODES), with English names.
WHISPER_LANGUAGES: dict[str, str] = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani",
    "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bo": "Tibetan",
    "br": "Breton", "bs": "Bosnian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh", "da": "Danish",
    "de": "German", "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fo": "Faroese", "fr": "French",
    "gl": "Galician", "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiian", "he": "Hebrew",
    "hi": "Hindi", "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian", "hy": "Armenian",
    "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese", "jw": "Javanese",
    "ka": "Georgian", "kk": "Kazakh", "km": "Khmer", "kn": "Kannada", "ko": "Korean", "la": "Latin",
    "lb": "Luxembourgish", "ln": "Lingala", "lo": "Lao", "lt": "Lithuanian", "lv": "Latvian",
    "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian",
    "mr": "Marathi", "ms": "Malay", "mt": "Maltese", "my": "Myanmar", "ne": "Nepali", "nl": "Dutch",
    "nn": "Nynorsk", "no": "Norwegian", "oc": "Occitan", "pa": "Punjabi", "pl": "Polish",
    "ps": "Pashto", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian", "sa": "Sanskrit",
    "sd": "Sindhi", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian", "sn": "Shona",
    "so": "Somali", "sq": "Albanian", "sr": "Serbian", "su": "Sundanese", "sv": "Swedish",
    "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "tg": "Tajik", "th": "Thai", "tk": "Turkmen",
    "tl": "Tagalog", "tr": "Turkish", "tt": "Tatar", "uk": "Ukrainian", "ur": "Urdu",
    "uz": "Uzbek", "vi": "Vietnamese", "yi": "Yiddish", "yo": "Yoruba", "zh": "Chinese",
    "yue": "Cantonese",
}


def is_language(code: object) -> bool:
    """True for a Whisper language code (lowercase, as Whisper writes it)."""
    return isinstance(code, str) and code in WHISPER_LANGUAGES


def clean_text(text: str, limit: int = MAX_SEGMENT_CHARS) -> str:
    """One line of plain text: whitespace runs become one space, control and invisible
    formatting characters (bidi overrides, zero-width marks) are dropped, then capped."""
    kept = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"):
            kept.append(ch)
    return " ".join("".join(kept).split())[:limit]


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be finite")
    return value


Seconds = Annotated[float, Field(ge=0)]


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: Seconds
    end: Seconds
    text: Annotated[str, Field(min_length=1, max_length=MAX_SEGMENT_CHARS)]
    language: str | None = None

    @field_validator("start", "end")
    @classmethod
    def _times_finite(cls, value: float) -> float:
        return _finite(value)

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str | None) -> str | None:
        if value is not None and not is_language(value):
            raise ValueError("unknown language")
        return value

    @model_validator(mode="after")
    def _range(self) -> Segment:
        if self.end <= self.start:
            raise ValueError("a segment must end after it starts")
        return self


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = TRANSCRIPT_VERSION
    origin: Literal["whisper", "subtitles"]
    model: Annotated[str, Field(max_length=64)] | None
    language: str
    language_probability: Annotated[float, Field(ge=0, le=1)] | None
    requested_language: str
    duration: Annotated[float, Field(gt=0)]
    segments: Annotated[list[Segment], Field(max_length=MAX_SEGMENTS)]

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        if value != UNKNOWN_LANGUAGE and not is_language(value):
            raise ValueError("unknown language")
        return value

    @field_validator("requested_language")
    @classmethod
    def _requested(cls, value: str) -> str:
        if value != AUTO and not is_language(value):
            raise ValueError("unknown language")
        return value

    @field_validator("duration")
    @classmethod
    def _duration(cls, value: float) -> float:
        return _finite(value)

    @model_validator(mode="after")
    def _segments_in_order(self) -> Transcript:
        previous = 0.0
        for segment in self.segments:
            if segment.start < previous:
                raise ValueError("segments must be sorted by start")
            if segment.end > self.duration + _END_TOLERANCE:
                raise ValueError("a segment ends after the video")
            previous = segment.start
        return self


def dominant_language(segments: list[Segment]) -> str:
    """The language that covers the most speech, or "und" when none is known."""
    totals: dict[str, float] = {}
    for segment in segments:
        if segment.language is not None:
            totals[segment.language] = totals.get(segment.language, 0.0) + segment.end - segment.start
    return max(totals, key=totals.__getitem__) if totals else UNKNOWN_LANGUAGE


# --- cache file ---------------------------------------------------------------------------

def save_transcript(path: Path, transcript: Transcript) -> None:
    """Write the transcript as JSON, atomically (a reader never sees half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".transcript-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(transcript.model_dump_json())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_transcript(path: Path) -> Transcript | None:
    """The transcript cached at `path`, or None if it is missing, not a regular file, too
    large or invalid."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TRANSCRIPT_FILE_BYTES:
            return None
        return Transcript.model_validate_json(path.read_bytes())
    except (OSError, ValueError, ValidationError):
        return None


# --- subtitles ----------------------------------------------------------------------------

_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})")
_TIMING = re.compile(r"^\s*(\S+)\s+-->\s+(\S+)")
_TAG = re.compile(r"<[^>]*>")
_ASS_TAG = re.compile(r"\{\\[^}]*\}")
_SKIPPED_BLOCKS = ("NOTE", "STYLE", "REGION", "WEBVTT")


def _invalid_subtitles() -> AppError:
    return AppError("invalid_subtitles")


def _parse_time(text: str) -> float | None:
    match = _TIME.fullmatch(text)
    if match is None:
        return None
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        return None
    return (int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
            + int(fraction) / 10 ** len(fraction))


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _cues(text: str) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].lstrip().startswith(_SKIPPED_BLOCKS):
            continue
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        match = _TIMING.match(lines[timing])
        if match is None:
            continue
        start, end = _parse_time(match.group(1)), _parse_time(match.group(2))
        if start is None or end is None:
            continue
        body = " ".join(lines[timing + 1:])
        body = clean_text(html.unescape(_ASS_TAG.sub("", _TAG.sub("", body))))
        if body:
            cues.append((start, end, body))
    return cues


def parse_subtitles(data: bytes, *, filename: str | None, duration: float | None,
                    language: str) -> Transcript:
    """A transcript from an SRT or WebVTT file. Cues past `duration` are dropped and ends are
    pulled back to it; with no known duration, the last cue's end is used. `language` is
    "auto" (unknown) or a Whisper code. Raises invalid_subtitles when no cue is usable."""
    if language != AUTO and not is_language(language):
        raise AppError("invalid_request", "Choose a language from the list, or auto.")
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise _invalid_subtitles()
    tagged = None if language == AUTO else language
    segments = []
    for start, end, body in sorted(_cues(text), key=lambda cue: cue[0]):
        if duration is not None:
            if start >= duration:
                continue
            end = min(end, duration)
        if end > start:
            segments.append(Segment(start=round(start, 3), end=round(end, 3), text=body, language=tagged))
    if not segments:
        raise _invalid_subtitles()
    if len(segments) > MAX_SEGMENTS:
        raise AppError("invalid_subtitles", f"This subtitle file has more than {MAX_SEGMENTS:,} cues.")
    length = duration if duration is not None else max(s.end for s in segments)
    return Transcript(origin="subtitles", model=None, language=tagged or UNKNOWN_LANGUAGE,
                      language_probability=None, requested_language=language, duration=length,
                      segments=segments)
