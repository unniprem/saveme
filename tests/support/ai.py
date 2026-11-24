"""Helpers for the AI Highlights tests: a stand-in for the transcriber worker run and transcripts."""
import asyncio
import json

from app.transcript import Segment, Transcript, save_transcript


def speech_transcript(seconds: float = 300.0, *, step: float = 5.0, language: str = "en",
                      duration: float | None = None, origin: str = "whisper",
                      requested: str = "auto") -> Transcript:
    segments, t = [], 0.0
    while t < seconds:
        segments.append(Segment(start=t, end=min(seconds, t + step), text=f"words at {int(t)}",
                                language=language))
        t += step
    return Transcript(origin=origin, model="base" if origin == "whisper" else None, language=language,
                      language_probability=0.9 if origin == "whisper" else None,
                      requested_language=requested, duration=duration or seconds, segments=segments)


class TranscriberStub:
    """Stand-in for run_worker running app.transcriber: emits the phases and progress the real
    worker does, writes workdir/audio.wav, then (after `gate` when `block`) replaces it with
    transcript.json holding `transcript`, or raises `error`."""

    def __init__(self, transcript: Transcript | None = None, *, block: bool = False,
                 error: BaseException | None = None):
        self.transcript = transcript or speech_transcript()
        self.block = block
        self.error = error
        self.gate = asyncio.Event()
        self.calls: list[dict] = []
        self.kwargs: list[dict] = []
        self.cancelled = False
        self.started = asyncio.Event()

    async def __call__(self, request, *, cmd, timeout, on_event=None, workdir=None,
                       max_workdir_bytes=None, poll_interval=1.0):
        self.calls.append(request)
        self.kwargs.append({"cmd": cmd, "timeout": timeout, "workdir": workdir,
                            "max_workdir_bytes": max_workdir_bytes})
        on_event({"event": "phase", "phase": "extracting", "detail": "Extracting audio"})
        on_event({"event": "progress", "stage": "extracting", "percent": 100.0})
        (workdir / "audio.wav").write_bytes(b"\0" * 128)
        on_event({"event": "phase", "phase": "transcribing", "detail": "Loading the transcription model"})
        on_event({"event": "progress", "stage": "transcribing", "percent": 40.0,
                  "detail": "Transcribing (English)"})
        self.started.set()
        try:
            if self.block:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        (workdir / "audio.wav").unlink()
        save_transcript(workdir / "transcript.json", self.transcript)
        return {"file": "transcript.json", "segments": len(self.transcript.segments),
                "language": self.transcript.language, "duration": self.transcript.duration}


def clips_answer(*ranges: tuple[int, int, int]) -> dict:
    """An OpenRouter completion choosing (first segment, last segment, score) clips."""
    from tests.support.openrouter import completion

    return completion({"clips": [{"title": f"Clip {a}-{b}", "start_segment": a, "end_segment": b,
                                  "score": score, "summary": "A summary.", "reason": "A reason.",
                                  "tags": ["Tag"]} for a, b, score in ranges]})


def dump(value) -> str:
    return json.dumps(value, sort_keys=True)
