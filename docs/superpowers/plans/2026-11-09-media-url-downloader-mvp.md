# Media URL Downloader (saveme), Build 1 MVP: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local Docker web app where the owner pastes a media URL, inspects it, and saves the video (MP4) or audio (MP3), with strict SSRF protection, real progress, and automatic cleanup.

**Architecture:**
- **API server:** FastAPI. It validates URLs, owns an in-memory `JobManager`, and never fetches remote URLs itself.
- **Worker:** each inspect or download runs `python -m app.worker` in its own process group. The worker installs a connect-time IP guard before yt-dlp runs, uses only yt-dlp's native downloaders, and reaches ffmpeg/ffprobe through wrappers that pin `-protocol_whitelist file,pipe`.
- **Files:** they live in `TEMP_DIR/<job_id>/` under fixed names and are streamed to the browser, then deleted.

**Tech Stack:**
- Python 3.12, FastAPI, Uvicorn, `yt-dlp[default]==2026.10.26`
- FFmpeg, Deno (YouTube JS challenges)
- Vanilla JS, with Tailwind v4 compiled by the standalone CLI
- pytest and pytest-asyncio
- Docker Compose

**Spec:** `docs/superpowers/specs/2026-11-09-media-url-downloader-design.md`, which builds on `Media_URL_Downloader_BRD.md`. Read both before starting any task.

## Global Constraints

- Python **3.12**. Image `python:3.12-slim-trixie`. Local venv: `uv venv --python 3.12 .venv && uv pip install -r requirements-dev.txt`.
- **Never** add PostgreSQL, Redis, Celery, React, Playwright, telemetry, cloud services, logins, or **curl_cffi**. Install yt-dlp as `yt-dlp[default]` only.
- **Subprocesses:**
  - Never use `shell=True`. Pass arguments as lists.
  - Workers start with `start_new_session=True` and are killed by process group.
  - Every worker environment drops all `*_proxy` / `*_PROXY` variables, matched case-insensitively.
- The client sends only `url`, `output ∈ {video, audio}`, and `quality ∈ {best, 1080, 720, 480}`. Pydantic `extra="forbid"`.
- **Error body**, always: `{"error": {"code": "<code>", "message": "<plain sentence>"}}`. It never contains stack traces, filesystem paths, or the `TEMP_DIR` value.
- **Job IDs:** `secrets.token_urlsafe(16)`, validated against `^[A-Za-z0-9_-]{22}$` before any filesystem use. On-disk names are fixed (`media.<ext>`). Titles never touch the filesystem.
- **Defaults** (spec §8):

  | Variable | Default |
  |---|---|
  | `MAX_ACTIVE_JOBS` | 2 |
  | `MAX_QUEUED_JOBS` | 4 |
  | `MAX_ACTIVE_INSPECTIONS` | 4 |
  | `INSPECT_TIMEOUT_SECONDS` | 30 |
  | `MAX_FILE_SIZE_MB` | 2048 |
  | `MAX_JOB_SECONDS` | 1800 |
  | `JOB_TTL_MINUTES` | 30 |
  | `MAX_READY_FILES` | 6 |
  | `MAX_TOTAL_DISK_MB` | 10240 |
  | `TEMP_DIR` | `/tmp/media-jobs` |
  | `LOG_LEVEL` | `info` |
  | `ALLOWED_HOSTS` | `localhost,127.0.0.1` |

- **Pinned copy:**
  - Footer: `Only download media you have the right to save. Works with many sites, not all — compatibility depends on the source.`
  - Messages:
    - `Links with login details aren't supported.`
    - `Live streams aren't supported.`
    - `This media has no audio track.`
    - `Server storage is full, try again later.`
  - Downgrade notice: `{req}p wasn't available; downloaded {got}p.`
- **Docker:** port bound to `127.0.0.1:8000:8000`, runs as uid 10001, no volumes required, `docker compose up -d --build` works with no `.env` present.
- **Commits:**
  - One commit per task.
  - Conventional message (`feat:`, `test:`, `docs:`, `chore:`).
  - Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Work on branch `feature/media-url-downloader-mvp`.
- The default `pytest` run is offline. Real-site tests carry `@pytest.mark.network` and are excluded by `addopts`.

## Review Focus

1. **Pasted share text.** Users paste things like `"Look at this! https://x.com/a/status/1 "`. The single http(s) URL should be extracted. If the text contains zero URLs, or several, it is `invalid_url`. *Task 3 test `test_extracts_single_url_from_share_text`.*
2. **The job vanished while the page is polling** (container restart or record purge). `GET /api/jobs/{id}` returns 404 `job_not_found`. The page then says "This download was lost (the server may have restarted). Please start again." and resets. *Task 12 test `test_unknown_job_is_404_job_not_found`; Task 13 manual check M5.*
3. **Non-Latin or emoji titles.** The saved filename keeps them via `filename*=UTF-8''…`. The ASCII fallback is readable or `download`, never empty or broken. *Task 5 test `test_unicode_title_disposition`.*
4. **Double-click and reload during a job.** The download buttons are disabled while a job is active, so a double-click doesn't create a second job. Reloading the page resumes polling the same job, using its ID from `sessionStorage`. *Task 13 manual checks M3 and M4.*
5. **Long media over the size cap.** The message names the limit and suggests a fix, for example `This file is larger than the 2 GB limit. Try a lower quality or audio only.` *Task 2 test `test_too_large_message`; Task 11 test `test_oversize_job_fails_cleanly`.*

---

## File Structure

```text
app/
  __init__.py
  __main__.py        python -m app → uvicorn with LOG_LEVEL (Task 12)
  config.py          Settings, load_settings, ConfigError (Task 1)
  schemas.py         Output/Quality literals, request models (Task 1)
  errors.py          AppError, CATALOG, classify_ytdlp_error, too_large_message (Task 2)
  netguard.py        is_public_ip, check_url_syntax, validate_source_url (Task 3); install_connect_guard (Task 4)
  filenames.py       sanitize_filename, content_disposition, media_type_for (Task 5)
  ytdl.py            yt-dlp options, format safety, inspect mapping, selection helpers (Task 6)
  bin/_ffwrap.py     whitelist injection + real-binary lookup (Task 7)
  bin/ffmpeg         executable wrapper (Task 7)
  bin/ffprobe        executable wrapper (Task 7)
  worker.py          subprocess entry: inspect (Task 8), download (Task 9)
  runner.py          parent-side worker process control (Task 10)
  jobs.py            Job, JobManager, prepare_temp_dir, dir_size (Task 11)
  main.py            create_app, routes, middleware, delivery (Task 12)
static/
  index.html, app.js, src/input.css           (Task 13; dist/styles.css built, git-ignored)
scripts/
  build-css.sh       pinned Tailwind standalone CLI, checksum-verified (Task 13)
  docker-smoke.sh    (Task 14)
tests/
  conftest.py
  support/{__init__,media,server,policy,worker_entry,procs}.py   (Task 8)
  unit/test_config.py, test_errors.py, test_netguard.py, test_filenames.py, test_ytdl.py, test_ffwrap.py, test_packaging.py
  integration/test_connect_guard.py, test_ffwrap_canary.py, test_worker_inspect.py, test_worker_download.py, test_runner.py, test_jobs.py, test_api.py
  network/urls.py, network/test_real_sites.py   (Task 15)
Dockerfile, compose.yaml, .dockerignore, .env.example, .gitignore
pyproject.toml, requirements.in, requirements.txt, requirements-dev.in, requirements-dev.txt
README.md, JOURNAL.md
```

---

### Task 1: Project scaffold, settings, request schemas

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `requirements.in`, `requirements.txt`, `requirements-dev.in`, `requirements-dev.txt`, `app/__init__.py`, `app/config.py`, `app/schemas.py`, `tests/__init__.py`, `tests/unit/__init__.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - `Settings` is a frozen dataclass with these fields:
    - `max_active_jobs`, `max_queued_jobs`, `max_active_inspections`, `inspect_timeout_seconds`
    - `max_file_size_mb`, `max_job_seconds`, `job_ttl_minutes`, `max_ready_files`, `max_total_disk_mb`
    - `temp_dir: Path`, `log_level: str`, `allowed_hosts: tuple[str, ...]`

    It also has these properties: `max_file_bytes`, `job_ttl_seconds`, `max_total_disk_bytes`.
  - `ConfigError(Exception)` with attributes `.var` and `.message`. `str()` includes `var`.
  - `load_settings(env: Mapping[str, str]) -> Settings`.
  - In `app/schemas.py`:
    - `Output = Literal["video", "audio"]` and `Quality = Literal["best", "1080", "720", "480"]`
    - `InspectRequest(url: str)`
    - `JobRequest(url: str, output: Output, quality: Quality = "best")`
    - Both models use `ConfigDict(extra="forbid")`.

- [ ] **Step 1: Write the scaffold files.**
  - `.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.cache/`, `static/dist/`, `.env`.
  - `pyproject.toml`:
    - `[project] name="saveme" requires-python=">=3.12"`
    - `[tool.pytest.ini_options]`: `testpaths=["tests"]`, `asyncio_mode="auto"`, `addopts="-m 'not network'"`, `markers=["network: hits real public sites (opt-in)"]`
  - `requirements.in`: `fastapi`, `uvicorn`, `yt-dlp[default]==2026.10.26`.
  - `requirements-dev.in`: `-r requirements.txt`, `pytest`, `pytest-asyncio`, `httpx`.
  - Compile both:
    `uv pip compile requirements.in -o requirements.txt --python-version 3.12 && uv pip compile requirements-dev.in -o requirements-dev.txt --python-version 3.12`.
    Then create the venv and install as in Global Constraints. Verify `curl_cffi` is absent: `grep -i curl requirements.txt` prints nothing.

- [ ] **Step 2: Write the failing tests in `tests/unit/test_config.py`.**

```python
from pathlib import Path
import pytest
from app.config import load_settings, ConfigError

