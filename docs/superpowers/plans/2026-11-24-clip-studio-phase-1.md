# Clip Studio Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manual Clip Studio to SaveMe. It loads a video from a link or an uploaded file, previews it, lets you pick a start and end, and cuts that range server-side by FFmpeg stream copy as a cancellable job with progress, export and cleanup.

**Architecture:**
- Sources and clips are new kinds of `Job` in the existing `JobManager`, so they share its queue, slots, disk budget, sweep and shutdown.
- Probing and cutting are new worker modes, backed by a new local-media module, `app/mediafile.py`.
- The page gains `static/common.js` (helpers moved out of `app.js`) and `static/studio.js` (the studio), with no new libraries.

**Tech Stack:** Python 3.12, FastAPI/Starlette 1.10, the FFmpeg/ffprobe wrappers in `app/bin`, vanilla JavaScript, and Tailwind v4 (classes in HTML).

**Spec:** `docs/superpowers/specs/2026-11-24-clip-studio-phase-1-design.md`

## Global Constraints

- No React, no database, no paid or external services, no new Python or JS dependencies.
- Every protection stays: the URL checks, the connection guard, the FFmpeg whitelist, the 64 KiB body cap (except `POST /api/sources/upload`), Host/Origin, degraded mode, the worker memory cap, the queue and the disk budget.
- FFmpeg and ffprobe run only inside the worker subprocess, through `app/bin` wrappers.
- `app/mediafile.py` and `app/worker.py` must not import `yt_dlp` or `app.ytdl` at module level (the guard is installed first).
- New setting: `MAX_CLIP_SOURCES`, default `2`, range `1`-`20`.
- `MIN_CLIP_SECONDS = 0.1`; end tolerance `0.05` s; times are rounded to milliseconds.
- New error codes and statuses:

  | Code | Status | Message |
  |---|---|---|
  | `source_not_found` | 404 | "This video is no longer open for clipping. It may have been closed, or removed after a while unused. Load it again." |
  | `source_not_ready` | 409 | "The video is still loading. Wait until it's ready, then try again." |
  | `invalid_clip` | 400 | "Choose a start before the end, inside the video." |
  | `unreadable_media` | 422 | "SaveMe can't read this file as a video. Try an MP4, MOV, MKV or WebM file." |
  | `upload_incomplete` | 400 | "The upload stopped before the whole file arrived. Please try again." |
  | `clip_failed` | 422 | "Couldn't cut this clip. Try a different start or end." |

- The clip filename is `"<title> (clip <a>-<b>).<ext>"`, with times like `0m12s`, `1m40.5s`, `1h02m05s`.
- Page copy (exact):
  - "Clip a video from your computer"
  - "Clips are cut without re-encoding, so they keep the original quality. A few players may show up to a few seconds before your start."
  - "Clip saved. Mark another part, or close the studio."
  - "Close Clip Studio to fetch another link."
  - "Your browser can't preview this video. You can still type a start and end."
