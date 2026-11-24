"""Stand-ins for the renderer worker in RenderManager tests, and a runner that renders for real."""
import asyncio
import json
import shutil
from pathlib import Path

from app.runner import run_worker
from app.transcript import Segment, Transcript, Word, save_transcript


class RendererStub:
    """Mimics `python -m app.renderer` without FFmpeg: `render` emits progress and writes a small file named
    by `output`; `analyze` writes an analysis with no faces; `asset` decodes a logo with app.png or reports a
    video. Clips (1-based, by order of render calls) in `block` wait on `gate`; those in `fail` raise
    `error`; a source's download writes real bytes when `media` is given."""

    def __init__(self, *, block: tuple[int, ...] = (), fail: dict[int, Exception] | None = None,
                 media: Path | None = None, duration: float = 30.0, video_seconds: float = 5.0):
        self.block, self.fail = block, fail or {}
        self.gate = asyncio.Event()
        self.calls: list[dict] = []
        self.renders = 0
        self.media, self.duration, self.video_seconds = media, duration, video_seconds
        self.started = asyncio.Event()
        self.cancelled: list[int] = []

    async def __call__(self, request, *, cmd, timeout, on_event=None, workdir=None, max_workdir_bytes=None,
                       poll_interval=1.0):
        self.calls.append(request)
        mode = request["mode"]
        if mode == "download":
            target = workdir / "media.mp4"
            if self.media is not None:
                shutil.copy(self.media, target)
            else:
                target.write_bytes(b"\0" * 4096)
            return {"file": "media.mp4", "ext": "mp4", "title": "Stub Video", "duration": self.duration,
                    "notice": None}
        if mode == "analyze":
            on_event and on_event({"event": "progress", "stage": "analyzing", "percent": 50.0})
            (workdir / request["output"]).write_text(json.dumps({
                "version": 1, "width": 1280, "height": 720, "fps": request["fps"],
                "duration": request["end"] - request["start"], "samples": [], "cuts": [], "tracks": []}))
            return {"file": request["output"], "samples": 0, "faces": 0, "tracks": 0, "cuts": 0}
        if mode == "asset":
            if request["kind"] == "logo":
                from app.png import decode

                width, height, rgba = decode(Path(request["path"]).read_bytes())
                Path(request["path"]).with_suffix(".rgba").write_bytes(rgba)
                return {"kind": "logo", "width": width, "height": height,
                        "file": Path(request["path"]).with_suffix(".rgba").name}
            return {"kind": "video", "width": 640, "height": 360, "duration": self.video_seconds, "has_audio": True}
        assert mode == "render", mode
        self.renders += 1
        number = self.renders
        on_event({"event": "progress", "stage": "rendering", "percent": 0.0, "encoder": request["encoder"]})
        on_event({"event": "progress", "stage": "rendering", "percent": 50.0, "encoder": request["encoder"]})
        self.started.set()
        if number in self.block:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled.append(number)
                raise
        if number in self.fail:
            raise self.fail[number]
        (workdir / request["output"]).write_bytes(b"\0\0\0\x18ftypmp42" + bytes(1000))
        on_event({"event": "progress", "stage": "rendering", "percent": 100.0, "encoder": request["encoder"]})
        if request["kind"] == "frame":
            return {"file": request["output"], "width": 540, "height": 960, "mode": request["layout"]["mode"]}
        return {"file": request["output"], "width": 1080, "height": 1920, "duration": request["end"] - request["start"],
                "bytes": 1012, "fps": 30.0, "encoder": request["encoder"], "mode": request["layout"]["mode"],
                "notice": None, "video_codec": "h264", "audio_codec": "aac"}


class RealRenderRunner:
    """A source download copies a real video; every renderer mode runs the real worker."""

    def __init__(self, media: Path, duration: float):
        self.media, self.duration = media, duration
        self.calls: list[dict] = []

    async def __call__(self, request, *, cmd, timeout, on_event=None, workdir=None, max_workdir_bytes=None,
                       poll_interval=1.0):
        self.calls.append(request)
        if request["mode"] == "download":
            shutil.copy(self.media, workdir / "media.mp4")
            return {"file": "media.mp4", "ext": "mp4", "title": "Real Video", "duration": self.duration,
                    "notice": None}
        return await run_worker(request, cmd=cmd, timeout=timeout, on_event=on_event, workdir=workdir,
                                max_workdir_bytes=max_workdir_bytes, poll_interval=0.2)


def worded_transcript(duration: float, *, words_per_second: float = 2.0, with_words: bool = True,
                      language: str = "en", text: str | None = None) -> Transcript:
    """A transcript of 2-second segments of numbered words ("w0 w1 …"), with word times when asked."""
    segments, t, n = [], 0.0, 0
    while t < duration - 0.5:
        end = min(duration, t + 2.0)
        count = max(1, int((end - t) * words_per_second))
        step = (end - t) / count
        tokens = (text.split() if text else [f"w{n + i}" for i in range(count)])[:count]
        words = [Word(start=round(t + i * step, 3), end=round(t + (i + 0.8) * step, 3), text=tok)
                 for i, tok in enumerate(tokens)]
        segments.append(Segment(start=round(t, 3), end=round(end, 3), text=" ".join(tokens), language=language,
                                words=words if with_words else None))
        n += count
        t = end
    return Transcript(origin="whisper", model="base", language=language, language_probability=0.9,
                      requested_language="auto", duration=duration, segments=segments)


def put_transcript(source_folder: Path, transcript: Transcript) -> None:
    save_transcript(source_folder / "ai" / "transcript.json", transcript)
