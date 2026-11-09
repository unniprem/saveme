"""Parent-side runner for the isolated download/inspect worker subprocess."""
import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from app.errors import CATALOG, AppError, too_large_message

logger = logging.getLogger(__name__)

DEFAULT_WORKER_CMD: tuple[str, ...] = (sys.executable, "-m", "app.worker")

RunWorker = Callable[..., Awaitable[dict]]

_LINE_LIMIT = 1024 * 1024
_STDERR_KEEP = 64 * 1024
_TERM_GRACE = 5.0


def dir_size(path: Path) -> int:
    """Total size in bytes of all files under path (0 if missing)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), _TERM_GRACE)
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()


async def run_worker(
    request: dict,
    *,
    cmd: Sequence[str],
    timeout: float,
    on_event: Callable[[dict], None] | None = None,
    workdir: Path | None = None,
    max_workdir_bytes: int | None = None,
    poll_interval: float = 1.0,
) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=_clean_env(),
        cwd=str(workdir) if workdir else tempfile.gettempdir(),
        limit=_LINE_LIMIT + 1,
    )
    stderr_tail = bytearray()
    kill_reason: list[AppError] = []

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(8192):
            stderr_tail.extend(chunk)
            del stderr_tail[:-_STDERR_KEEP]

    async def watchdog() -> None:
        deadline = time.monotonic() + timeout
        while True:
            await asyncio.sleep(max(0.0, min(poll_interval, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                kill_reason.append(AppError("timeout"))
                break
            if workdir is not None and max_workdir_bytes is not None:
                if await asyncio.to_thread(dir_size, workdir) > max_workdir_bytes:
                    kill_reason.append(
                        AppError("too_large", too_large_message(max_workdir_bytes // 2)))
                    break
        await _kill_group(proc)

    async def converse() -> dict:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(json.dumps(request).encode() + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        result: dict | None = None
        error: dict | None = None
        garbage = False
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:  # line longer than the stream limit
                logger.error("worker stdout line exceeded %d bytes", _LINE_LIMIT)
                raise AppError("internal")
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except ValueError:
                garbage = True
                continue
            if not isinstance(event, dict):
                garbage = True
                continue
            kind = event.get("event")
            if kind in ("progress", "phase"):
                if on_event is not None:
                    on_event(event)
            elif kind == "result" and isinstance(event.get("data"), dict):
                result = event["data"]
            elif kind == "error":
                error = event
            else:
                garbage = True
        await proc.wait()
        if error is not None:
            code = error.get("code")
            if code not in CATALOG:
                code = "internal"
            message = error.get("message")
            raise AppError(code, message if isinstance(message, str) and code != "internal" else None)
        if garbage or result is None or proc.returncode != 0:
            raise AppError("internal")
        return result

    stderr_task = asyncio.create_task(drain_stderr())
    watch_task = asyncio.create_task(watchdog())
    main_task = asyncio.create_task(converse())
    try:
        await asyncio.wait({main_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        if kill_reason:
            raise kill_reason[0]
        return main_task.result()
    except AppError as exc:
        if exc.code == "internal":
            tail = bytes(stderr_tail).decode(errors="replace")
            logger.warning("worker failed (rc=%s); stderr tail: %s", proc.returncode, tail)
        raise
    finally:
        watch_task.cancel()
        main_task.cancel()
        await _kill_group(proc)
        stderr_task.cancel()
        await asyncio.gather(watch_task, main_task, stderr_task, return_exceptions=True)