- No `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `eval` in any static JS. No inline scripts or styles.
- JOURNAL.md: after every task, a `docs: update journal` commit. It updates the top session entry with the task's commit, the test results, pending work and handoff notes.

## Review Focus

1. **A source closed or expired while its clip is queued or running.** Expected: the clip is cancelled, no folder is orphaned, and polling the clip shows `cancelled`. Test in Task 5.
2. **An upload abandoned mid-transfer** (tab closed, Cancel, reload). Expected: the record and folder are removed and the queue place is freed. Test in Task 7.
3. **A source the browser can't play** (MKV, HEVC, a broken file). Expected: the editor still works with typed times and the server's duration. Covered by the browser check in Task 9; the server side (duration from `probe`) is tested in Task 2.
4. **Times reversed, out of range, too short, or not numbers.** Expected: a clear 400 (`invalid_clip`, or `invalid_request` naming the field), and the page shows it next to the fields. Tests in Tasks 5 and 6.
5. **The server restarts while a studio is open.** Expected: the page's next poll gets `source_not_found`, clears `saveme.studio` and says the video is gone. Test (API side) in Task 6; page behaviour in the Task 9 browser check.

---

### Task 1: Local-media module, and duration in download results

**Files:**
- Create: `app/mediafile.py`
- Modify: `app/worker.py` (remove `_to_mp4` and `_stream_kinds`; use `mediafile`; add `duration` to the download result)
- Test: `tests/integration/test_mediafile.py`; adjust `tests/integration/test_worker_download.py` imports (`_to_mp4` → `mediafile.remux_to_mp4`)

**Interfaces:**
- Produces:
  - `WRAPPER_DIR: Path` (`app/bin`).
  - `@dataclass(frozen=True) class Probe: kinds: frozenset[str]; duration: float | None; format_name: str; codecs: tuple[str, ...]`. `kinds` and `codecs` leave out attached pictures.
  - `probe(path: Path) -> Probe | None`. ffprobe JSON (`-show_entries format=format_name,duration:stream=codec_type,codec_name:stream_disposition=attached_pic`); None on any failure.
  - `remux_to_mp4(src: Path) -> tuple[Path, str | None]`. Same behaviour as today's `_to_mp4`.
  - `stderr_tail(data: bytes) -> str`.
- The download result dict gains `"duration": float | None` (the final file's probed duration).

- [ ] **Step 1: Write failing tests**
  - `test_probe_reports_kinds_duration_and_codecs`: on `media["sample.mp4"]`, `kinds == {"video","audio"}`, `abs(duration - 2.0) < 0.1`, `"mov" in format_name`, `codecs == ("h264","aac")`.
  - `test_probe_returns_none_for_junk`: on `media["junk.jpg"]`, it returns `None`.
  - `test_probe_ignores_cover_art`: an MP4 with a cover added (mutagen `covr`) still has `kinds == {"video","audio"}`.
  - Move the two existing `_to_mp4` tests to import `remux_to_mp4` from `app.mediafile`.
  - `test_download_result_includes_duration` in `test_worker_download.py`: `/media/sample.mp4`, video; `abs(result["duration"] - 2.0) < 0.1`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/integration/test_mediafile.py tests/integration/test_worker_download.py -q`. Expected: the new tests FAIL (import error or missing key).
- [ ] **Step 3: Implement `app/mediafile.py`, and switch `worker.py` to `probe()` and `remux_to_mp4()`.** `_lacks_audio` uses `probe(p).kinds`. `_download` returns `duration` from `probe(final)` for every output.
- [ ] **Step 4: Run** the full suite, `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `refactor: move local media helpers to app/mediafile.py and report duration`, then the journal commit.

### Task 2: Worker `probe` mode for uploads

**Files:**
- Modify: `app/mediafile.py`, `app/worker.py`, `app/errors.py` (add `unreadable_media`)
- Test: `tests/integration/test_worker_probe.py`; `tests/support/media.py` (add `sample.mkv`, `sample.ts`, `sample.webm` (VP9 + Opus) and a 10 s `long.mp4` with a keyframe every 1 s and chapters, used by Task 3)

**Interfaces:**
- Produces:
  - `container_ext(p: Probe) -> str | None`. mov family → `"mp4"`; `matroska,webm` → `"webm"` if every codec is in `{vp8, vp9, av1, opus, vorbis}`, else `"mkv"`; `mpegts` → `"ts"`; `flv` → `"flv"`; `avi` → `"avi"`; anything else → None.
  - `prepare_upload(src: Path, workdir: Path) -> dict`, returning `{"file", "ext", "duration", "notice": None}`.
  - Worker request: `{"mode": "probe", "source": "<workdir>/upload.bin", "workdir": "<abs dir>"}`. `source` must be a regular, non-symlink file whose parent is `workdir`; otherwise `internal`.
  - Worker modes now dispatch before any URL check. Only `inspect` and `download` need `url`.

- [ ] **Step 1: Write failing tests** (each copies the input to `job/upload.bin` and runs `run_worker_proc`):
  - `test_probe_keeps_mp4`: result `file == "media.mp4"`, `ext == "mp4"`, `abs(duration-2) < 0.1`, and the folder lists only `media.mp4`.
  - `test_probe_remuxes_mkv_and_ts_to_mp4` (parametrized over `sample.mkv` and `sample.ts`): `ext == "mp4"`, and the output probes as h264+aac.
  - `test_probe_webm_becomes_mp4_or_stays_webm`: `ext in {"mp4","webm"}`.
  - `test_probe_refuses_audio_only`: `sample.mp3` gives error code `no_video`, and the folder is empty.
  - `test_probe_refuses_junk`: `big.mp4` (random bytes) gives `unreadable_media`, and the folder is empty.
  - `test_probe_refuses_source_outside_workdir`: gives `internal`.
  - `test_container_ext_mapping` (unit, built from `Probe` values): covers each row of the mapping.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/integration/test_worker_probe.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `container_ext` and `prepare_upload`. Rename to `media.<ext>`; for any ext other than mp4, call `remux_to_mp4`. A remux failure keeps `mkv`/`webm`; any other ext raises `unreadable_media`. Add `run_probe(req, emit) -> dict` to `worker.py`, which validates arguments and clears the folder on failure, as `run_download` does.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: add a worker probe mode that checks and remuxes uploaded videos`, then the journal commit.