def test_defaults_when_env_empty():
    s = load_settings({})
    assert (s.max_active_jobs, s.max_queued_jobs, s.max_active_inspections) == (2, 4, 4)
    assert (s.inspect_timeout_seconds, s.max_file_size_mb, s.max_job_seconds) == (30, 2048, 1800)
    assert (s.job_ttl_minutes, s.max_ready_files, s.max_total_disk_mb) == (30, 6, 10240)
    assert s.temp_dir == Path("/tmp/media-jobs") and s.log_level == "info"
    assert s.allowed_hosts == ("localhost", "127.0.0.1")
    assert s.max_file_bytes == 2048 * 1024 * 1024 and s.job_ttl_seconds == 1800

def test_env_overrides_and_blank_means_default():
    s = load_settings({"MAX_ACTIVE_JOBS": "3", "TEMP_DIR": "/data/jobs", "MAX_QUEUED_JOBS": "",
                       "ALLOWED_HOSTS": "localhost, saveme.lan"})
    assert s.max_active_jobs == 3 and s.temp_dir == Path("/data/jobs") and s.max_queued_jobs == 4
    assert s.allowed_hosts == ("localhost", "saveme.lan")

@pytest.mark.parametrize("var,value", [
    ("MAX_ACTIVE_JOBS", "0"), ("MAX_ACTIVE_JOBS", "abc"), ("MAX_FILE_SIZE_MB", "-1"),
    ("INSPECT_TIMEOUT_SECONDS", "1"), ("TEMP_DIR", "relative/path"), ("TEMP_DIR", "/"),
    ("LOG_LEVEL", "loud"), ("ALLOWED_HOSTS", " , "),
])
def test_invalid_values_name_the_variable(var, value):
    with pytest.raises(ConfigError) as e:
        load_settings({var: value})
    assert e.value.var == var and var in str(e.value)

def test_disk_budget_must_hold_one_reservation():
    with pytest.raises(ConfigError) as e:
        load_settings({"MAX_FILE_SIZE_MB": "2048", "MAX_TOTAL_DISK_MB": "4000"})
    assert e.value.var == "MAX_TOTAL_DISK_MB"
```

- [ ] **Step 3: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/unit/test_config.py -v`. Expected: an ImportError (no `app.config`).

- [ ] **Step 4: Implement `load_settings` and the schemas.**
  - **Bounds** (inclusive):

    | Variable | Allowed range |
    |---|---|
    | `MAX_ACTIVE_JOBS` | 1–16 |
    | `MAX_QUEUED_JOBS` | 0–100 |
    | `MAX_ACTIVE_INSPECTIONS` | 1–32 |
    | `INSPECT_TIMEOUT_SECONDS` | 5–300 |
    | `MAX_FILE_SIZE_MB` | 1–102400 |
    | `MAX_JOB_SECONDS` | 10–86400 |
    | `JOB_TTL_MINUTES` | 1–1440 |
    | `MAX_READY_FILES` | 1–100 |

  - **Cross-field checks:**
    - `MAX_TOTAL_DISK_MB` ≥ 2×`MAX_FILE_SIZE_MB`.
    - `TEMP_DIR` must be absolute and must not be `/`.
    - `LOG_LEVEL` ∈ {debug, info, warning, error}.
    - `ALLOWED_HOSTS` is split on commas and stripped, and the result must be non-empty.
  - **Blank values:** an empty string means "unset".
  - **Schemas:** write `app/schemas.py` per the Interfaces block.

- [ ] **Step 5: Run the tests and confirm they pass.** Run `.venv/bin/pytest tests/unit/test_config.py -v`. Expected: all pass.

- [ ] **Step 6: Commit.** Run `git add` on the files above, then `git commit` with the message `chore: scaffold project, settings and request schemas`.

---

### Task 2: Error catalogue and yt-dlp error classification

**Files:**
- Create: `app/errors.py`, `tests/unit/test_errors.py`

**Interfaces:**
- Produces:
  - `AppError(Exception)`:
    - Constructor: `AppError(code: str, message: str | None = None, status: int | None = None)`. A missing message or status falls back to the catalogue.
    - Attributes: `.code`, `.message`, `.status`.
    - `to_dict() -> {"code", "message"}`.
  - `CATALOG: dict[str, tuple[int, str]]`.
  - `classify_ytdlp_error(text: str) -> AppError`.
  - `too_large_message(limit_bytes: int) -> str`.
  - `BLOCKED_MARKER = "[saveme-blocked]"`.
- Catalogue: each code with its HTTP status and default message. The builder writes the messages, except the pinned ones in Global Constraints.

  | Code | Status | Default message (builder writes unless pinned) |
  |---|---|---|
  | `invalid_url` | 400 | |
  | `invalid_request` | 400 | |
  | `blocked_address` | 403 | `This link points to a private or local network address, which isn't allowed.` |
  | `forbidden_origin` | 403 | |
  | `job_not_found` | 404 | |
  | `not_ready` | 409 | |
  | `expired` | 410 | |
  | `unsupported` | 422 | |
  | `login_required` | 422 | |
  | `unavailable` | 422 | |
  | `geo_blocked` | 422 | |
  | `drm` | 422 | |
  | `live_unsupported` | 422 | |
  | `playlist_unsupported` | 422 | |
  | `no_audio` | 422 | |
  | `no_video` | 422 | |
  | `extraction_failed` | 422 | |
  | `too_large` | 413 | |
  | `busy` | 429 | |
  | `rate_limited` | 429 | |
  | `internal` | 500 | |
  | `unreachable` | 502 | |
  | `disk_full` | 503 | |
  | `config_error` | 503 | |
  | `timeout` | 504 | |

- [ ] **Step 1: Write the failing tests.**

```python
import pytest
from app.errors import AppError, classify_ytdlp_error, too_large_message, BLOCKED_MARKER

@pytest.mark.parametrize("text,code", [
    ("ERROR: Unsupported URL: https://example.com/", "unsupported"),
    ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate", "login_required"),
    ("ERROR: [youtube] x: Video unavailable. This video is private", "login_required"),
    ("ERROR: [Instagram] x: Requested content is not available, rate-limit reached or login required", "login_required"),
    ("ERROR: [youtube] x: Video unavailable", "unavailable"),
    ("ERROR: [generic] Unable to download webpage: HTTP Error 404: Not Found", "unavailable"),
    ("ERROR: [generic] Unable to download webpage: HTTP Error 429: Too Many Requests", "rate_limited"),
    ("ERROR: [youtube] x: The uploader has not made this video available in your country", "geo_blocked"),
    ("ERROR: [x] y: This video is DRM protected", "drm"),
    ("ERROR: [youtube] x: This live event will begin in 3 hours", "live_unsupported"),
    ("ERROR: [generic] Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided>", "unreachable"),
    (f"ERROR: [generic] Unable to download webpage: {BLOCKED_MARKER} 10.0.0.1 is not public", "blocked_address"),
    ("ERROR: something nobody anticipated", "extraction_failed"),
])
def test_classify(text, code):
    assert classify_ytdlp_error(text).code == code

def test_catalogue_defaults_and_shape():
    e = AppError("busy")
    assert e.status == 429 and e.message and e.to_dict() == {"code": "busy", "message": e.message}
    assert AppError("no_audio").message == "This media has no audio track."

def test_too_large_message():
    m = too_large_message(2048 * 1024 * 1024)
    assert "2 GB" in m and "lower quality" in m
    assert "1 MB" in too_large_message(1024 * 1024)
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/unit/test_errors.py -v`. Expected: an ImportError.

- [ ] **Step 3: Implement.** `classify_ytdlp_error` lowercases the text and checks keyword groups in this exact order. The first match wins.
  1. blocked (`BLOCKED_MARKER`)
  2. drm
  3. live (`live event`, `is live`, `live stream`)
  4. login (`sign in`, `login`, `log in`, `private`, `cookies`, `members-only`, `confirm your age`)
  5. geo (`your country`, `geo`)
  6. rate (`429`, `too many requests`)
  7. unavailable (`video unavailable`, `404`, `has been removed`, `does not exist`)
  8. unsupported (`unsupported url`, `no video formats`, `no media`)
  9. unreachable (`nodename`, `name or service not known`, `connection refused`, `network is unreachable`, `timed out`, `unable to download webpage`)
  10. default: `extraction_failed`

  `too_large_message` formats whole GB when the limit is ≥1024 MB, otherwise MB, as `This file is larger than the {N} {unit} limit. Try a lower quality or audio only.`

- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/pytest tests/unit/test_errors.py -v`. Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add error catalogue and yt-dlp error classification`.

---

### Task 3: IP policy and URL pre-validation (layer 1)

**Files:**
- Create: `app/netguard.py` (policy and validation only; the guard comes in Task 4), `tests/unit/test_netguard.py`

**Interfaces:**
- Consumes: `AppError` (Task 2).
- Produces:
  - `IPAddress = IPv4Address | IPv6Address`; `IpPolicy = Callable[[IPAddress], bool]`
  - `is_public_ip(ip: IPAddress) -> bool`
  - `check_url_syntax(raw: str) -> str` returns the cleaned URL or raises `AppError("invalid_url")`
  - `Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]`
  - `async def system_resolver(host: str, port: int) -> list[IPAddress]`
  - `async def validate_source_url(raw: str, *, policy: IpPolicy = is_public_ip, resolver: Resolver = system_resolver, timeout: float = 5.0) -> str`

- [ ] **Step 1: Write the failing tests.**

