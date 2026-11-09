# Media URL Downloader ("saveme"), Build 1 (MVP): Design

**Date:** 2026-11-09
**Requirements source:** [`Media_URL_Downloader_BRD.md`](../../../Media_URL_Downloader_BRD.md) (v1.0). This document does not repeat the BRD; it records what was decided on top of it. Where the two conflict, this document wins for Build 1.

## 1. Intent

A self-hosted web page, run locally with Docker, where the owner pastes a link to a video, from YouTube, Reddit, Twitter/X, Facebook, Instagram, a direct `.mp4` link or many other sites, and gets back a playable video file or an MP3. It is a personal tool: one user, on their own machine, with no accounts and no history.

The owner wants it to handle "anything," including file hosts such as Streamtape and LuluVid. yt-dlp (version 2026.10.26) has **no extractor** for those hosts, and their pages build the media URL in JavaScript, so they need the headless-browser fallback. **That fallback is Build 2**, by the owner's decision. Build 1 is the BRD's P0 scope and must leave a clean seam for Build 2 (see §10).

### Success, in the owner's terms
- Paste a public YouTube, Reddit or Twitter/X link, or a direct MP4 link, and save a video that plays in QuickTime and on a phone, or an MP3.
- Public Facebook and Instagram posts work when yt-dlp can reach them without a login. Login-gated posts fail with a clear "requires login" message rather than a generic error.
- Unsupported sites (including Streamtape and LuluVid in Build 1) fail fast with a clear message.

### Goals, non-goals, anti-goals
- **Goals:** the BRD's P0 list (§3 of the BRD), all of it.
- **Non-goals for Build 1:** everything in the BRD's P1 list (browser fallback, multi-media selection, cancel, M4A or WebM output, host-mounted output folder), playlists, live streams, logins and cookies.
- **Anti-goals.** Build 1 is a failure, even if it technically works, if:
  - it can be pointed at the owner's LAN, the Docker host or a cloud metadata endpoint through any path (redirects, DNS tricks, HLS segment URLs, FFmpeg reading a playlist);
  - it shows fake progress percentages or quality options that don't exist;
  - it leaves multi-GB orphan files in the container;
  - it claims to support sites it doesn't;
  - it pulls in a database, Redis, Celery, React, telemetry or any hosted service.

## 2. Decisions made (owner-approved)