### Task 3: Worker `clip` mode

**Files:**
- Modify: `app/mediafile.py`, `app/worker.py`, `app/errors.py` (add `clip_failed`)
- Test: `tests/integration/test_worker_clip.py`

**Interfaces:**
- Produces:
  - `cut(src: Path, dst: Path, *, start: float, end: float, keep_tags: bool, max_bytes: int, on_progress: Callable[[float | None, int | None], None]) -> None`. It raises `AppError` (`too_large`, `clip_failed`).
  - Worker request: `{"mode": "clip", "source": "<abs media.<ext>>", "workdir": "<abs dir>", "start": float, "end": float, "keep_tags": bool, "max_bytes": int}`. The result is `{"file": "media.<ext>", "ext": "<ext>"}`.
  - Events: `{"event": "progress", "detail": "Cutting the clip", "percent": float | None, "downloaded_bytes": int | None, "total_bytes": None}`, at most 4 per second plus the last one.
- FFmpeg arguments are exactly as in spec §6.5. `-movflags +faststart` is added only for mp4, and `-map_metadata -1` only when `keep_tags` is false. Times are formatted `f"{x:.3f}"`.

- [ ] **Step 1: Write failing tests** (source `long.mp4`, 10 s, keyframe every 1 s, title tag and chapters):
  - `test_clip_cuts_the_range`: start 3.3, end 7.3; `abs(duration - 4.0) < 0.15`; streams h264+aac; the folder lists only `media.mp4`.
  - `test_clip_reports_progress`: at least one progress event; percents never decrease; the last is ≥ 99.
  - `test_clip_drops_chapters_keeps_title_when_asked`: with `keep_tags` true, the title is present and there are no chapters.
  - `test_clip_drops_all_tags_for_uploads`: with `keep_tags` false, no title tag.
  - `test_clip_too_large`: `max_bytes=1000` gives `too_large`, and the folder is empty.
  - `test_clip_bad_request_is_internal`: `end <= start`, or a `source` that isn't `media.<ext>`, gives `internal`.
  - `test_clip_from_mkv_stays_mkv`: from `sample.mkv`, `ext == "mkv"`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/integration/test_worker_clip.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `cut` with `subprocess.Popen` (stdout PIPE) parsing `out_time_us`, `total_size` and `progress=end`. Then probe the output: no video or a zero duration gives `clip_failed`; a size ≥ `max_bytes` gives `too_large`. Add `run_clip(req, emit)` with argument checks and `_clear` on failure.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: add a worker clip mode that cuts by stream copy with progress`, then the journal commit.

### Task 4: Sources in the job manager (link sources, close, idle TTL, cap)

**Files:**
- Modify: `app/config.py` (`max_clip_sources`), `app/errors.py` (`source_not_found`, `source_not_ready`), `app/jobs.py`
- Test: `tests/integration/test_sources.py` (reuses `StubRunner`/`FakeClock` patterns from `test_jobs.py`; move both into `tests/support/jobs.py` and import them in both files); `tests/unit/test_config.py`

**Interfaces:**
- Produces (on `JobManager`):
  - `open_link_source(url: str, quality: str) -> Job`
  - `get_source(source_id: str) -> Job`, which raises `source_not_found`
  - `source_view(job: Job) -> dict` (the keys in spec §6.3)
  - `source_media(source_id: str) -> tuple[Path, str]`, which touches the source and raises `source_not_ready` unless it's ready
  - `touch(job: Job) -> None`
  - `close_source(source_id: str) -> Job`
  - `get(job_id)` now refuses sources.
  - `public_view` gains `"kind"`.
- `Job` gains:
  - `kind: str = "download"`
  - `origin: str | None = None`
  - `source_id: str | None = None`
  - `start: float | None = None`
  - `end: float | None = None`
  - `duration: float | None = None`
  - `last_used: float | None = None`
- Statuses: `_PENDING` adds `uploading`, `checking` and `cutting`; `_ACTIVE` adds the same three.
- Expired reasons: `closed`, `idle`, `replaced`.

- [ ] **Step 1: Write failing tests:**
  - `test_link_source_downloads_and_stays_ready`: `open_link_source`, then finish the stub. The view has `status "ready"`, `origin "link"`, `title "My Video"` and the duration from the result. `get(id)` raises `job_not_found`.
  - `test_source_is_not_delivered_or_counted_as_ready_file`: with `max_ready_files=1`, a ready source plus 2 ready downloads leaves the source ready. `claim_delivery(source.id)` raises `job_not_found`.
  - `test_source_is_not_evicted_for_disk_room`: a tight budget gives `disk_full` for a new job; the source stays.
  - `test_idle_source_expires_after_ttl`: advance the clock by the TTL and sweep; `expired`, reason `idle`, folder gone. Touching via `source_media` just before the TTL keeps it ready.
  - `test_close_source_in_each_state`: queued, downloading and ready all end `expired`/`closed` with the folder gone; a second close is a no-op.
  - `test_source_cap_replaces_least_recently_used`: `max_clip_sources=2`; a third open closes the LRU ready source (`replaced`). When both are still downloading, the third gets `busy`.
  - `test_source_media_requires_ready`: gives `source_not_ready`.
  - `test_config_max_clip_sources`: default 2; `"0"` and `"21"` raise `ConfigError("MAX_CLIP_SOURCES")`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/integration/test_sources.py tests/unit/test_config.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** in `jobs.py`:
  - A link source runs `_download` (the same worker request as a video download).
  - `_make_ready` stores `duration` and sets `last_used`.
  - `_enforce_ready_cap` and the `_make_room` eviction skip `kind == "source"`.
  - The sweep expires idle sources (no pending clip).
  - The orphan sweep's live set includes open sources.
  - A source's run on `CancelledError` doesn't keep its folder.
- [ ] **Step 4: Run** the full suite. Expected: all pass (update the existing exact-key assertions in `test_jobs.py` for `kind`).
- [ ] **Step 5: Commit** `feat: keep clip sources in the job manager with close, idle expiry and a cap`, then the journal commit.

### Task 5: Clips and uploads in the job manager

**Files:**
- Modify: `app/jobs.py`, `app/filenames.py`, `app/errors.py` (`invalid_clip`, `upload_incomplete`)
- Test: `tests/integration/test_sources.py`, `tests/unit/test_filenames.py`

**Interfaces:**
- Produces:
  - `submit_clip(source_id: str, start: float, end: float) -> Job`
  - `begin_upload(title: str | None) -> Job` (status `uploading`, folder created, admitted with `ensure_queue_room` and `_make_room(count_active=True)`, cap applied)
  - `finish_upload(job: Job) -> None` (queues the `probe` run)
  - `abort_upload(job: Job) -> None` (forgets the record, removes the folder)
  - `UPLOAD_NAME = "upload.bin"` and `MIN_CLIP_SECONDS = 0.1` in `app/jobs.py`
  - `clip_filename(title: str | None, start: float, end: float, ext: str) -> str` in `app/filenames.py`
- Worker requests are those of Tasks 2 and 3. A clip's `keep_tags` is `source.origin == "link"`; its `max_bytes` is `settings.max_file_bytes`.

- [ ] **Step 1: Write failing tests:**
  - `test_clip_filename`: `("Talk", 65, 100.5, "mp4")` gives `"Talk (clip 1m05s-1m40.5s).mp4"`; `(None, 0, 12, "mkv")` gives `"download (clip 0m00s-0m12s).mkv"`; 3725 s formats as `1h02m05s`.
  - `test_clip_runs_and_becomes_a_normal_ready_job`: the stub sees mode `clip` with `source` set to the source's file, `keep_tags True`, and rounded times. Progress events set status `cutting`. When ready, `filename` matches `clip_filename`, `public_view["kind"] == "clip"`, and delivery then expires it.
  - `test_clip_validation`: `end-start < 0.1`, `start >= duration`, `end > duration+0.05` and `start < 0` each raise `invalid_clip`. `end` of `duration+0.04` is clamped to the duration. A source that isn't ready gives `source_not_ready`; an unknown ID gives `source_not_found`.
  - `test_clip_cancel_has_no_resume`: cancel while running gives `cancelled`, `resume_seconds None`, folder gone; `resume` raises `not_resumable`.
  - `test_closing_source_cancels_its_clips` (Review Focus 1): close with one clip queued and one running; both `cancelled`, every folder gone after the stubs finish.
  - `test_source_in_use_does_not_expire`: a pending clip keeps the source alive past the TTL; when the clip ends, `last_used` resets.
  - `test_upload_lifecycle`: `begin_upload("clip.mov")`, write the file, `finish_upload`; the stub sees mode `probe`; the result makes the source ready with `origin "upload"`.
  - `test_abort_upload_frees_place` (Review Focus 2): `abort_upload` leaves no record, no folder, and room in the queue.
  - `test_upload_admission_counts_active_jobs`: a running job's reservation makes `begin_upload` raise `disk_full`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** `_run_job` dispatches by kind and origin to `_download`, `_probe_upload` or `_clip`, all through one `_run_worker_into(job, request, *, keep_on_cancel: bool)`. `cancel()` gives clips no resume window. `close_source` cancels the source's pending clips. `_on_event` uses `cutting` for clips.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: run clip and upload jobs in the job manager`, then the journal commit.