```python
import socket, ipaddress, asyncio, pytest
from app.netguard import is_public_ip, check_url_syntax, validate_source_url
from app.errors import AppError

BLOCKED = ["127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.5.4", "192.168.1.1", "100.64.0.1",
           "169.254.169.254", "0.0.0.0", "224.0.0.1", "255.255.255.255", "192.0.2.1", "198.18.0.1",
           "::1", "::", "fe80::1", "fc00::1", "fd00:ec2::254", "ff02::1", "::ffff:127.0.0.1",
           "::ffff:10.0.0.1", "::127.0.0.1", "64:ff9b::a00:1", "2002:7f00:1::1", "2001:db8::1"]
ALLOWED = ["93.184.215.14", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"]

@pytest.mark.parametrize("ip", BLOCKED)
def test_blocked_ips(ip): assert not is_public_ip(ipaddress.ip_address(ip))

@pytest.mark.parametrize("ip", ALLOWED)
def test_allowed_ips(ip): assert is_public_ip(ipaddress.ip_address(ip))

@pytest.mark.parametrize("raw", ["", "not a url", "example.com/video", "file:///etc/passwd",
    "ftp://example.com/a.mp4", "javascript:alert(1)", "data:text/html,hi", "http://",
    "http://user:pass@example.com/v", "http://example.com/\x00", "https://example.com/" + "a" * 2100,
    "http://example.com:99999/", "two https://a.com/x links https://b.com/y"])
def test_rejects_invalid(raw):
    with pytest.raises(AppError) as e: check_url_syntax(raw)
    assert e.value.code == "invalid_url"

def test_userinfo_message():
    with pytest.raises(AppError) as e: check_url_syntax("https://u:p@example.com/")
    assert e.value.message == "Links with login details aren't supported."

def test_strips_surrounding_whitespace():
    assert check_url_syntax("  https://example.com/v?id=1 \n") == "https://example.com/v?id=1"

def test_extracts_single_url_from_share_text():
    assert check_url_syntax("Look at this! https://x.com/a/status/1 ") == "https://x.com/a/status/1"

def stub(*ips):
    async def r(host, port): return [ipaddress.ip_address(i) for i in ips]
    return r

async def test_accepts_public(): assert await validate_source_url("https://example.com/v", resolver=stub("93.184.215.14")) == "https://example.com/v"

async def test_any_private_answer_blocks():
    with pytest.raises(AppError) as e: await validate_source_url("https://example.com/", resolver=stub("8.8.8.8", "10.0.0.1"))
    assert (e.value.code, e.value.status) == ("blocked_address", 403)

@pytest.mark.parametrize("raw", ["http://2130706433/", "http://127.1/", "http://[::1]:8000/", "http://169.254.169.254/latest/meta-data/"])
async def test_numeric_and_literal_hosts_blocked(raw):
    with pytest.raises(AppError) as e: await validate_source_url(raw)   # system resolver; numeric forms resolve offline
    assert e.value.code == "blocked_address"

async def test_unresolvable_and_empty():
    async def boom(h, p): raise socket.gaierror("nope")
    async def slow(h, p): await asyncio.sleep(10)
    for r in (boom, stub(), slow):
        with pytest.raises(AppError) as e: await validate_source_url("https://nx.example/", resolver=r, timeout=0.2)
        assert e.value.code == "unreachable"
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/unit/test_netguard.py -v`. Expected: an ImportError.

- [ ] **Step 3: Implement.**
  - **`is_public_ip`:**
    - Unwrap IPv4-mapped (`.ipv4_mapped`), IPv4-compatible (any address in `::/96` other than `::` and `::1` → `IPv4Address(int(ip) & 0xFFFFFFFF)`), 6to4 (`.sixtofour`), Teredo (`.teredo[1]`) and NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48` → the low 32 bits).
    - Every address checked, including the embedded one, must have `is_global` true and `is_multicast` false.
  - **`check_url_syntax`:**
    - Strip the input.
    - If it still contains whitespace, find the `https?://\S+` tokens. Exactly one token must be found; use it.
    - Reject anything over 2048 characters or containing control characters.
    - Parse with `urllib.parse.urlsplit`. The scheme must be in {http, https}, the hostname must be non-empty, there must be no username or password, and accessing `.port` must not raise.
  - **`system_resolver`:** use `loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)`.
  - **`validate_source_url`:** wrap the resolver in `asyncio.wait_for(timeout)`. A timeout, a `gaierror` or an empty answer → `unreachable`. Any address that fails the policy → `blocked_address`.

- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/pytest tests/unit/test_netguard.py -v`. Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add IP policy and URL pre-validation`.

---

### Task 4: Connect-time socket guard (layer 2)

**Files:**
- Modify: `app/netguard.py`
- Create: `tests/integration/__init__.py`, `tests/integration/test_connect_guard.py`

**Interfaces:**
- Produces:
  - `class BlockedAddressError(ConnectionError)`. Its `str()` starts with `BLOCKED_MARKER`.
  - `install_connect_guard(policy: IpPolicy = is_public_ip, *, resolve: Callable | None = None) -> None`. It is idempotent. `resolve` defaults to the original `socket.getaddrinfo` captured at install time.

- [ ] **Step 1: Write the failing tests.** Each test runs a script in a fresh interpreter: `subprocess.run([sys.executable, "-c", PRE + body], capture_output=True, text=True, timeout=20)`.
  - `PRE` is the common prefix. It imports `app.netguard` and calls `install_connect_guard()`, unless the test passes its own policy or resolver.
  - A `listener` fixture opens a TCP socket on `127.0.0.1:0` and counts accepted connections.

  The tests:
  - `test_blocks_loopback_ip`: `socket.create_connection(("127.0.0.1", port), timeout=2)` raises `BlockedAddressError`. The script prints `BLOCKED`. Assert the listener accepted 0 connections.
  - `test_blocks_hostname_resolving_private`: the same with `("localhost", port)` prints `BLOCKED`.
  - `test_rebinding_blocked`: install with `resolve=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]`. Connecting to `("rebind.test", 80)` prints `BLOCKED`. This proves the connect-time check doesn't trust any earlier validation.
  - `test_urllib_error_carries_marker`: `urllib.request.urlopen(f"http://127.0.0.1:{port}/")` fails, and the printed exception text contains `[saveme-blocked]`.
  - `test_connect_ex_and_unix_blocked`: `s.connect_ex(("127.0.0.1", port))` raises, and `socket.socket(socket.AF_UNIX).connect("/tmp/x")` raises `BlockedAddressError`.
  - `test_custom_policy_allows`: `install_connect_guard(policy=lambda ip: True)`. The connection succeeds and the listener counts 1.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/integration/test_connect_guard.py -v`. Expected: FAIL (`install_connect_guard` is not defined).

- [ ] **Step 3: Implement `install_connect_guard`.**
  - **`socket.socket.connect` and `connect_ex`** are replaced on the Python-level class.
    - For `AF_INET`/`AF_INET6`, if `address[0]` parses as an IP, it is checked against the policy.
    - Otherwise the host is resolved through `resolve`. Every answer is checked, then the socket connects to the first answer whose family matches.
    - Any other family raises.
  - **`socket.getaddrinfo`** is wrapped to raise when any INET answer fails the policy.
  - **`socket.create_connection`** is wrapped too.
  - Errors read `f"{BLOCKED_MARKER} {ip} is not a public address"`.

- [ ] **Step 4: Run the tests and confirm they pass.** Run the same command. Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add connect-time socket guard`.

---

### Task 5: Download filenames and Content-Disposition

**Files:**
- Create: `app/filenames.py`, `tests/unit/test_filenames.py`

**Interfaces:**
- Produces:
  - `sanitize_filename(title: str | None, ext: str) -> str`. `ext` must be one of {mp4, mp3, mkv, webm}; anything else is a `ValueError`.
  - `content_disposition(filename: str) -> str`.
  - `media_type_for(ext: str) -> str`, mapping `mp4→video/mp4`, `mp3→audio/mpeg`, `mkv→video/x-matroska`, `webm→video/webm`.

- [ ] **Step 1: Write the failing tests.**

```python
import pytest
from app.filenames import sanitize_filename, content_disposition, media_type_for

@pytest.mark.parametrize("title,ext,expected", [
    ("My Video", "mp4", "My Video.mp4"),
    ("../../etc/passwd", "mp4", "etc passwd.mp4"),
    ("a\nb\tc\x00d", "mp3", "a b c d.mp3"),
    ('x<>:"|?*\\/y', "mp4", "x y.mp4"),
    ("CON", "mp4", "CON_.mp4"),
    (None, "mp3", "download.mp3"), ("", "mp4", "download.mp4"), ("...", "mp4", "download.mp4"),
])
def test_sanitize(title, ext, expected): assert sanitize_filename(title, ext) == expected

def test_hostile_shell_title_has_no_separator():
    name = sanitize_filename("../../x; $(rm -rf /)", "mp3")
    assert "/" not in name and "\\" not in name and name.endswith(".mp3") and not name.startswith(".")

def test_long_title_truncated_on_codepoint_boundary():
    name = sanitize_filename("é" * 500, "mp4")
    stem = name.removesuffix(".mp4")
    assert len(stem.encode("utf-8")) <= 150 and stem == "é" * len(stem)

def test_bad_ext_rejected():
    with pytest.raises(ValueError): sanitize_filename("x", "sh")

def test_unicode_title_disposition():
    name = sanitize_filename("日本語 🎵 title", "mp4")
    assert name == "日本語 🎵 title.mp4"
    cd = content_disposition(name)
    assert cd.startswith("attachment; ") and 'filename="title.mp4"' in cd
    assert "filename*=UTF-8''%E6%97%A5%E6%9C%AC%E8%AA%9E%20%F0%9F%8E%B5%20title.mp4" in cd

def test_all_non_ascii_falls_back():
    assert 'filename="download.mp4"' in content_disposition("日本語.mp4")

def test_media_types(): assert media_type_for("mp3") == "audio/mpeg" and media_type_for("mkv") == "video/x-matroska"
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/unit/test_filenames.py -v`. Expected: an ImportError.

- [ ] **Step 3: Implement.**
  1. Apply NFKC normalization.
  2. Replace control characters and `/\:*?"<>|` with a space.
  3. Collapse whitespace.
  4. Strip leading and trailing `.` and space.
  5. Append `_` to Windows reserved stems (CON, PRN, AUX, NUL, COM1–9, LPT1–9, case-insensitive).
  6. Truncate to ≤150 UTF-8 bytes without splitting a codepoint.
  7. Use `download` if the stem is empty.
  8. Add `.{ext}`.

  For the ASCII fallback, apply NFKD, encode to ASCII with `ignore`, drop `"` and `\`, then run the same collapse and strip. If the stem is empty, use `download`. `filename*` uses `urllib.parse.quote(name, safe="")`.

- [ ] **Step 4: Run the tests and confirm they pass.** Run the same command. Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add filename sanitization and content disposition`.