| Topic | Decision |
|---|---|
| Scope | BRD P0 only. Headless-browser fallback = Build 2. |
| Video "Best" | Highest resolution/fps available **regardless of codec** (may be VP9/AV1 4K that QuickTime can't open; VLC and browsers can). |
| Video 1080p / 720p / 480p | Highest resolution **not exceeding** the choice, **preferring H.264 video + AAC audio in MP4** so it plays everywhere. |
| Quality menu | Only lists resolutions that actually exist for that link. "Best" is always listed when video exists, labelled with its real resolution (e.g. "Best (2160p)"). |
| Quality tiers measure the **short side** *(builder's call, post-review)* | "1080p" means the short side of the frame, matching yt-dlp's `res` sort: a 1080×1920 vertical Short/Reel is "1080p", not "1920p". This applies to the menu, the cap filter, the downgrade check and the "Best (Np)" label, so vertical clips still get an H.264 option. |
| Audio | MP3, LAME VBR quality 2 (~190 kbps). |
| Concurrency | 2 active jobs; up to 4 more queued in memory; beyond that, rejected with "busy, try again shortly". |
| Limits | 2 GB max per file; 30 min max per job; 30 s max per inspection; ready files deleted right after a complete download or 30 min after becoming ready. |
| Safety model | Every network connection the downloader makes is checked at connect time (not just the pasted URL); all media network I/O happens in that guarded process; FFmpeg only reads local files. |
| UI | One responsive page, light and dark mode, Tailwind compiled at image build (no CDN at runtime), rights notice, compatibility notice. |
| Tests | Offline suite using FFmpeg-generated sample media served locally; separate opt-in `network` suite hitting real public links. |
| Git | Work on `feature/media-url-downloader-mvp`, one commit per completed task, JOURNAL.md updated at session end. |

## 3. Architecture

One container, one Python process tree:

```text
Browser ──HTTP──> FastAPI/Uvicorn (API server, never fetches remote URLs itself)
                    │  pre-validates URL (scheme, host, DNS → all public IPs)
                    │  JobManager (in-memory registry, semaphore, bounded queue, cleanup loop)
                    │
                    └─ spawns per inspect/job:  python -m app.worker <mode>   (own process group)
                          │  installs socket connect-guard BEFORE importing yt-dlp
                          │  runs yt-dlp Python API (native downloaders only)
                          │  yt-dlp spawns ffmpeg/ffprobe for merge / MP3 (local files only,
                          │      protocol whitelist = file)
                          │  yt-dlp spawns deno for YouTube JS challenges (no network permission)
                          └─ emits JSON lines on stdout → parent parses progress/result
                    files: TEMP_DIR/<job_id>/  (fixed internal names, never metadata-derived)
```

**Why a subprocess per inspect or job:** it can be hard-killed (the whole process group, including the ffmpeg and deno children) on timeout or size breach. The socket guard is process-wide without affecting the API server. A crash in yt-dlp can't take down the server.

### Suggested file layout (builder may adjust)

```text
app/
  main.py        FastAPI app, routes, static mount, lifespan (startup wipe, cleanup loop, shutdown kill)
  config.py      env-driven settings (validated, with defaults below)
  netguard.py    IP policy, URL pre-validation, socket connect-guard installer
  worker.py      subprocess entry: inspect | download; JSON-lines protocol
  ytdl.py        yt-dlp option building, format selection, info → options mapping
  jobs.py        Job model, JobManager (queue, semaphore, lifecycle, limits, cleanup)
  errors.py      yt-dlp/internal error → (code, user message) mapping
  filenames.py   download-name sanitization, Content-Disposition
static/
  index.html, app.js, src/input.css → (built) dist/styles.css
tests/
  unit/, integration/ (offline, local servers), network/ (opt-in, marker `network`)
Dockerfile, compose.yaml, .dockerignore, .env.example, README.md, pyproject.toml (or requirements*.txt), JOURNAL.md
```

## 4. Network safety (the part that must not be wrong)

### 4.1 IP policy (single source of truth, `netguard.py`)
Allow an address only if `ipaddress.ip_address(x).is_global` is true **and** it is not multicast. Before checking, unwrap IPv4-mapped IPv6 (`::ffff:a.b.c.d`), IPv4-compatible IPv6, 6to4 (`2002::/16`), Teredo, and NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`) to their embedded IPv4 and check that address too. This rejects:
- loopback;
- RFC1918 private ranges;
- CGNAT (`100.64/10`);
- link-local, including `169.254.169.254`;
- ULA, including AWS's `fd00:ec2::254`;
- `0.0.0.0/8`;
- reserved, documentation and benchmark ranges;
- multicast;
- broadcast.

### 4.2 Layer 1: pre-validation in the API server (fast, readable errors)
For both `/api/inspect` and `/api/jobs`, the server rejects a URL unless all of the following hold:
- It is at most 2,048 characters, contains no control characters or whitespace, and parses cleanly.
- The scheme is exactly `http` or `https`.
- The host is non-empty.
- It has **no userinfo**. A URL with credentials, such as `user:pass@host`, is rejected with "links with login details aren't supported".
- The port is valid, if one is given.

The server resolves the host with `getaddrinfo`, with a timeout, run off the event loop. **Every** returned address must pass the policy, and an empty result is an error. Numeric host forms like `2130706433` or `0x7f.1` get resolved and caught here.

### 4.3 Layer 2: connect-time guard inside the worker (authoritative)
`app.worker` installs the guard **before importing yt-dlp**:
- It replaces `socket.socket.connect` and `connect_ex`. For `AF_INET` and `AF_INET6`, if the address is an IP literal, it checks it against the policy. If it is a hostname, it resolves it, checks **all** answers, and connects to a checked IP.
- `AF_UNIX` and other families raise.
- It also wraps `socket.create_connection` and `socket.getaddrinfo` so failures surface early with a clear `BlockedAddress` error.

Because the check happens on the IP actually being connected, it covers:
- redirects;
- DNS rebinding (resolution at validation time vs at connect time);
- manifest and segment URLs;
- thumbnails;
- every extractor sub-request.

The guard raises a dedicated exception, which maps to the `blocked_address` user error.

### 4.4 Closing the side doors
- **Native downloaders only.** Set `external_downloader={'default': 'native'}` and `hls_prefer_native=True`. Exclude formats whose protocol isn't native-downloadable (`rtmp*`, `rtsp`, `mms`, `f4m` if non-native, and so on) with a format filter. Reject live streams (`is_live` or `live_status in {is_live, is_upcoming}`) with "live streams aren't supported".
- **No proxies.** Pass `proxy=''` and strip `*_proxy` / `*_PROXY` env vars from the worker environment. A proxy would make the guard check the proxy, not the target.
- **No curl_cffi.** Install `yt-dlp[default]` only. curl_cffi does its networking in C and would bypass the Python guard. Add a test that asserts `curl_cffi` is not importable in the image.
- **FFmpeg and ffprobe read local files only.** Every ffmpeg/ffprobe invocation gets `-protocol_whitelist file,pipe` (or the equivalent) on inputs, through yt-dlp `postprocessor_args` (input-scoped keys) and any direct calls. Test: a "media" file that is actually an HLS playlist pointing at a local HTTP server must not cause any request to reach that server during MP3 extraction or merge.
- **Deno** (required by yt-dlp ≥2025.11 for full YouTube support, through the bundled `yt-dlp-ejs`). Verify yt-dlp runs it without network permissions. Pass `--no-remote-components`, or the API equivalent, so the EJS scripts come only from the pinned `yt-dlp-ejs` package. If Deno *can* reach the network in yt-dlp's invocation, document it and wrap or deny it.
- **No client-controlled yt-dlp options.** The API accepts only `url`, `output` and `quality` from fixed enums. The worker receives its arguments as a JSON blob over stdin or argv. There is no `shell=True` anywhere.
- **Thumbnails.** The page loads the thumbnail URL directly in the browser with `referrerpolicy="no-referrer"`, and only if it's `http(s)`. The server never proxies thumbnails.

## 5. Inspect and download behaviour

### 5.1 yt-dlp baseline options (both modes)
- `noplaylist=True`, `playlist_items='1'`.
- `socket_timeout` of about 20 s, `retries` of about 3, `fragment_retries` of about 3.
- `noprogress=True` (progress comes from hooks), `quiet=True`.
- `cachedir=False`, no cookies, no `.netrc`, no config files (`ignoreconfig`).
- No overwrite outside the workdir.
- `paths={'home': workdir, 'temp': workdir}` and an output template `media.%(ext)s`. **Titles never touch the filesystem.**

### 5.2 Inspect (`download=False`)
The response maps `info` to the following fields:
- `title`
- `duration_seconds`
- `thumbnail_url`
- `source`: extractor name plus domain
- `webpage_url`
- `video`: `{available, qualities: [{id, label}]}`
- `audio`: `{available}`
- `notices`: a list of messages, for example:
  - "this post has N videos; the first will be downloaded"
  - "only 360p is available"
  - "direct link: details unknown until download"

Rules:
- **Quality ids** are `best`, `1080`, `720` and `480`. A tier appears only if some native-downloadable video format has that **short side**, i.e. `min(width, height)`, or `height` when the width is unknown. The builder picks a small tolerance for 1088/1072-style values.
- **Unknown codecs.** If a format's `vcodec` is unknown (common for direct links via the generic extractor), treat video and audio as "maybe available": offer `best` only and add a notice.
- **No audio stream.** If no format has audio, `audio.available=false` and the MP3 button is disabled with a reason.
- **Rejected cases.** Live and upcoming streams, playlist-only results with no entries, and DRM-flagged formats (`has_drm`) are rejected with readable errors.
- **Limits.** Inspections run under their own semaphore (`MAX_ACTIVE_INSPECTIONS`, default 4) and a hard timeout (`INSPECT_TIMEOUT_SECONDS`, default 30). If too many are already running, the server returns 429.

### 5.3 Video download
- **Format selection:** video-plus-audio merge with a single-file fallback.
  - For `best`: highest resolution, then fps, regardless of codec.
  - For `1080`, `720` and `480`: cap the **short side** at the chosen value (yt-dlp's `res:N` sort), then prefer `vcodec:h264` and `acodec:aac` / `ext:mp4:m4a`. yt-dlp's `-t mp4` preset shows the sort string pattern.
- **Containers:** `merge_output_format='mp4/mkv'` and remux single-file results to MP4 where possible. If the result can't be MP4, deliver what was produced with the correct MIME type and a notice.
- **Explicit downgrade:** the worker re-extracts at job time. If the chosen format's short side is below the requested quality, the job carries a `notice`, for example "1080p wasn't available; downloaded 720p." It never silently downgrades.

### 5.4 Audio (MP3)
- **Format and conversion:** format `bestaudio/best`, then `FFmpegExtractAudio` with `preferredcodec='mp3'` and `preferredquality='2'`.
- **Embedding:** no thumbnail embedding. Basic tag metadata (title, artist) is optional; the builder decides.
- **No audio track:** if the source has no audio, the job fails with "this media has no audio track".

### 5.5 Size, time and disk limits (defense in depth)
- **Per-file size:**
  - yt-dlp `max_filesize` handles size known up front and plain HTTP.
  - The worker's progress hook aborts when `downloaded_bytes` (summed across parts) exceeds `MAX_FILE_SIZE_MB`.
  - The parent polls the workdir size every ~1 s and kills the process group if it's over the limit plus some headroom for merge output.
- **Per-job time:** the parent kills the process group (SIGTERM, then SIGKILL after ~5 s) at `MAX_JOB_SECONDS`.
- **Total disk** *(builder's call, post-review)*: `MAX_TOTAL_DISK_MB` is a soft guard, checked when a job is submitted **and again when a queued job is about to start**. Each active job reserves `2 × MAX_FILE_SIZE_MB`, because a merge holds the parts and the output at the same time. The check passes if the current `TEMP_DIR` usage plus the reservations of other active jobs plus this job's reservation fits within the budget.
  - If it doesn't fit, the oldest **unsaved ready files are evicted first** (the job becomes `expired`, reason "evicted for space").
  - If it still doesn't fit, a submission gets 503 "server storage is full, try again later", and a queued job stays queued until space frees up, its own time limit permitting.
  - The parent's per-job kill threshold is `2 × MAX_FILE_SIZE_MB` of workdir usage, covering the parts plus the merged output.
  - The default budget of 10 GB fits two concurrent maximum-size merges.
- **Ready files:** at most `MAX_READY_FILES` (default 6) are kept. When the limit is exceeded, the oldest ready file expires early.
- **Cleanup on failure:** any failure, timeout or size breach deletes the job's workdir immediately.

## 6. Job lifecycle and API

### 6.1 States
`queued → downloading → converting → ready → expired` with `failed` reachable from any non-terminal state. `expired` covers "delivered and deleted", "TTL passed" and "evicted"; a `reason` string says which. Job records are in-memory only, purged ~1 h after reaching a terminal state, and lost on restart by design.

- **Job IDs:** `secrets.token_urlsafe(16)`, which is also the workdir name. The API validates IDs against `^[A-Za-z0-9_-]{22}$` before any filesystem use.
- **Progress:**
  - `progress_percent` is set only when an exact `total_bytes` is known, or a fragment index and count are known. It is never set from `total_bytes_estimate` alone. Otherwise it is null, and the UI shows bytes downloaded with an indeterminate bar.
  - With separate video and audio streams, `detail` says "Downloading video track (1 of 2)" and so on, and the percent is per track.
  - Converting has a null percent.

### 6.2 Endpoints (BRD contract, refined)
| Method & path | Success | Errors |
|---|---|---|
| `POST /api/inspect` `{url}` | 200 inspect payload (§5.2) | 400 `invalid_url`; 403 `blocked_address`; 422 `unsupported` / `login_required` / `unavailable` / `geo_blocked` / `drm` / `live_unsupported` / `playlist_unsupported`; 429 `busy` / `rate_limited`; 502 `unreachable`; 504 `timeout` |
| `POST /api/jobs` `{url, output: video\|audio, quality: best\|1080\|720\|480}` | 202 `{job_id, status: "queued"}` | 400 (bad body or enum, readable), 403, 429 queue full, 503 disk full |
| `GET /api/jobs/{id}` | 200 `{job_id, status, detail, progress_percent, downloaded_bytes, total_bytes, notice, error: {code,message}\|null, filename}` | 404 |
| `GET /api/jobs/{id}/file` | 200 streamed attachment | 404 unknown, 409 not ready, 410 expired |
| `GET /health` | 200 `{status:"ok"}` | n/a |

- **Error shape:** every error body is `{"error": {"code", "message"}}`. FastAPI validation errors are reformatted to this shape. No stack traces or filesystem paths ever reach the client. Full details are logged to stdout.
- **File delivery:**
  - Streams from disk using `FileResponse`.
  - Sets `Content-Type` to `video/mp4`, `audio/mpeg`, `video/x-matroska` or `video/webm`.
  - Sets `Content-Disposition: attachment` with an ASCII `filename=` fallback plus an RFC 5987 `filename*=`.
  - After a **complete, non-Range** response, the workdir is deleted and the job becomes `expired` (reason "delivered"). An aborted transfer leaves the file in place until its TTL.
- **Download filename:** derived from the title, sanitized as follows:
  - NFKC-normalize the title.
  - Drop control characters and `/\:*?"<>|`.
  - Collapse whitespace.
  - Strip leading and trailing dots and spaces.
  - Avoid Windows reserved names.
  - Truncate to ≤150 UTF-8 bytes.
  - Fall back to `download`.
  - Add the real extension.

  The filename only ever appears in the header, never on disk.

### 6.3 Cleanup
- **Startup:** delete every subdirectory of `TEMP_DIR` whose name matches the job-ID pattern. Refuse to start if `TEMP_DIR` resolves to `/`, the app directory, or a non-dedicated location (for example, it must end in a dedicated folder name or contain a marker file the app created).
- **Every 60 s:**
  - Expire ready jobs past `JOB_TTL_MINUTES`.
  - Delete orphan job directories that are not in the registry.
  - Purge old records.
- **Shutdown** (SIGTERM from `docker compose down`, with `init: true` in compose): kill all worker process groups, delete all workdirs, then exit within the grace period.

## 7. Page (UI)

There is one page: `static/index.html`, plus vanilla `app.js` and Tailwind CSS compiled at image build. The page:
- shows a header: "saveme". The builder may choose the display name.
- has a URL field with a visible label and an **Inspect** button. Pressing Enter inspects.
- **Inspecting:** a spinner and the text "Inspecting…".
- **Result card:**
  - thumbnail, title, duration and source;
  - any notices;
  - a quality `<select>` with only real options;
  - a **Download Video** button;
  - a **Download Audio (MP3)** button, which is disabled with a reason if no audio is available.

  The download buttons appear only after a successful inspect.
- **Job panel:**
  - Shows the status text and a progress bar. The bar is determinate only when the server gives a percent; otherwise it is indeterminate and shows the MB downloaded.
  - Shows the detail line and any notice.
  - **Ready:** a **Save File** link to `/api/jobs/{id}/file` with the `download` attribute. After clicking, the panel says "Saved — file removed from server."
- **Failed:** a readable message and a "Try another link" action.
- **Footer notice:** "Only download media you have the right to save. Works with many sites, not all — compatibility depends on the source."

Behaviour:
- **Polling:** the page polls `GET /api/jobs/{id}` every 1 s while the job is active. The polling stops on a terminal state.
- **Accessibility:**
  - labelled controls;
  - an `aria-live="polite"` status region;
  - visible focus;
  - full keyboard operation;
  - respects `prefers-reduced-motion`;
  - light and dark via `prefers-color-scheme`;
  - works at 360 px width.
- **Visual style:** clean and calm. The builder has latitude on look and feel.

## 8. Configuration (`.env.example`)

| Var | Default | Meaning |
|---|---|---|
| `MAX_ACTIVE_JOBS` | 2 | concurrent downloads |
| `MAX_QUEUED_JOBS` | 4 | waiting jobs beyond active |
| `MAX_ACTIVE_INSPECTIONS` | 4 | concurrent inspections |
| `INSPECT_TIMEOUT_SECONDS` | 30 | per inspection |
| `MAX_FILE_SIZE_MB` | 2048 | per job output/download |
| `MAX_JOB_SECONDS` | 1800 | per job wall clock |
| `JOB_TTL_MINUTES` | 30 | ready-file lifetime |
| `MAX_READY_FILES` | 6 | retained ready files |
| `MAX_TOTAL_DISK_MB` | 10240 | TEMP_DIR budget (see §5.5) |
| `TEMP_DIR` | `/tmp/media-jobs` | job workspace |
| `LOG_LEVEL` | `info` | stdout logging |

Settings are validated at startup (positive ints, sane bounds). *(Builder's call, post-review.)* An invalid config puts the app in **degraded mode** rather than exiting, so `restart: unless-stopped` can't crash-loop:
- the server logs one clear error line naming the bad variable;
- `/health` returns 503 with `config_error`, and the container shows as unhealthy;
- every `/api/*` endpoint returns 503 `config_error`;
- the page shows a banner with the message.

## 9. Docker and dependencies

- **Base image:** `python:3.12-slim` (Debian). The builder picks bookworm or trixie, preferring whichever gives FFmpeg ≥ 6.
- **Installed from apt:** `ffmpeg`. **Deno** is installed as a pinned binary with checksum verification, multi-arch (amd64 and arm64, since the owner is on Apple Silicon).
- **Python dependencies:** pinned exactly, including `fastapi`, `uvicorn`, `yt-dlp[default]==2026.10.26` and `yt-dlp-ejs` per its pin. No curl_cffi. The yt-dlp version is a build `ARG` with the pinned default.
- **README update command:**
  `docker compose build --no-cache --build-arg YTDLP_VERSION=<new> && docker compose up -d`.
  Also mention bumping the pin in the repo.
- **Tailwind CSS:**
  - Compiled in a build stage using the pinned standalone Tailwind CLI, chosen per `TARGETARCH` and checksum-verified.
  - Only `dist/styles.css` is copied into the final image.
  - Local dev (tests) doesn't need the CSS.
- **Runtime hardening:**
  - Runs as a non-root user (uid 10001).
  - `TEMP_DIR` is owned by that user.
  - The app code is read-only for that user.
- **compose.yaml:**
  - `ports: ["127.0.0.1:8000:8000"]`, `init: true`, `stop_grace_period: 20s`, `restart: unless-stopped`
  - `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`
  - memory, CPU and PIDs limits (for example 2 GB, 2 CPUs, 256)
  - `env_file` optional, healthcheck hitting `/health` using Python (there's no curl in slim)
  - logs to stdout and stderr
  - no volumes required
- **Startup:** `docker compose up -d --build` from a clean checkout must work with no `.env` present, using the defaults.

## 10. Seam for Build 2 (do not build now)

Keep extraction behind one function boundary in the worker, e.g. `resolve(url) -> info`. Build 2 will add a Playwright resolver that is tried when yt-dlp returns "unsupported". That resolver captures media requests and runs them under the same IP policy, with a browser-level request interceptor. Don't add Playwright, flags or stubs for it now.

## 11. Testing

`pytest` (with `pytest-asyncio`, or anyio) runs offline by default. `pytest -m network` runs the real-site checks.

### Offline (must pass)
- **URL validation:** malformed, non-http(s) (`file:`, `ftp:`, `javascript:`, `data:`), userinfo, overlong, whitespace or control characters.
- **IP policy:** a table test over IPv4 and IPv6 private, loopback, link-local, CGNAT, metadata, mapped, NAT64, 6to4 and multicast addresses, plus a few public ones.
- **DNS:** mixed public and private answers are rejected. Rebinding: a resolver stub returns a public address at validation and a private one at connect, and the connect-time guard blocks it.
- **Connect guard in a real worker subprocess:** connecting to `127.0.0.1` is blocked with the `blocked_address` code.
- **Redirect:** the policy is injected in-process so that a loopback test server counts as "public". A redirect from it to another non-allowed loopback address/port is blocked. Production entrypoints never accept an injected policy.
- **FFmpeg side door:** the HLS-playlist-disguised-as-media test from §4.4.
- **Media round trip:** FFmpeg generates a 2 s H.264 + AAC MP4 and an MP3, served by a local server under the test policy. The tests run inspect, then a video job, then an audio job, and use ffprobe to verify a valid MP4 with video and audio and a valid MP3 with roughly the right duration.
- **Quality:** an available quality succeeds, and an unavailable quality yields an explicit downgrade notice or rejection.
- **Job lifecycle:** state transitions are checked with a stub runner (success, failure, conversion phase).
- **Concurrency:** with 7 or more submissions, at most 2 are active, 4 are queued, and the next is rejected with 429.
- **Oversize:** both Content-Length and chunked responses. Timeout: a trickling server. In both cases the job fails cleanly and the workdir is gone.
- **TTL and cleanup:** TTL expiry (injectable clock), startup wipe of stale directories, orphan cleanup, ready-file cap.
- **Hostile titles:** a page whose `<title>` is `../../x; $(rm -rf /)\n`, with an `og:video`, produces files only inside the workdir. The sanitized `Content-Disposition` is checked, along with filename sanitizer unit tests.
- **File endpoint:** correct MIME type and disposition; the directory is deleted after a full download; a second GET returns 410; requesting a file that isn't ready returns 409.
- **Image:** `curl_cffi` is not importable in the image. This runs as part of the Docker smoke test.

### Network (opt-in, run before calling it done)
- A public YouTube video, for example `jNQXAC9IVRw`, inspected and then a 480p video and an MP3 downloaded.
- One public Reddit video post.
- One direct public MP4.
- One unsupported URL (for example a Streamtape-style page) returns `unsupported`.
- One private-IP URL returns `blocked_address`.

The builder picks stable URLs and keeps them in one place. The opt-in suite needs the internet, so it is excluded from the default run.

### Docker smoke (script)
`docker compose up -d --build`, then wait for healthy, then:
- `GET /health`
- inspect an invalid URL and expect 400
- inspect `http://169.254.169.254/` and expect 403
- check that the container runs as a non-root user and that `curl_cffi` is absent

Then `docker compose down`.

### Reporting
The final report lists which network checks actually passed. Facebook, Instagram and Twitter/X are reported as "not verified" unless they were actually tested.

## 12. Left to the builder

Already settled by the builder during review. Keep these:
- **Test-only policy injection:** a test-only entry module, never an env var.
- **ffmpeg as a downloader:** if yt-dlp ever falls back to ffmpeg as the downloader, it is wrapped so `-protocol_whitelist file,pipe` is always applied.
- **Thumbnail URLs:** the worker checks the thumbnail URL's host against the IP policy and drops non-public ones.
- **Preselected quality:** the highest H.264-preferring tier, not "Best".
- **"Saved" message:** shown only after the job reports `expired` with reason "delivered".

Still left to the builder:
- Exact yt-dlp format and sort strings that meet §5.3, and the height tolerance for tiers.
- Internal module boundaries, naming, and the async process-management details.
- UI visual design within §7.
- Exact pinned versions (current stable at build time), Debian release, and Tailwind and Deno versions.
- Whether the yt-dlp worker gets its arguments via stdin JSON or argv JSON.
- The error-message wording, as long as it is plain, specific, and free of internals.