### Task 6: Studio API routes (no upload yet)

**Files:**
- Create: `app/studio_routes.py`
- Modify: `app/main.py` (include the routes; `_validation_message` handles number errors), `app/schemas.py`
- Test: `tests/integration/test_studio_api.py`

**Interfaces:**
- Consumes: the Task 4-5 manager methods.
- Produces:
  - `add_studio_routes(router: APIRouter, settings: Settings, jobs: Callable[[], JobManager], *, ip_policy, resolver) -> None`, called from `_api_router`.
  - Schemas: `SourceRequest(url: str, quality: Quality = "best")` and `ClipRequest(start: float, end: float)`, both `extra="forbid"`, with times `Field(strict=True, ge=0, allow_inf_nan=False)`.
  - Routes and shapes exactly as in spec §6.3. The media route returns `FileResponse(path, media_type=..., headers={"Cache-Control": "no-store"})`.
  - `_validation_message` for `float_type`, `finite_number` and `greater_than_equal` on `start`/`end` returns `"<field> must be a number of seconds, 0 or more"`.

- [ ] **Step 1: Write failing tests** (`make_app`/`running` from `test_api.py`, moved to `tests/support/api.py`):
  - `test_studio_limits`: `GET /api/studio` gives `{"max_upload_bytes": settings.max_file_bytes, "idle_minutes": settings.job_ttl_minutes, "min_clip_seconds": 0.1}`.
  - `test_link_source_flow`: POST `/api/sources`, poll to ready; `GET media` gives 200 with `video/mp4`, `Cache-Control: no-store`; `Range: bytes=0-99` gives 206 with 100 bytes.
  - `test_clip_flow_end_to_end_with_stub`: POST clips, poll `/api/jobs/{id}` to ready, `GET /file` gives a body, then `expired`/`delivered`.
  - `test_clip_request_validation` (Review Focus 4): `{"start": "1", "end": 2}` and `{"start": true, ...}` give 400 `invalid_request` naming `start`; reversed times give 400 `invalid_clip`; an extra field gives 400.
  - `test_unknown_source_and_cross_kind_ids`: a job ID on `/api/sources/{id}` gives 404 `source_not_found`; a source ID on `/api/jobs/{id}` gives 404 `job_not_found`.
  - `test_close_route_is_post_with_origin_check`: a foreign Origin gives 403 `forbidden_origin`; same-origin gives 200 with `status "expired"`, `reason "closed"`.
  - `test_source_gone_after_restart` (Review Focus 5): open a source, restart the app (new lifespan); `GET /api/sources/{id}` gives 404 `source_not_found`.
  - `test_link_source_refuses_private_address`: `blocked_address`, as in `POST /api/jobs`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/integration/test_studio_api.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the routes. `POST /api/sources` mirrors `create_job`, including the `checking_jobs` place reservation. Move that counter into a small shared object so both routes count against the same queue.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: add Clip Studio API routes for sources, preview and clips`, then the journal commit.

### Task 7: Upload route

**Files:**
- Modify: `app/main.py` (the Guard skips `_read_body` for `POST /api/sources/upload` only), `app/studio_routes.py`
- Test: `tests/integration/test_studio_api.py`

**Interfaces:**
- Produces:
  - `UPLOAD_PATH = "/api/sources/upload"` in `app/studio_routes.py`
  - `receive_upload(request: Request, dest: Path, size: int, *, idle: float, limit: float, is_open: Callable[[], bool]) -> None`. It raises `AppError` (`timeout`, `upload_incomplete`, `invalid_request`, `source_not_found` when closed meanwhile).
  - Title from `X-SaveMe-Filename`: `unquote` (errors replaced), cut to 1,024 characters, extension stripped, blank becomes None. The view shows "Uploaded video" for None.
  - `create_app` gains `upload_idle_seconds: float = 60.0` for tests.

- [ ] **Step 1: Write failing tests:**
  - `test_upload_success`: POST 4 KiB of `sample.mp4` bytes with the octet-stream type, Content-Length and `X-SaveMe-Filename: My%20Clip.mp4`. Gives 202; the stub's probe call sees `source == <workdir>/upload.bin` with the same bytes; the source is ready with title "My Clip".
  - `test_upload_header_checks`: a wrong content type gives 415; no length (chunked) gives 411; length 0 gives 400; length over `max_file_bytes` gives 413 `too_large`. None of these leaves a record or folder.
  - `test_upload_bypasses_only_its_own_body_cap`: a 100 KiB upload is accepted, while a 100 KiB POST to `/api/sources` still gets 413 `request_too_large`.
  - `test_upload_origin_and_host_checks_still_apply`: a foreign Origin gives 403; a bad Host gives 400.
  - `test_upload_short_body_or_disconnect_cleans_up` (Review Focus 2): a body that ends early, sent through a real uvicorn server (as in the existing aborted-download test), leaves no folder and no pending job.
  - `test_upload_idle_timeout`: with `upload_idle_seconds=0.2`, a client that stalls mid-body gives `timeout`, and everything is cleaned up.
  - `test_upload_closed_mid_transfer`: closing the source while bytes are still arriving stops the upload, and the folder is removed.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** `request.stream()` with `anyio.fail_after` per chunk and an overall deadline; 1 MiB buffered writes through `anyio.to_thread.run_sync`; `ClientDisconnect` becomes `upload_incomplete`; `abort_upload` runs in a `finally` on any failure.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: accept video uploads for Clip Studio with their own limits`, then the journal commit.