---

### Task 6: yt-dlp options, format safety, inspect mapping, selection

**Files:**
- Create: `app/ytdl.py`, `tests/unit/test_ytdl.py`

**Interfaces:**
- Consumes: `AppError` (Task 2) and `Quality`/`Output` (Task 1).
- Produces:
  - `FFMPEG_WRAPPER_DIR: Path` = `Path(__file__).parent / "bin"`.
  - `NATIVE_PROTOCOLS = frozenset({"http", "https", "m3u8", "m3u8_native", "http_dash_segments", "http_dash_segments_generator"})`.
  - `TIERS = (1080, 720, 480)` and `TIER_TOLERANCE = 16`.
  - `base_opts(workdir: Path | None) -> dict`.
  - `video_opts(quality: Quality) -> dict` and `audio_opts() -> dict`. Both are merged on top of `base_opts`.
  - `short_side(fmt: dict) -> int | None`.
  - `unwrap_first_entry(info: dict) -> tuple[dict, str | None]`. Raises `playlist_unsupported` when a playlist has no entries.
  - `keep_safe_formats(info: dict) -> dict`. Returns a copy with only native-protocol, non-DRM formats. Raises `live_unsupported`, `drm` (all formats DRM) or `unsupported` (no formats left).
  - `inspect_payload(info: dict) -> dict`, in the spec §5.2 shape:
    `{title, duration_seconds, thumbnail_url, source, webpage_url, video: {available, qualities: [{id, label}], default}, audio: {available}, notices: [str]}`.
  - `chosen_formats(processed_info: dict) -> list[dict]` returns `processed_info.get("requested_formats") or [processed_info]`.
  - `downgrade_notice(chosen: list[dict], quality: Quality) -> str | None`.

- [ ] **Step 1: Write the failing tests.**
  - Build synthetic infos with a helper `f(id, w, h, vcodec, acodec, ext, proto="https", tbr=1000)` that sets `url=f"https://cdn.example/{id}"`.
  - Fixtures:
    - `YT`: 313 (3840×2160 vp9), 248 (1920×1080 vp9), 137 (1920×1080 avc1), 247 (1280×720 vp9), 136 (1280×720 avc1), 135 (854×480 avc1), 140 (audio mp4a), 251 (audio opus).
    - `VERTICAL`: 1080×1920 in avc1 and vp9, 720×1280 avc1, plus one audio format.
    - `LOW`: a single 640×360 avc1+mp4a format.
    - `DIRECT`: a single format with no codec or size information.
  - Helpers: `info(formats)` returns `{"id": "x", "title": "t", "extractor_key": "Youtube", "webpage_url": "https://www.youtube.com/watch?v=x", "formats": formats}`, and `select(formats, q)` returns the processed info.
  - Process selections offline with `yt_dlp.YoutubeDL({**base_opts(None), **video_opts(q), "quiet": True}).process_ie_result(copy.deepcopy(info), download=False)`. This works because the formats carry URLs and `download=False` needs no network access.

```python
def test_base_opts_lock_down():
    o = base_opts(Path("/w"))
    assert o["noplaylist"] is True and o["playlist_items"] == "1" and o["cachedir"] is False
    assert o["proxy"] == "" and o["external_downloader"] == {"default": "native"} and o["hls_prefer_native"] is True
    assert o["outtmpl"] == {"default": "media.%(ext)s"} and o["paths"] == {"home": "/w", "temp": "/w"}
    assert o["ffmpeg_location"] == str(FFMPEG_WRAPPER_DIR) and o["remote_components"] == set()
    assert "cookiefile" not in o and o.get("usenetrc") is False

def test_tiers_listed_only_when_present():
    p = inspect_payload(info(YT))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best", "1080", "720", "480"]
    assert p["video"]["qualities"][0]["label"] == "Best (2160p)" and p["video"]["default"] == "1080"

def test_vertical_uses_short_side():
    p = inspect_payload(info(VERTICAL))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best", "1080", "720"]
    assert p["video"]["qualities"][0]["label"] == "Best (1080p)"

def test_low_res_offers_best_only_with_notice():
    p = inspect_payload(info(LOW))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best"] and p["video"]["default"] == "best"
    assert any("360p" in n for n in p["notices"])

def test_direct_link_unknown_codecs():
    p = inspect_payload(info(DIRECT))
    assert p["video"]["available"] and p["audio"]["available"] and [q["id"] for q in p["video"]["qualities"]] == ["best"]
    assert p["notices"]

def test_audio_only_and_video_only_sources():
    assert inspect_payload(info([f("a", None, None, "none", "mp4a", "m4a")]))["video"]["available"] is False
    assert inspect_payload(info([f("v", 640, 360, "avc1", "none", "mp4")]))["audio"]["available"] is False

def test_keep_safe_formats():
    out = keep_safe_formats(info(YT + [f("r", 1920, 1080, "avc1", "mp4a", "flv", proto="rtmp")]))
    assert all(x["protocol"] in NATIVE_PROTOCOLS for x in out["formats"])
    for bad, code in [({"is_live": True}, "live_unsupported"), ({"live_status": "is_upcoming"}, "live_unsupported")]:
        with pytest.raises(AppError) as e: keep_safe_formats({**info(YT), **bad})
        assert e.value.code == code
    with pytest.raises(AppError) as e: keep_safe_formats(info([{**f("d", 640, 360, "avc1", "mp4a", "mp4"), "has_drm": True}]))
    assert e.value.code == "drm"

def test_unwrap_playlist():
    e1 = info(LOW)
    assert unwrap_first_entry({"_type": "playlist", "entries": [e1], "playlist_count": 3}) == (e1, "This post has 3 videos; the first will be downloaded.")
    with pytest.raises(AppError) as e: unwrap_first_entry({"_type": "playlist", "entries": []})
    assert e.value.code == "playlist_unsupported"

def test_select_720_prefers_h264_and_aac():
    ids = [x["format_id"] for x in chosen_formats(select(YT, "720"))]
    assert ids == ["136", "140"]

def test_select_1080_vertical_is_h264(): assert chosen_formats(select(VERTICAL, "1080"))[0]["vcodec"].startswith("avc1")
def test_select_best_any_codec(): assert chosen_formats(select(YT, "best"))[0]["format_id"] == "313"

def test_downgrade_notice():
    assert downgrade_notice([{"width": 1280, "height": 720, "vcodec": "avc1"}], "1080") == "1080p wasn't available; downloaded 720p."
    assert downgrade_notice([{"width": 1920, "height": 1080, "vcodec": "avc1"}], "1080") is None
    assert downgrade_notice([{"width": 640, "height": 360, "vcodec": "avc1"}], "best") is None
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/unit/test_ytdl.py -v`. Expected: an ImportError.

- [ ] **Step 3: Implement.**
  - **`base_opts` keys:**
    - Exactly the keys asserted above.
    - `quiet=True`, `noprogress=True`, `no_warnings=False`.
    - `socket_timeout=20`, `retries=3`, `fragment_retries=3`, `overwrites=True`.
    - `js_runtimes={"deno": {}}`.
    - `remote_components=set()`. The parameter name was confirmed in YoutubeDL.py:743; Deno runs without `--allow-net`.
  - **`video_opts`:**
    - `format="bv*+ba/b"` and `merge_output_format="mp4/mkv"`.
    - For `best`, no `format_sort`.
    - For a tier N, `format_sort=[f"res:{N}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]`.
  - **`audio_opts`:** `format="ba/b"` and `postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "2"}]`.
  - **Video and audio availability:** a format with an unknown codec (key missing or `None`) counts as "maybe". Availability is `vcodec != "none"` (or `acodec`), or unknown.
  - **Tiers:** a tier is listed if any format with video has `abs(short_side - N) <= TIER_TOLERANCE`.
  - **Labels and notices:**
    - `Best ({max_short_side}p)`. If the size is unknown, the label is just `Best`.
    - `default` is the highest listed tier, otherwise `best`.
    - If no tier exists and the size is known, add the notice `Only {N}p is available.`
    - For unknown codecs, add the notice `Details for this link are unknown until download.`
  - **`source`:** `f"{extractor_key} · {domain}"`. The `Generic` extractor shows as `Web page`.
  - **Downgrade:** a notice is returned only when `quality != "best"` and the chosen short side differs from N by more than `TIER_TOLERANCE`.

- [ ] **Step 4: Run the tests and confirm they pass.** Run the same command. Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add yt-dlp option building, format safety and inspect mapping`.

---

### Task 7: ffmpeg/ffprobe protocol-whitelist wrappers

**Files:**
- Create: `app/bin/_ffwrap.py`, `app/bin/ffmpeg`, `app/bin/ffprobe` (both `chmod 755`, shebang `#!/usr/bin/env python3`, stdlib only), `tests/unit/test_ffwrap.py`, `tests/integration/test_ffwrap_canary.py`

**Interfaces:**
- Produces:
  - `inject_whitelist(argv: list[str], tool: Literal["ffmpeg", "ffprobe"]) -> list[str]`.
  - `find_real(name: str, wrapper_dir: Path, path_env: str) -> str`. It searches PATH and skips `wrapper_dir`. The `SAVEME_FFMPEG_DIR` environment variable, if set, wins.
  - The scripts `os.execv` the real binary with the injected arguments.

- [ ] **Step 1: Write the failing unit tests.**

```python
from app.bin._ffwrap import inject_whitelist
W = ["-protocol_whitelist", "file,pipe"]
def test_ffmpeg_before_every_input():
    assert inject_whitelist(["-y", "-i", "a.mp4", "-i", "b.m4a", "out.mp4"], "ffmpeg") == ["-y", *W, "-i", "a.mp4", *W, "-i", "b.m4a", "out.mp4"]
def test_ffmpeg_version_untouched(): assert inject_whitelist(["-version"], "ffmpeg") == ["-version"]
def test_ffprobe_prefixed(): assert inject_whitelist(["-show_streams", "x.mp4"], "ffprobe") == [*W, "-show_streams", "x.mp4"]
def test_existing_whitelist_overridden():
    assert inject_whitelist(["-protocol_whitelist", "http,file", "-i", "x"], "ffmpeg") == [*W, "-i", "x"]
```

