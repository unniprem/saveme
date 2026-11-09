"""Run a worker entry module as a real subprocess, the way the runner does."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Every raw stdout line from every run_worker_proc call in this session.
STDOUT_LINES: list[str] = []


def worker_env() -> dict[str, str]:
    """The current environment minus every *_proxy variable (any case)."""
    return {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}


def run_worker_proc(request: dict, *, entry: str = "tests.support.worker_entry",
                    timeout: float = 60) -> tuple[list[dict], int]:
    """Send `request` on stdin; return (stdout events, exit code). Fails the test
    if the worker overruns `timeout` or writes anything but JSON objects to stdout."""
    with subprocess.Popen([sys.executable, "-m", entry], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT,
                          env=worker_env(), start_new_session=True) as proc:
        try:
            out, err = proc.communicate(json.dumps(request).encode(), timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:  # also reaps anything the worker left running in its group
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if timed_out:
            out, err = proc.communicate()
    stderr = err.decode(errors="replace")
    sys.stderr.write(stderr)  # shown by pytest when the test fails
    if timed_out:
        pytest.fail(f"worker {entry} ran longer than {timeout}s")
    lines = out.decode(errors="replace").splitlines()
    STDOUT_LINES.extend(lines)
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            pytest.fail(f"worker stdout line is not a JSON object: {line!r}")
        events.append(event)
    return events, proc.returncode