### Task 8: Shared page helpers

**Files:**
- Create: `static/common.js`
- Modify: `static/app.js` (use `window.SaveMe`), `static/index.html` (scripts: `common.js`, `studio.js`, `app.js`, all `defer`; studio.js is added in Task 9), `static/src/input.css` (`@source` the new JS files), `tests/unit/test_page.py`

**Interfaces:**
- Produces `window.SaveMe = Object.freeze({ ApiError, api, isNum, str, fmtMB, fmtDuration, httpUrl, setText, show, setLine, JOB_ID_RE, MSG_OFFLINE, MSG_BAD_REPLY })`. The behaviour is identical to the current `app.js` helpers.

- [ ] **Step 1: Update tests:**
  - `test_references_built_css_and_deferred_scripts`: the scripts are `common.js` then `app.js`, all `defer`.
  - `test_static_js_never_injects_html` covers every `static/*.js`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/unit/test_page.py -q`. Expected: FAIL.
- [ ] **Step 3: Move the helpers;** `app.js` destructures them from `window.SaveMe`.
- [ ] **Step 4: Run** the tests, then load the page in a browser: inspect a direct MP4 from the local media server and download it, with no console errors.
- [ ] **Step 5: Commit** `refactor: share the page's API and DOM helpers through common.js`, then the journal commit.