- [ ] **Step 2: Write the failing canary test.**
  - Start a local HTTP server that counts hits on `/canary.ts`.
  - Write `evil.m3u8` to `tmp_path`:
    `#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttp://127.0.0.1:{port}/canary.ts\n#EXT-X-ENDLIST\n`
  - `test_control_real_ffprobe_fetches`: the **real** ffprobe (`shutil.which("ffprobe")`) run on `evil.m3u8` produces hits > 0. This proves the test is meaningful. If it doesn't, fix the payload before going on.
  - `test_wrapped_ffprobe_and_ffmpeg_do_not_fetch`: reset the counter, then run `app/bin/ffprobe -show_streams evil.m3u8` and `app/bin/ffmpeg -i evil.m3u8 -f null -`. Hits must be 0.
  - `test_wrapper_passthrough`: `app/bin/ffmpeg -version` returns 0. `app/bin/ffprobe` succeeds on a valid 1 s MP4 that the test generates.

- [ ] **Step 3: Run both test files and confirm they fail.** Run `.venv/bin/pytest tests/unit/test_ffwrap.py tests/integration/test_ffwrap_canary.py -v`. Expected: FAIL.

- [ ] **Step 4: Implement the wrappers.** Scripts run directly, so `sys.path[0]` is `app/bin` and `import _ffwrap` works. The real binary comes from `find_real`.

- [ ] **Step 5: Run the tests and confirm they pass.** Expected: all pass, and the control test shows hits > 0.

- [ ] **Step 6: Commit.** Message: `feat: add ffmpeg/ffprobe wrappers that restrict inputs to local files`.

---

### Task 8: Test support infrastructure and worker inspect mode

**Files:**
- Create:
  - `tests/conftest.py`
  - `tests/support/__init__.py`, `tests/support/media.py`, `tests/support/server.py`, `tests/support/policy.py`, `tests/support/worker_entry.py`, `tests/support/procs.py`
  - `app/worker.py`
  - `tests/integration/test_worker_inspect.py`

**Interfaces:**
- Consumes: Tasks 2–7.
- Produces:
  - **Worker protocol:**
    - The parent writes one JSON object to stdin and closes it. The worker writes **only** JSON lines to stdout: the protocol fd is a `dup` of fd 1, and fd 1 is then `dup2`'d to fd 2 so stray prints go to stderr. Exit code 0 means a result; 1 means an error.
    - Requests:
      - `{"mode": "inspect", "url": str}`
      - `{"mode": "download", "url", "output", "quality", "workdir": str, "max_bytes": int}`
    - Events:
      - `{"event": "progress", "detail": str, "downloaded_bytes": int, "total_bytes": int|None, "percent": float|None}`
      - `{"event": "phase", "phase": "converting", "detail": str}`
      - `{"event": "result", "data": dict}`
      - `{"event": "error", "code": str, "message": str}`
  - **Worker functions:**
    - `app.worker.serve(stdin: TextIO, out: TextIO) -> int`
    - `app.worker.main() -> None`: `install_connect_guard()`, then fd setup, then `sys.exit(serve(...))`
    - `resolve(ydl: YoutubeDL, url: str) -> tuple[dict, str | None]`: the **single extraction boundary** (spec §10). It returns the safe info and an optional playlist notice. Both modes call it, and Build 2's browser resolver will hook in here.
    - `run_inspect(url: str) -> dict`
    - `safe_thumbnail(url: str | None) -> str | None`. Uses the guarded `getaddrinfo`. Returns `None` for non-http(s) or blocked hosts.
  - **Test support:**
    - `tests/support/policy.py`: `allow_loopback(ip) -> bool`, true for `127.0.0.1` or `is_public_ip(ip)`.
    - `tests/support/worker_entry.py`: installs the guard with `allow_loopback`, then `serve(...)`. It also applies `SAVEME_TEST_FFMPEG_DIR` if needed.
    - `tests/support/procs.py`: `run_worker_proc(request: dict, *, entry: str = "tests.support.worker_entry", timeout: float = 60) -> tuple[list[dict], int]`.
    - `tests/support/media.py`: `make_media(dir: Path) -> dict[str, Path]`, generated once per session with ffmpeg. It produces:
      - `sample.mp4`: 2 s, 640×360, H.264 + AAC
      - `video_only.mp4`
      - `sample.mp3`
      - `big.mp4`: 3 MiB of random bytes
      - `hls/master.m3u8`: separate video and audio renditions, 4 s
    - `tests/support/server.py`: `MediaServer` (a threaded `http.server`) with `.url(path) -> str`, `.hits: Counter[str]` and `.close()`. Routes:
      - `/media/<name>`: correct Content-Type and Content-Length
      - `/chunked/<name>`: no Content-Length
      - `/slow/<name>`: trickles 1 KiB every 0.5 s
      - `/redirect?to=<url>`: 302 to the given URL
      - `/page?title=<t>&video=<path>&thumb=<url>`: HTML with a `<title>` plus `og:video`, `og:title` and `og:image`
      - `/plain`: HTML with no media
      - `/missing`: 404
      - `/evil.mp4`: an HLS playlist pointing at `/canary.ts`, served as `video/mp4`
      - `/canary.ts`
      - `/hls/...`
    - Fixtures in `tests/conftest.py`: `media` (session) and `media_server` (session).

  The HLS generation command, run in `hls/`:

  ```bash
  ffmpeg -f lavfi -i testsrc=size=640x360:rate=25 -f lavfi -i sine=frequency=440 -t 4 \
    -map 0:v -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -f hls -hls_time 2 -hls_playlist_type vod \
    -var_stream_map "v:0,agroup:aud a:0,agroup:aud,default:yes" -master_pl_name master.m3u8 \
    -hls_segment_filename "%v_%03d.ts" "%v.m3u8"
  ```

- [ ] **Step 1: Write the support modules and the failing tests in `tests/integration/test_worker_inspect.py`.**
  - `test_inspect_direct_mp4`: the last event is `result`, `video.available` and `audio.available` are true, and the qualities start with `best`.
  - `test_inspect_hls_master`: the qualities are `["best"]` and the label is `Best (360p)`.
  - `test_redirect_to_metadata_ip_blocked`: inspecting `/redirect?to=http://169.254.169.254/latest/meta-data/` gives an error event with code `blocked_address`.
  - `test_production_entry_blocks_loopback`: `entry="app.worker"` on `/media/sample.mp4` gives code `blocked_address`, and `media_server.hits["/media/sample.mp4"]` is unchanged.
  - `test_unsupported_and_missing`: `/plain` gives `unsupported`, and `/missing` gives `unavailable`.
  - `test_private_thumbnail_dropped`: `/page?...&thumb=http://10.0.0.1/x.jpg` gives `thumbnail_url is None`.
  - `test_stdout_is_protocol_only`: every stdout line from every run above parses as JSON with an `event` key.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/integration/test_worker_inspect.py -v`. Expected: FAIL (no `app.worker`).

- [ ] **Step 3: Implement inspect.**
  - `resolve`:
    1. `ydl.extract_info(url, download=False, process=False)`
    2. `unwrap_first_entry`; if the entry is a `url`/`url_transparent` result, extract it the same way
    3. `keep_safe_formats`
  - `run_inspect`: `resolve` with `YoutubeDL(base_opts(None))`, then `inspect_payload`, then `safe_thumbnail`, then add the playlist notice.
  - Exceptions:
    - yt-dlp `DownloadError` and `ExtractorError` → `classify_ytdlp_error(str(e))`
    - `AppError` → its code
    - anything else → `internal`, with the traceback logged to stderr only
  - A yt-dlp `logger` object routes all yt-dlp messages to stderr.

- [ ] **Step 4: Run the tests and confirm they pass.** Expected: all pass. If generic `og:video` extraction behaves differently from what's assumed here, adjust the `/page` fixture HTML, not the assertions.

- [ ] **Step 5: Commit.** Message: `feat: add worker subprocess with guarded inspect mode`.

---

### Task 9: Worker download mode (video, MP3, progress, limits)

**Files:**
- Modify: `app/worker.py`
- Create: `tests/integration/test_worker_download.py`

**Interfaces:**
- Produces: `run_download(req: dict, emit: Callable[[dict], None]) -> dict`. It returns `{"file": "media.<ext>", "ext": str, "title": str | None, "notice": str | None}`, where `file` is relative to the workdir.

- [ ] **Step 1: Write the failing tests.** Each test uses a fresh `tmp_path / "job"` as the workdir and `max_bytes = 50 MiB` unless noted.
  - `test_video_direct_mp4`: the result file is `media.mp4`. ffprobe reports one h264 stream and one aac stream. Once the job finishes, the workdir contains only `media.mp4`.
  - `test_audio_mp3`: the result file is `media.mp3`. ffprobe reports codec `mp3` and a duration of 2.0 ± 0.3 s. A `phase` event with `converting` was emitted.
  - `test_hls_merge_to_mp4`: details include `(1 of 2)` and `(2 of 2)`. The final `media.mp4` has both video and audio.
  - `test_quality_downgrade_notice`: HLS with quality `720` gives the notice `720p wasn't available; downloaded 360p.`
  - `test_audio_from_video_only_fails`: code `no_audio`.
  - `test_size_limit_aborts`: parametrized over `/media/big.mp4` (Content-Length) and `/chunked/big.mp4` (chunked). With `max_bytes=1 MiB`, both give code `too_large`, with no `media.*` file left behind.
  - `test_hostile_title_stays_in_workdir`: `/page?title=../../x;%20$(rm%20-rf%20/)&video=/media/sample.mp4`. The result file is `media.mp4` and the title equals the raw title. The workdir's parent listing is unchanged apart from `job/`.
  - `test_percent_only_with_exact_total`: on `/chunked/sample.mp4`, every progress `percent` is None. On `/media/sample.mp4`, the last percent is ≥ 99.
  - `test_side_door_canary`: an audio job on `/evil.mp4` leaves `media_server.hits["/canary.ts"] == 0`. The job's own outcome doesn't matter.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/integration/test_worker_download.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `run_download`.**
  - **Steps:**
    1. Run `resolve` (Task 8).
    2. `ydl.process_ie_result(info, download=True)` with `base_opts(workdir)` + `video_opts(q)` or `audio_opts()` + `max_filesize=max_bytes` + `progress_hooks` + `postprocessor_hooks`.
  - **Progress hook:**
    - Number the parts from `info["requested_formats"]` (1 part when absent).
    - The detail reads `Downloading video track (i of n)`, `Downloading audio track (i of n)`, or `Downloading` for a single part.
    - `percent` comes from the exact `total_bytes`, or from `fragment_index`/`fragment_count`. Never from `total_bytes_estimate`.
    - Throttle emits to at most 4 per second.
    - Raise a private `_TooLarge` when the running total across parts exceeds `max_bytes`. It is reported as `too_large` with the message `too_large_message(max_bytes)`.
  - **Postprocessor hook:** on `status == "started"` from an `FFmpeg*` postprocessor, emit a phase event with detail `Merging video and audio`, `Converting to MP3` or `Converting to MP4`.
  - **Video output that isn't MP4:** remux with `app/bin/ffmpeg -i in -c copy media.mp4`. On failure, keep the original and set the notice `Delivered as {EXT} (couldn't convert to MP4).`
  - **Error mapping:**
    - Audio output where the source has no audio stream (ffprobe on the downloaded file, or the PP error text) → `no_audio`.
    - Video output where the source has no video → `no_video`.
  - **Cleanup:** delete everything in the workdir except the final file.

- [ ] **Step 4: Run the tests and confirm they pass.** Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add worker download mode with progress, limits and MP3/MP4 output`.

