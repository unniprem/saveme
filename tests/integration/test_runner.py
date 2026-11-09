import asyncio
import os
import subprocess
import sys
import time

import pytest

from app.errors import AppError
from app.runner import dir_size, run_worker


def fake(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def wait_dead(pid: int, limit: float = 8.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if not alive(pid):
            return True
        await asyncio.sleep(0.1)
    return False


SPAWN_CHILD = """
import subprocess, sys, time
p = subprocess.Popen(["sleep", "60"])
print(p.pid, flush=True)
time.sleep(60)
"""


async def test_result_and_events():
    script = (
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'event':'progress','detail':'x','downloaded_bytes':1,"
        "'total_bytes':2,'percent':50.0}), flush=True)\n"
        "print(json.dumps({'event':'result','data':{'ok':1}}), flush=True)\n"
    )
    events = []
    data = await run_worker({"a": 1}, cmd=fake(script), timeout=10, on_event=events.append)
    assert data == {"ok": 1}
    assert [e["event"] for e in events] == ["progress"]


async def test_request_is_sent_on_stdin():
    script = (
        "import sys, json\n"
        "req = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'event':'result','data':req}))\n"
    )
    assert await run_worker({"url": "u"}, cmd=fake(script), timeout=10) == {"url": "u"}


async def test_error_event_raises():
    script = (
        "import sys, json\n"
        "print(json.dumps({'event':'error','code':'unsupported','message':'nope'}))\n"
        "sys.exit(1)\n"
    )
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(script), timeout=10)
    assert ei.value.code == "unsupported"


async def test_unknown_error_code_is_internal():
    script = (
        "import sys, json\n"
        "print(json.dumps({'event':'error','code':'weird','message':'secret'}))\n"
        "sys.exit(1)\n"
    )
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(script), timeout=10)
    assert ei.value.code == "internal"
    assert "secret" not in ei.value.message


async def test_timeout_kills_group():
    start = time.monotonic()
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(SPAWN_CHILD), timeout=1, poll_interval=0.2)
    assert ei.value.code == "timeout"
    assert time.monotonic() - start < 8


async def test_timeout_child_is_dead(tmp_path):
    pidfile = tmp_path / "pid"
    script = (
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep','60'])\n"
        f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(script), timeout=1, poll_interval=0.2)
    assert ei.value.code == "timeout"
    assert await wait_dead(int(pidfile.read_text()))


async def test_workdir_growth_kills(tmp_path):
    script = (
        "import time\n"
        "open('big.bin','wb').write(b'0'*(2*1024*1024))\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(script), timeout=30, workdir=tmp_path,
                         max_workdir_bytes=1024 * 1024, poll_interval=0.1)
    assert ei.value.code == "too_large"
    assert "limit" in ei.value.message


async def test_garbage_stdout_is_internal():
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake("print('hello')"), timeout=10)
    assert ei.value.code == "internal"


async def test_abnormal_exit_is_internal_and_hides_stderr(caplog):
    script = "import sys; sys.stderr.write('SECRET-TRACE'); sys.exit(3)"
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(script), timeout=10)
    assert ei.value.code == "internal"
    assert "SECRET-TRACE" not in ei.value.message
    assert "SECRET-TRACE" in caplog.text


async def test_overlong_line_is_internal():
    script = "print('x' * (2*1024*1024))"
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(script), timeout=10)
    assert ei.value.code == "internal"


async def test_proxy_env_stripped(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://x:1")
    monkeypatch.setenv("http_proxy", "http://x:1")
    script = (
        "import os, json\n"
        "print(json.dumps({'event':'result','data':{'keys':list(os.environ)}}))\n"
    )
    data = await run_worker({}, cmd=fake(script), timeout=10)
    assert "HTTPS_PROXY" not in data["keys"]
    assert "http_proxy" not in data["keys"]


async def test_cancellation_kills_worker(tmp_path):
    pidfile = tmp_path / "pid"
    script = (
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep','60'])\n"
        f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    task = asyncio.create_task(run_worker({}, cmd=fake(script), timeout=60))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.1)
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_dead(pid)


def test_dir_size(tmp_path):
    assert dir_size(tmp_path / "missing") == 0
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "b").write_bytes(b"123")
    assert dir_size(tmp_path) == 8


async def test_nonexistent_cmd_is_internal():
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=["/nonexistent/worker"], timeout=5)
    assert ei.value.code == "internal"
    assert "/nonexistent" not in ei.value.message


async def test_nonexistent_workdir_is_internal(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake("pass"), timeout=5, workdir=missing)
    assert ei.value.code == "internal"
    assert str(tmp_path) not in ei.value.message


async def test_error_event_with_unhashable_code_is_internal():
    script = (
        "import sys, json\n"
        "print(json.dumps({'event':'error','code':[1,2],'message':['x']}))\n"
        "sys.exit(1)\n"
    )
    with pytest.raises(AppError) as ei:
        await run_worker({}, cmd=fake(script), timeout=10)
    assert ei.value.code == "internal"