### Task 9: Clip Studio page

**Files:**
- Create: `static/studio.js`
- Modify: `static/index.html` (Clip button, file control, `#studio` section), `static/app.js` (studio state and entry points), `static/src/input.css` (`#how` hides while `#studio` shows), `tests/unit/test_page.py`

**Interfaces:**
- Produces `window.SaveMeStudio = Object.freeze({ openLink({url, quality, title}), openFile(file), restore(): boolean, isOpen(): boolean, onClosed(callback) })`.
- Element IDs:
  - `clip`, `upload-file`, `upload-label`, `studio`, `studio-title`, `studio-close`
  - Loading: `studio-loading`, `studio-load-status`, `studio-load-percent`, `studio-load-bar`, `studio-load-fill`, `studio-load-meta`, `studio-load-error`, `studio-load-cancel`
  - Editor: `studio-editor`, `studio-video`, `studio-preview-note`, `timeline`, `timeline-range`, `timeline-playhead`, `handle-start`, `handle-end`, `clip-start`, `clip-end`, `set-start`, `set-end`, `clip-length`, `play-clip`, `export-clip`, `clip-msg`
  - Export: `export`, `export-status`, `export-percent`, `export-bar`, `export-fill`, `export-meta`, `export-save`, `export-error`, `export-cancel`