---

### Task 10: Parent-side worker runner

**Files:**
- Create: `app/runner.py`, `tests/integration/test_runner.py`

**Interfaces:**
- Produces:
  - `DEFAULT_WORKER_CMD: tuple[str, ...] = (sys.executable, "-m", "app.worker")`.
  - `dir_size(path: Path) -> int`. Recursive; returns 0 if the path is missing.
  - `async def run_worker(request: dict, *, cmd: Sequence[str], timeout: float, on_event: Callable[[dict], None] | None = None, workdir: Path | None = None, max_workdir_bytes: int | None = None, poll_interval: float = 1.0) -> dict`. It returns the result `data`, or raises `AppError`.
  - `RunWorker` is a type alias for this callable. It is what `JobManager` and `create_app` accept.

- [ ] **Step 1: Write the failing tests.** They use fake worker commands built with `[sys.executable, "-c", script]`.
  - `test_result_and_events`: the script prints a progress line and then a result line. `on_event` received the progress, and the return value equals `data`.
  - `test_error_event_raises`: an `error` event with `unsupported` and exit 1 raises `AppError` with code `unsupported`.
  - `test_timeout_kills_group`: the script spawns `sleep 60` as a child, prints its pid, and sleeps. With `timeout=1`, `AppError("timeout")` is raised within 8 s and the child pid is no longer alive (`os.kill(pid, 0)` raises `ProcessLookupError`).
  - `test_workdir_growth_kills`: the script writes 2 MiB into `workdir` and sleeps. With `max_workdir_bytes=1 MiB` and `poll_interval=0.1`, the result is `too_large`.
  - `test_garbage_stdout_is_internal`: printing `hello` and exiting 0 gives `internal`.
  - `test_proxy_env_stripped`: monkeypatch `HTTPS_PROXY` and `http_proxy`. The script reports `os.environ` keys in its result data, and neither proxy key is present.
  - `test_cancellation_kills_worker`: run as a task, then cancel it. The child is gone within 8 s.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/integration/test_runner.py -v`. Expected: an ImportError.

- [ ] **Step 3: Implement.**
  - Start the process with `asyncio.create_subprocess_exec(*cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, start_new_session=True, env=clean_env, cwd=workdir or tempfile.gettempdir())`.
  - Write the request as JSON to stdin and close it.
  - Read stdout lines, up to 1 MiB each, and pass `progress` and `phase` events to `on_event`.
  - Drain stderr concurrently, keeping the last 64 KiB. Log it when there's a failure.
  - A watchdog task enforces the timeout and polls `dir_size`.
  - Killing a worker: `os.killpg(pgid, SIGTERM)`, wait up to 5 s, then `SIGKILL`. The same kill runs in `finally`, so it also happens on cancellation.
  - The size-breach message comes from `too_large_message(max_workdir_bytes // 2)`. The workdir allowance is 2× the file limit, so this names the per-file limit.

- [ ] **Step 4: Run the tests and confirm they pass.** Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add worker runner with timeout, size and cancellation kill`.

---

### Task 11: JobManager (lifecycle, queue, disk, TTL, cleanup)

**Files:**
- Create: `app/jobs.py`, `tests/integration/test_jobs.py`

**Interfaces:**
- Consumes: `Settings`, `ConfigError`, `AppError`, `sanitize_filename`, `media_type_for`, `run_worker`, `dir_size`, `DEFAULT_WORKER_CMD`.
- Produces:
  - `JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")`, `MARKER_NAME = ".saveme-tempdir"` and `APP_DIR = Path(__file__).resolve().parent.parent` (the repo or image root).
  - `prepare_temp_dir(path: Path, *, app_dir: Path) -> None`. Raises `ConfigError("TEMP_DIR", ...)` for `/`, for `app_dir` or anything inside it, or for an existing non-empty directory without the marker. It creates the directory and the marker otherwise.
  - The `Job` dataclass:
    - `id, url, output, quality, status, detail, progress_percent, downloaded_bytes, total_bytes`
    - `notice, error: AppError | None, reason: str | None, title, filename, media_type`
    - `file_path: Path | None`, `workdir: Path`
    - `created_at, started_at, ready_at, finished_at`
  - `class JobManager`:
    - `JobManager(settings, *, run_worker: RunWorker = run_worker, worker_cmd=DEFAULT_WORKER_CMD, clock=time.monotonic, disk_usage=dir_size, sweep_interval=60.0, app_dir=APP_DIR)`
    - `async start()` prepares the temp dir, wipes job-ID directories, and starts the sweeper.
    - `async shutdown()`.
    - `submit(url, output, quality) -> Job` is synchronous and schedules an asyncio task.
    - `get(job_id) -> Job` raises `job_not_found`, including for IDs that don't match `JOB_ID_RE`.
    - `public_view(job) -> dict` has keys `job_id, status, detail, progress_percent, downloaded_bytes, total_bytes, notice, error, filename, reason`. It never includes paths.
    - `file_for_delivery(job_id) -> Job` raises `not_ready` / `expired` / `job_not_found`.
    - `mark_delivered(job_id) -> None` deletes the workdir and sets status `expired` with reason `delivered`.
    - `async sweep() -> None`.
- Statuses and reasons:
  - Status ∈ {queued, downloading, converting, ready, failed, expired}.
  - Expired reasons are `delivered`, `ttl` or `evicted`.
  - Records in a terminal state are purged 3600 s after `finished_at`.

- [ ] **Step 1: Write the failing tests.**
  - `StubRunner` is an async callable with `run_worker`'s signature. Each call waits on an `asyncio.Event` that the test controls. It can emit events, write `workdir/media.mp4`, and return `{"file": "media.mp4", "ext": "mp4", "title": "My Video", "notice": None}`, or raise.
  - Tests use `tmp_path / "media-jobs"` as `temp_dir`.

  The tests:
  - `test_lifecycle_to_ready`: the job is `queued` after submit. A progress event moves it to `downloading` and a phase event to `converting`. On return it is `ready` with filename `My Video.mp4` and media type `video/mp4`. `json.dumps(public_view)` doesn't contain `str(temp_dir)`.
  - `test_failure_cleans_workdir`: a stub raising `AppError("unsupported")` leaves the job `failed` with `error.code == "unsupported"`, and the workdir no longer exists.
  - `test_concurrency_and_queue_limit`: with defaults (2 active, 4 queued), six submits produce 2 jobs in `downloading` and 4 `queued`. The seventh raises `AppError` busy (429). Releasing one job moves a queued job to active, and active stays ≤ 2 at every observation.
  - `test_ready_cap_evicts_oldest`: with `max_ready_files=2`, three ready jobs leave the oldest `expired` with reason `evicted`.
  - `test_ttl_expiry`: with a fake clock, advancing it past `job_ttl_seconds` and calling `sweep()` gives `expired` with reason `ttl`, and the file is deleted.
  - `test_delivery_states`: `file_for_delivery` on a queued job gives 409 `not_ready`. After `mark_delivered`, it gives 410 `expired`. An unknown ID gives 404. `get("../../etc")` gives 404 and nothing on disk is touched.
  - `test_disk_pressure_evicts_then_refuses`: with a `disk_usage` stub returning a huge number, submit evicts the ready jobs first. If usage is still over, it raises 503 `disk_full` with the message `Server storage is full, try again later.`
  - `test_queued_job_waits_for_disk`: when disk is over budget at start time, the job stays `queued`. Once the stub reports usage below budget, it starts.
  - `test_startup_wipe_and_prepare`: a temp dir holding the marker, two job-ID-shaped directories and a `keep.txt` file loses the directories after `start()`, and `keep.txt` remains. `prepare_temp_dir` raises for `/` and for a non-empty unmarked directory.
  - `test_shutdown_kills_and_cleans`: shutting down with blocking stubs cancels the tasks, and the workdirs are gone.
  - `test_record_purge`: a terminal job older than 3600 s, after `sweep()`, makes `get` raise `job_not_found`.
  - `test_sweep_removes_orphans`: a job-ID-shaped directory created in `temp_dir` after `start()` with no registry entry is gone after `sweep()`. A ready job's directory survives.
  - `test_oversize_job_fails_cleanly`: a real runner with the `tests.support.worker_entry` cmd, `max_file_size_mb=1` and `/chunked/big.mp4`. The job ends `failed` with code `too_large`, the message contains `1 MB`, and the workdir is gone.
  - `test_timeout_job_fails_cleanly`: a real runner with `max_job_seconds=3` and `/slow/sample.mp4`. The job ends `failed` with code `timeout`, and the workdir is gone.
  - `test_real_video_job_ready`: a real runner on `/media/sample.mp4` with output video ends `ready`, and `file_path` exists.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/integration/test_jobs.py -v`. Expected: an ImportError.

- [ ] **Step 3: Implement.**
  - **Admission at submit:** raise busy when `active + queued >= max_active + max_queued`.
  - **Disk check, at submit and again at start:**
    - Reservation per active job = `2 × max_file_bytes`.
    - It fits when `disk_usage(temp_dir) + reservations_of_other_active + 2 × max_file_bytes <= max_total_disk_bytes`.
    - When it doesn't fit, evict ready jobs oldest-first, then re-check.
    - If it still doesn't fit: at submit, raise `disk_full`; at start, release the slot, sleep 2 s, and retry until the job's `max_job_seconds` deadline, after which it fails with `disk_full`.
  - **Job task:**
    1. Acquire the `asyncio.Semaphore(max_active_jobs)`.
    2. Run the disk check.
    3. Create the workdir.
    4. `run_worker(..., timeout=max_job_seconds, workdir=wd, max_workdir_bytes=2 × max_file_bytes)`.
    5. Map events onto the job.
    6. On success: set ready, `ready_at`, `filename = sanitize_filename(title, ext)` and `notice`, then enforce the ready-file cap.
    7. On `AppError`: set `failed`. Any other exception becomes `AppError("internal")`.
    8. Always remove the workdir unless the job is ready.
  - **Sweeper:** runs every `sweep_interval`. It handles TTL expiry, removes orphan job-ID directories that aren't non-terminal jobs in the registry, and purges old records.

- [ ] **Step 4: Run the tests and confirm they pass.** Expected: all pass.

- [ ] **Step 5: Commit.** Message: `feat: add in-memory job manager with queue, disk budget, TTL and cleanup`.

---

### Task 12: HTTP API, delivery, middleware, degraded mode

**Files:**
- Create: `app/main.py`, `app/__main__.py`, `tests/integration/test_api.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `STATIC_DIR = APP_DIR / "static"`.
  - `create_app(settings: Settings | ConfigError, *, worker_cmd=DEFAULT_WORKER_CMD, ip_policy=is_public_ip, resolver=system_resolver, run_worker=run_worker, static_dir=STATIC_DIR, sweep_interval=60.0) -> FastAPI`.
  - The module-level `app = create_app(_settings_or_error())`, where `_settings_or_error()` catches `ConfigError`.
  - `app/__main__.py` runs `uvicorn.run("app.main:app", host="0.0.0.0", port=8000, log_level=<LOG_LEVEL or "info">, timeout_graceful_shutdown=10)`.
- Routes (spec §6.2):
  - `GET /`: `index.html`.
  - `/static`: `StaticFiles`.
  - `GET /health`: 200 `{"status": "ok"}`. In degraded mode, 503 `{"status": "error", "error": {...config_error}}`.
  - `POST /api/inspect`, `POST /api/jobs` (202), `GET /api/jobs/{id}`, `GET /api/jobs/{id}/file`.
- Middleware:
  - `TrustedHostMiddleware(allowed_hosts=settings.allowed_hosts)`.
  - Origin check: a POST whose `Origin` header has a host not in `allowed_hosts` → 403 `forbidden_origin`.
  - Security headers on every response:
    - `Content-Security-Policy: default-src 'self'; img-src 'self' https: http: data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`
    - `X-Content-Type-Options: nosniff`
    - `Referrer-Policy: no-referrer`
- Handlers:
  - `AppError` → its status plus the error body.
  - `RequestValidationError` → 400 `invalid_request`, with a message naming the field, e.g. `output must be one of: video, audio`.
  - A catch-all `Exception` → 500 `internal`, logging the traceback.
- Inspect:
  1. `validate_source_url(url, policy=ip_policy, resolver=resolver)`.
  2. Non-blocking semaphore acquire; if it's taken, 429 `busy`.
  3. `run_worker({"mode": "inspect", "url": url}, cmd=worker_cmd, timeout=inspect_timeout_seconds)`.
- Delivery:
  - A `StreamingResponse` over 1 MiB file chunks, with an explicit `Content-Length`, `media_type_for(ext)`, and `Content-Disposition: content_disposition(job.filename)`.
  - The generator calls `manager.mark_delivered(id)` **after its loop finishes**. If the client disconnects mid-stream, that line never runs.
  - The lifespan calls `manager.start()` and `manager.shutdown()`.

- [ ] **Step 1: Write the failing tests.**
  - Use `httpx.AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1")` inside an `asgi_lifespan`-style `async with` that calls the lifespan via `app.router.lifespan_context(app)`.
  - Unless a test says otherwise, use a test app with `ip_policy=allow_loopback`, a stub resolver that returns the literal host's own IP, and `worker_cmd=(sys.executable, "-m", "tests.support.worker_entry")`.

  The tests:
  - `test_health`.
  - `test_inspect_invalid_400_shape`: the body is exactly `{"error": {"code": "invalid_url", "message": <str>}}`.
  - `test_inspect_blocked_403`: the production policy and the real resolver, on `http://169.254.169.254/`, `http://127.0.0.1:8000/` and `http://[::1]/`.
  - `test_inspect_file_scheme_400`.
  - `test_extra_fields_rejected`: `{"url": ..., "format": "bestvideo"}` gives 400 `invalid_request`.
  - `test_bad_enum_message_names_field`.
  - `test_inspect_success`: on `/media/sample.mp4`, 200, with the qualities shape.
  - `test_inspect_busy_429`: `max_active_inspections=1` with a blocking stub `run_worker`.
  - `test_inspect_timeout_504`: a stub raises `AppError("timeout")`.
  - `test_job_end_to_end`:
    1. POST returns 202 `{job_id, status: "queued"}`.
    2. Poll until `ready`, with a 30 s cap.
    3. GET file returns 200 with `content-type: video/mp4`. The `content-disposition` has `filename=` and `filename*=`, and the body bytes equal the file on disk.
    4. The status is then `expired` with reason `delivered`.
    5. A second GET file gives 410.
  - `test_unknown_job_is_404_job_not_found`.
  - `test_file_not_ready_409`.
  - `test_no_temp_dir_in_any_response`: concatenate every response body from the end-to-end flow; it doesn't contain `str(temp_dir)`.
  - `test_aborted_download_keeps_file`:
    - Run the app in a real `uvicorn.Server` on a free port in a background thread.
    - Use a 3 MiB ready file (a stub runner writes it).
    - With a raw socket, send the GET, read 64 KiB, and close.
    - After 1 s the job is still `ready` and the file exists.
  - `test_bad_host_header_400` and `test_cross_origin_post_403`.
  - `test_degraded_mode`: with `create_app(ConfigError("MAX_ACTIVE_JOBS", "must be 1-16"))`, `/health` gives 503 with `error.code == "config_error"` and a message containing `MAX_ACTIVE_JOBS`, `/api/inspect` gives 503 `config_error`, and `GET /` gives 200.
  - `test_security_headers_on_index`.
  - `test_unexpected_exception_500_no_traceback`: the stub raises `RuntimeError("secret /tmp/x")`. The body code is `internal`, and neither `secret` nor `Traceback` appears in it.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/integration/test_api.py -v`. Expected: an ImportError.

- [ ] **Step 3: Implement `create_app` per the Interfaces block.**

- [ ] **Step 4: Run the tests and confirm they pass.** Then run the whole offline suite with `.venv/bin/pytest -q`. Expected: everything passes.

- [ ] **Step 5: Commit.** Message: `feat: add HTTP API with streamed delivery, host/origin checks and degraded mode`.

---

### Task 13: Page (HTML, JS, Tailwind)

**Files:**
- Create: `static/index.html`, `static/app.js`, `static/src/input.css`, `scripts/build-css.sh` (`chmod 755`)
- Modify: `tests/integration/test_api.py` (add page tests)

**Interfaces:**
- Consumes: the API from Task 12.
- `index.html` contains:
  - `<link rel="stylesheet" href="/static/dist/styles.css">` and `<script src="/static/app.js" defer>`.
  - No inline scripts or styles, because the CSP forbids them.
- `scripts/build-css.sh [out]`:
  - Downloads the Tailwind v4 standalone CLI (version pinned in the script as `TAILWIND_VERSION`) for the host OS and arch (`linux`/`macos` × `x64`/`arm64`) into `.cache/`.
  - Verifies it against the release's `sha256sums.txt`.
  - Builds `static/src/input.css` to `${1:-static/dist/styles.css}` with `--minify`.
- `input.css` is `@import "tailwindcss"; @source "../index.html"; @source "../app.js";`.

- [ ] **Step 1: Write the failing page tests.**
  - `test_index_page_contract`: `GET /` returns HTML containing:
    - `<label for="url"`
    - `id="url"`
    - `aria-live="polite"`
    - the exact footer copy from Global Constraints
    - `/static/app.js`
    - no `<script>` tags with inline content
  - `test_app_js_served`: `GET /static/app.js` gives 200 with a JavaScript content type.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/integration/test_api.py -k "page or app_js" -v`. Expected: FAIL.

- [ ] **Step 3: Build the page (spec §7).**
  - **States** in `app.js`: `idle → inspecting → inspected → job(queued|downloading|converting) → ready → saved | failed | lost`.
  - **Polling:** every 1 s while the job is active. It stops on a terminal state.
  - **Download buttons:** disabled while inspecting or while a job is active.
  - **sessionStorage:** the active `job_id` is stored there. On load, if one exists, polling resumes.
  - **Job lost:** a 404 `job_not_found` while polling shows `This download was lost (the server may have restarted). Please start again.`, clears storage, and resets.
  - **Progress bar:** determinate only when `progress_percent` is a number. Otherwise it is indeterminate, with `X.X MB downloaded`.
  - **Missing data:** a null duration or thumbnail hides that element.
  - **Thumbnail:** `<img referrerpolicy="no-referrer">`, only for `http(s)` URLs.
  - **Save File:** an `<a download href="/api/jobs/{id}/file">`. After the click, poll once more and show `Saved — file removed from server.` once the status is `expired` with reason `delivered`.
  - **Degraded mode:** on load, `GET /health`. A 503 shows a banner with `error.message`.
  - **Keyboard:** Enter in the URL field triggers Inspect.
  - **Accessibility and layout:** visible focus rings, `motion-reduce:` variants on spinners, and a layout that works at 360 px.

- [ ] **Step 4: Run the tests and confirm they pass.** Then build the CSS locally with `scripts/build-css.sh`. Expected: `static/dist/styles.css` exists and is under 60 KB.

- [ ] **Step 5: Do a quick manual check locally.**
  - Start the server with `.venv/bin/python -m app`, with `TEMP_DIR=$PWD/.cache/jobs` exported.
  - Open `http://127.0.0.1:8000` with the browser tool and confirm:
    - **M1:** the page renders, the label and footer are visible, and the layout looks right at 360 px width.
    - **M2:** inspecting `file:///etc/passwd` shows the readable invalid-link message.

  Checks M3–M6 need real media and run in Task 14.

- [ ] **Step 6: Commit.** Message: `feat: add single-page UI with polling, resume and accessible states`.

---

### Task 14: Docker image, compose, smoke test, browser walkthrough

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `.dockerignore`, `.env.example`, `scripts/docker-smoke.sh` (`chmod 755`), `tests/unit/test_packaging.py`

**Interfaces:**
- `Dockerfile` stages:
  1. `css`:
     - `FROM debian:trixie-slim`, with `curl` and `ca-certificates`.
     - Copies `scripts/build-css.sh` and `static/`.
     - Runs `scripts/build-css.sh /out/styles.css`. The script picks the arch from `uname -m`, which covers `TARGETARCH`.
  2. `deno`: `FROM denoland/deno:bin-${DENO_VERSION}`, with `ARG DENO_VERSION` pinned to the current stable 2.x at build time.
  3. Runtime:
     - `FROM python:3.12-slim-trixie`.
     - `apt-get install --no-install-recommends ffmpeg`.
     - `COPY --from=deno /deno /usr/local/bin/deno`.
     - `pip install --no-cache-dir -r requirements.txt`.
     - `ARG YTDLP_VERSION=2026.10.26` followed by `pip install "yt-dlp[default]==${YTDLP_VERSION}"`. This is a no-op unless the version is overridden.
     - Copy `app/` and `static/`, plus the built CSS into `static/dist/`. These are root-owned and read-only to the app user.
     - `useradd --uid 10001 --create-home app`.
     - `mkdir /tmp/media-jobs && chown app /tmp/media-jobs`.
     - `USER app`, `ENV PYTHONUNBUFFERED=1`, `EXPOSE 8000`.
     - A `HEALTHCHECK` using Python urllib against `http://127.0.0.1:8000/health`.
     - `CMD ["python", "-m", "app"]`.
- `compose.yaml`: one service, `saveme`.
  - `build: .`, `image: saveme:local`
  - `ports: ["127.0.0.1:8000:8000"]`
  - `env_file: [{path: .env, required: false}]`
  - `init: true`, `restart: unless-stopped`, `stop_grace_period: 20s`
  - `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`
  - `mem_limit: 2g`, `cpus: 2`, `pids_limit: 256`
  - a healthcheck mirroring the Dockerfile's
- `.env.example`: every variable from Global Constraints, each with its default value and a one-line comment.
- `.dockerignore`: `.git`, `.venv`, `.cache`, `tests`, `docs`, `**/__pycache__`, `static/dist`, `.env`.

- [ ] **Step 1: Write the failing packaging tests.**
  - `test_dockerfile_ytdlp_arg_matches_lock`: the `ARG YTDLP_VERSION=` default equals the `yt-dlp==` pin in `requirements.txt`.
  - `test_env_example_matches_settings_defaults`: parse `.env.example`. The keys are exactly the Settings variables, and `load_settings(parsed) == load_settings({})`.
  - `test_compose_binds_loopback_only`: `compose.yaml` text contains `"127.0.0.1:8000:8000"` and no other `ports` entries.
  - `test_no_curl_cffi_in_lock`: `curl` doesn't appear in `requirements.txt`, case-insensitively.

- [ ] **Step 2: Run the tests and confirm they fail.** Run `.venv/bin/pytest tests/unit/test_packaging.py -v`. Expected: FAIL.

- [ ] **Step 3: Write the Docker files.** Then run the packaging tests until they pass.

- [ ] **Step 4: Write `scripts/docker-smoke.sh`.** It uses `set -euo pipefail` and:
  1. Runs `docker compose up -d --build`.
  2. Waits up to 90 s for `docker inspect -f '{{.State.Health.Status}}'` to report `healthy`.
  3. Checks that `curl -s 127.0.0.1:8000/health` returns `{"status":"ok"}`.
  4. Checks that inspecting `not a url` returns HTTP 400.
  5. Checks that inspecting `http://169.254.169.254/` returns HTTP 403.
  6. Checks that `docker compose exec -T saveme id -u` prints `10001`.
  7. Checks that `docker compose exec -T saveme python -c "import importlib.util,sys; sys.exit(importlib.util.find_spec('curl_cffi') is not None)"` exits 0.
  8. Checks that `deno --version` and `ffmpeg -version` run inside the container.
  9. Prints `SMOKE OK`.

  It leaves the container running and states that in its last line, so the walkthrough can use it.

- [ ] **Step 5: Run the smoke test.** Run `scripts/docker-smoke.sh`. Expected: the last line is `SMOKE OK`. Then run `docker compose down`, run it again from a clean state (`docker compose down && docker image rm saveme:local`), and confirm it still passes.

- [ ] **Step 6: Walk through the page with the browser tool against the container.**
  - Use the direct-MP4 and YouTube URLs from `tests/network/urls.py` (created in Task 15). Create that file first if it doesn't exist yet.
  - Checks:
    - **M3:** inspect, then Download Video. While downloading, double-clicking leaves only one job (check the server logs). Save File downloads a playable MP4.
    - **M4:** start a job and reload mid-download. The page resumes the same job and finishes.
    - **M5:** start a job, then `docker compose restart`. The page shows the "download was lost" message.
    - **M6:** Download Audio produces an MP3. The keyboard alone (Tab, Enter) can complete the flow. Dark mode renders correctly (`emulate_media colorScheme=dark`).
  - Note any failures. Fix them in this task before committing.

- [ ] **Step 7: Commit.** Message: `feat: add Docker image, compose setup and smoke test`.

---

### Task 15: Real-site checks, README, final report

**Files:**
- Create: `tests/network/__init__.py`, `tests/network/urls.py`, `tests/network/test_real_sites.py`
- Modify: `README.md`, `JOURNAL.md` (prepend)

**Interfaces:**
- `tests/network/urls.py` holds constants, all in this one place:
  - `YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"` and `YOUTU_BE = "https://youtu.be/jNQXAC9IVRw"`
  - `REDDIT`: one public video post (the builder picks)
  - `DIRECT_MP4`: a stable public sample MP4 (the builder picks)
  - `UNSUPPORTED = "https://streamtape.com/"`
  - `PRIVATE_IP = "http://192.168.1.1/"`
- The tests use httpx against `SAVEME_BASE_URL` (default `http://127.0.0.1:8000`). They skip if `/health` is unreachable. Every test carries `@pytest.mark.network`.

- [ ] **Step 1: Write the network tests.**
  - `test_youtube_inspect_and_480p_and_mp3`: inspect lists `best` and `480`. A 480p video job reaches ready, and the downloaded file's ffprobe shows h264. An audio job's ffprobe shows mp3.
  - `test_youtu_be_short_link_inspects`.
  - `test_reddit_video`: inspect, then a video job reaches ready.
  - `test_direct_mp4`: inspect, then a video job reaches ready.
  - `test_unsupported`: 422 `unsupported`.
  - `test_private_ip_blocked`: 403 `blocked_address`.

- [ ] **Step 2: Run them against the container.** Run `docker compose up -d && .venv/bin/pytest -m network -v`. Record exactly which tests pass. Don't edit assertions to make a failing site pass. A failure caused by the site (rate limit, removed video) gets a replacement URL in `urls.py`, and the swap is noted.

- [ ] **Step 3: Write `README.md`.** Sections:
  - What it is
  - One-command start (`docker compose up -d --build` → `http://localhost:8000`)
  - Usage
  - What works and what doesn't: verified sites (from Step 2), best-effort sites (Facebook, Instagram and Twitter/X are "not verified" unless actually tested), not supported (Streamtape, LuluVid and other JS file hosts, coming in Build 2; DRM; logins; playlists; live streams)
  - Environment variables (the table)
  - Updating yt-dlp: bump `requirements.in` and recompile, or quick-override with `docker compose build --no-cache --build-arg YTDLP_VERSION=<new> && docker compose up -d`
  - Troubleshooting: unhealthy container → `docker compose logs`, the config banner, "requires login", rate limits, disk full
  - Safety notes: local-only binding, never expose publicly, the SSRF model in brief, Deno runs without network permission
  - Running tests: offline, network, smoke

- [ ] **Step 4: Run the full verification.**
  - Run `.venv/bin/pytest -q`. Expected: all offline tests pass.
  - Run `scripts/docker-smoke.sh`. Expected: `SMOKE OK`.
  - Run `.venv/bin/pytest -m network -v` and note the results.

- [ ] **Step 5: Commit.** Message: `docs: add README and real-site verification suite`.

- [ ] **Step 6: Prepend a `JOURNAL.md` entry for the session.** Use the format of the git-workflow-discipline skill:
  - an IST timestamp, a summary, a commit table from `git log --oneline`, key decisions, and issues encountered
  - an explicit list of verified and unverified sites

  Commit with `docs: update journal`.
