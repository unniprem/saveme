"""Test-only transcriber entry (`python -m tests.support.transcriber_entry`).

Same as `python -m app.transcriber`, except that faster-whisper is replaced by a fake model
(no download, instant) unless SAVEME_TEST_REAL_WHISPER=1. The fake returns one segment per
whole second of each window it is given, "second <n>", with times relative to the window, as
faster-whisper does. Knobs (environment):

- SAVEME_TEST_WINDOW_SECONDS, SAVEME_TEST_SEARCH_SECONDS, SAVEME_TEST_MERGE_SECONDS: smaller windows
- SAVEME_TEST_WHISPER_LANGS: comma-separated languages the fake "detects", one per window
- SAVEME_TEST_WHISPER_DELAY: seconds the fake sleeps per window (cancellation tests)
- SAVEME_TEST_WHISPER_MISSING=1: the model isn't installed
- SAVEME_TEST_WHISPER_LOG: a file the fake appends each call's options to, as JSON lines
- SAVEME_TEST_FFMPEG_DIR: where the app/bin wrappers find the real ffmpeg/ffprobe
"""
import json
import os
import sys
import time
from dataclasses import dataclass


@dataclass
class _Segment:
    start: float
    end: float
    text: str


@dataclass
class _Info:
    language: str
    language_probability: float
    duration: float


class FakeModel:
    def __init__(self) -> None:
        langs = os.environ.get("SAVEME_TEST_WHISPER_LANGS", "en")
        self.langs = [lang for lang in langs.split(",") if lang]
        self.calls = 0

    def transcribe(self, audio, language=None, **options):
        length = len(audio) / 16000
        detected = language or self.langs[min(self.calls, len(self.langs) - 1)]
        self.calls += 1
        log = os.environ.get("SAVEME_TEST_WHISPER_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as out:
                out.write(json.dumps({"language": language, "seconds": round(length, 3),
                                      "dtype": str(audio.dtype), **options}) + "\n")
        delay = float(os.environ.get("SAVEME_TEST_WHISPER_DELAY", "0"))

        def segments():
            for second in range(int(length)):
                if delay:
                    time.sleep(delay)
                yield _Segment(second + 0.1, second + 0.9, f"  second\n{second} ")

        return segments(), _Info(detected, 0.9, length)


def main() -> None:
    ffmpeg_dir = os.environ.get("SAVEME_TEST_FFMPEG_DIR")
    if ffmpeg_dir:
        os.environ["SAVEME_FFMPEG_DIR"] = ffmpeg_dir
    from app import transcriber

    for name, var in (("WINDOW_SECONDS", "SAVEME_TEST_WINDOW_SECONDS"),
                      ("SEARCH_SECONDS", "SAVEME_TEST_SEARCH_SECONDS"),
                      ("MERGE_TAIL_SECONDS", "SAVEME_TEST_MERGE_SECONDS")):
        if os.environ.get(var):
            setattr(transcriber, name, float(os.environ[var]))
    if os.environ.get("SAVEME_TEST_REAL_WHISPER") != "1":
        def load_model(name: str, cpu_threads: int):
            if os.environ.get("SAVEME_TEST_WHISPER_MISSING") == "1":
                raise transcriber.missing_model(name)
            return FakeModel()

        transcriber.load_model = load_model
    sys.exit(transcriber.main())


if __name__ == "__main__":
    main()