- Storage key `saveme.studio`: `{id, title, start, end, exportId, exportAutoSaved}`.
- `parseTime(text): number | null` accepts `ss(.s)`, `m:ss(.s)` and `h:mm:ss(.s)`. `fmtTime(seconds): string` gives `m:ss.s`, or `h:mm:ss.s` from one hour.

- [ ] **Step 1: Write failing static tests:**
  - `test_studio_markup`: every ID above exists; buttons have `type="button"`; `#studio`, `#studio-editor` and `#export` start `hidden`; the file input has `type=file` and an `accept` containing `video/*`; `#handle-start` and `#handle-end` have `role="slider"`.
  - `test_studio_js_pinned_copy`: the spec copy strings and `/api/sources` appear in `studio.js`; `sessionStorage` is used.
  - The scripts list is `common.js`, `studio.js`, `app.js`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/unit/test_page.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the markup, `studio.js` and the `app.js` hooks as in spec §6.6. Uploads use `XMLHttpRequest` with `upload.onprogress`, check against `GET /api/studio` limits first, and abort on Cancel or Close. While the source is ready and the tab is visible, poll it every 60 s. One export at a time.
- [ ] **Step 4: Run** the tests. Then `scripts/build-css.sh` and `python -m app`, and a browser check at 375 px and desktop, in light and dark:
  - link source → clip → saved file plays;
  - upload source (MP4, and MKV with HEVC for the preview fallback) → clip;
  - cancel an export; close mid-download; reload keeps the studio;
  - restart the server → gone message;
  - keyboard: handles, `I`/`O`, Tab order.
- [ ] **Step 5: Commit** `feat: add the Clip Studio page`, then the journal commit.

### Task 10: Docs, image checks and wrap-up

**Files:**
- Modify: `README.md`, `.env.example`, `scripts/docker-ffmpeg-check.sh` (cases "cut H.264 MP4 by stream copy" and "cut H.264 TS into MP4"), `tests/unit/test_packaging.py` (if it pins the README or env vars)

- [ ] **Step 1:** README: Features, Usage (Clip Studio), Configuration (`MAX_CLIP_SOURCES`), Security (uploads; cuts run in the worker; uploaded files lose their metadata in clips), How it works. `.env.example` gets the new setting.
- [ ] **Step 2: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 3: Run** `scripts/docker-ffmpeg-check.sh`. Expected: every case passes and the integration tests pass inside the image (Linux, minimal FFmpeg).
- [ ] **Step 4: Run** `scripts/docker-smoke.sh` in an isolated Compose project (not the live container). Expected: it passes.
- [ ] **Step 5: Commit** `docs: describe Clip Studio`, then the final journal commit with the full verification record and the handoff for Phase 2.
