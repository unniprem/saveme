# Business Requirements Document (BRD)

**Project:** Media URL Downloader  
**Version:** 1.0  
**Status:** Ready for implementation  
**Deployment:** Local Docker container  
**Target users:** Individual users downloading media they are authorized to access

## 1. Executive Summary

Build a lightweight, self-hosted web application that accepts a media or webpage URL, identifies downloadable video/audio where supported, and lets the user download the video or extract the audio. The application must run locally using Docker Compose and require no database, account registration, cloud services, or recurring subscription. It should prioritize simplicity, reliable operation, and safe handling of external URLs.

**Important limitation:** The system must not promise compatibility with every website. Media detection and downloading are best-effort for publicly accessible, supported, non-DRM content. Do not implement DRM circumvention, access-control bypass, or unauthorized downloading.

## 2. Business Goals and Success Criteria

### Goals
- Download videos or extract audio through a simple browser UI by pasting a URL.
- Support direct media links and websites compatible with yt-dlp.
- Run entirely inside Docker on macOS, Linux, or Windows with Docker installed.
- Keep dependencies and configuration minimal.
- Never store users, history, or persistent task records.

### Success Criteria
- A user can start the service with `docker compose up -d --build` and open `http://localhost:8000`.
- A supported public test URL can be inspected and its media downloaded successfully.
- MP3 extraction works on a supported sample with an audio stream.
- The UI displays meaningful errors for invalid, blocked, unsupported, or failed URLs.
- Temporary files are deleted after download or a configured expiration.
- No database service, schema, migrations, or persistent user state is needed.

## 3. Scope

### In Scope: MVP (P0)
1. Single-page responsive UI with URL input, **Inspect**, **Download Video**, and **Download Audio** actions.
2. URL validation and safe external request handling.
3. Metadata detection where available: title, duration, thumbnail, source, and formats.
4. Video download in best available compatible format, with MP4 output when conversion/merging is possible.
5. Audio extraction to MP3 with FFmpeg.
6. Simple quality selection based on available formats, e.g. Best / 1080p / 720p / 480p; show only real available choices or explain fallback.
7. Status/progress display: inspecting, queued, downloading, converting, ready, failed.
8. Browser-based file delivery and automatic temporary-file cleanup.
9. Dockerfile, Docker Compose setup, README, environment template, and automated tests.
10. Sensible resource limits and maximum concurrent jobs (default: 2).

### Optional Phase 2 (P1)
- M4A audio output and WebM video output.
- Cancel download from UI.
- Detect multiple media files embedded on a single webpage and allow selection.
- A separate Playwright-based fallback for JavaScript-rendered public pages where it is lawful and technically feasible.
- Optional host-mounted output folder in addition to browser downloads.

### Out of Scope
- Database, accounts, login, billing, analytics, or download history.
- Batch/playlist downloads in MVP.
- Scheduled or recurring downloads.
- Cloud hosting or distributed workers.
- DRM decryption, bypass of paywalls, access controls, authentication, or CAPTCHAs.
- Claims that arbitrary websites or protected media will always work.

## 4. User Journey

1. User opens `http://localhost:8000`.
2. User pastes a URL and selects **Inspect**.
3. Application validates the URL and performs a best-effort extraction/metadata lookup.
4. On success, the UI shows metadata and supported output formats/qualities.
5. User selects **Video** or **Audio (MP3)**, plus quality if applicable.
6. User initiates download. UI displays server-reported progress/status.
7. Server downloads/converts media to a uniquely named temporary workspace.
8. When ready, user clicks **Save File** and browser downloads the file.
9. Server deletes files after completed delivery or after a short configurable TTL, including failures and canceled tasks.
10. If extraction fails, the UI explains the reason without revealing internal stack traces.

## 5. Functional Requirements

| ID | Priority | Requirement | Acceptance Criteria |
|---|---|---|---|
| FR-01 | P0 | URL input and validation | Only valid `http`/`https` URLs accepted; malformed URL gets readable feedback. |
| FR-02 | P0 | Inspect URL | For supported media, show metadata and available output options; do not download entire content just to inspect. |
| FR-03 | P0 | Video download | Download supported video and audio tracks; merge to a playable output where applicable. |
| FR-04 | P0 | Audio extraction | Produce a playable MP3 from supported source media using FFmpeg. |
| FR-05 | P0 | Quality selection | Available choices are grounded in discovered media; unsupported selections are rejected or explicitly downgraded. |
| FR-06 | P0 | Progress and status | UI shows accurate states and available progress, with no fabricated percentages. |
| FR-07 | P0 | File delivery | Browser receives the completed file with a safe, sanitized filename and correct MIME type. |
| FR-08 | P0 | Error handling | Unsupported, unreachable, unavailable, blocked, or failed media returns a readable reason. |
| FR-09 | P0 | Temp cleanup | Temporary media removed after delivery/expiration; cleanup handles crash recovery on restart. |
| FR-10 | P0 | Concurrency controls | Maximum 2 active extraction jobs by default; excess work rejected or placed in bounded in-memory queue. |
| FR-11 | P0 | Docker deployment | One application container; no database, Redis or other mandatory containers. |
| FR-12 | P1 | Browser fallback | Optional Playwright worker attempts permitted media discovery on supported public pages. |
| FR-13 | P1 | Multiple media selection | When multiple accessible media URLs are found, user can choose which item to process. |

## 6. Technical Architecture

### Technology Stack
- **Frontend:** HTML + Tailwind CSS + vanilla JavaScript (served by FastAPI).
- **Backend:** Python 3.12+ with FastAPI and Uvicorn.
- **Media extractor:** yt-dlp, invoked through a controlled subprocess or Python API.
- **Conversion / merging:** FFmpeg and ffprobe.
- **Optional fallback:** Playwright in a later phase; not a required MVP dependency.
- **Deployment:** Docker and Docker Compose.
- **State:** In-memory task registry only; no persistent database.
- **Files:** Dedicated temporary job directories, not a permanent download archive.

### Logical Flow
```text
Browser (URL + output options)
  -> FastAPI (validation and job management)
  -> yt-dlp (metadata and extraction)
  -> FFmpeg / ffprobe (conversion, merge, validation)
  -> temporary job directory
  -> browser file download
  -> cleanup
```

### API Contract (suggested; implementation may refine)
- `POST /api/inspect` body: `{ "url": "https://..." }` -> metadata, available format options, limitations.
- `POST /api/jobs` body: `{ "url": "https://...", "output": "video|audio", "quality": "best|1080|720|480" }` -> `{ "job_id": "...", "status": "queued" }`.
- `GET /api/jobs/{job_id}` -> `{ "status": "queued|downloading|converting|ready|failed|expired", "progress_percent": number|null, "error": string|null }`.
- `GET /api/jobs/{job_id}/file` -> attachment response when ready; 404/410 for missing/expired files.
- `GET /health` -> simple health check.

Job identifiers must be cryptographically unpredictable, and job state must not expose filesystem paths. Use polling for the MVP; WebSockets/SSE are optional. Jobs may disappear after container restart by design.

## 7. Nonfunctional Requirements

### Security (Mandatory)
- Treat pasted URLs as untrusted. Prevent SSRF: reject private, loopback, link-local, reserved, multicast, and cloud metadata IPs; validate DNS answers and redirect targets; address DNS rebinding; apply equivalent checks to all extraction/network paths including yt-dlp and optional browser fallback.
- Bind Docker port to `127.0.0.1` by default; the service is local-only and unauthenticated. Never expose it directly to the public internet without a separate authentication and security design.
- Only allow HTTP/HTTPS source URLs; prohibit `file:`, local paths, and arbitrary yt-dlp arguments passed from clients.
- Do not use `shell=True`; supply subprocess arguments as a list and constrain resources and runtime.
- Sanitize filenames and do not let media metadata control output directories or paths.
- Limit download size, elapsed time, disk usage, concurrent tasks, and metadata inspection duration.
- Do not persist cookies, passwords, headers, or private session credentials. No authenticated downloading in MVP.
- Container runs as a non-root user where possible, with minimal filesystem permissions.

### Reliability
- Show clear errors; avoid crash loops for unsupported URLs.
- Failed conversions and interrupted downloads must not leave orphaned large files.
- Clean stale temporary job directories at startup and periodically while running.
- Support graceful termination on `docker compose down`.

### Performance
- Limit to 2 active downloads by default; allow configuration by environment variable.
- Avoid holding entire media files in application memory.
- Stream file delivery from disk and limit the number of ready files retained.
- Do not rely on fixed extraction success rates or promise downloads from all websites.

### UX
- Mobile-responsive page, accessible labels and keyboard operation.
- Clearly label output format and available quality; don't invent options.
- Communicate that only content users have the right to download is supported.

## 8. Docker Requirements

- Provide `Dockerfile`, `compose.yaml`, `.dockerignore`, `.env.example`, `README.md`.
- Container includes Python, yt-dlp, FFmpeg/ffprobe and application assets.
- Expose service on `127.0.0.1:8000:8000` by default.
- Use an internal writable temporary directory (`/tmp/media-jobs` or equivalent).
- **No database volume required.** Optional bind-mounted output directory only if explicitly configured.
- Configure environment variables such as `MAX_ACTIVE_JOBS=2`, `MAX_FILE_SIZE_MB`, `JOB_TTL_MINUTES`, `MAX_JOB_SECONDS`, `TEMP_DIR`.
- Docker image should use pinned, maintainable dependency versions, with a documented update command for yt-dlp.
- Include a health check and container log output to stdout/stderr.

## 9. Supported Sources and Constraints

- **Expected supported categories:** direct downloadable MP4/MP3/WebM links; many public sites supported by yt-dlp; some publicly accessible embedded players.
- **Best effort only:** dynamic sites, short-lived signed URLs, authentication-gated content, geo-restricted resources, site-rate limits, and changed provider APIs may fail.
- **Unsupported by design:** DRM-protected media and anything requiring circumvention of authentication, paywalls, CAPTCHAs, or access controls.
- The UI must clearly state that compatibility is source-dependent and the user must have permission to download the content.

## 10. Testing and Acceptance

### Required Automated Tests
- Invalid/malformed URL rejected.
- Localhost, private addresses, cloud metadata addresses and malicious redirects rejected; verify DNS/rebinding protections.
- Supported public test media can be inspected and downloaded.
- MP3 extraction produces valid audio output.
- Requested quality is available or receives explicit fallback/error.
- Job lifecycle transitions are correct.
- Two concurrent active jobs are handled without exceeding the configured limit.
- Oversize and timeout scenarios stop safely and clean up files.
- Abandoned or expired jobs get cleaned up.
- Hostile media titles cannot escape temp directory or inject commands.
- Browser can download ready files with proper content type and filename.
- Container starts from a clean checkout with `docker compose up -d --build`.

### Definition of Done
- All P0 functional requirements implemented and tested.
- Documentation includes one-command setup, usage, supported limitations, environment variables, troubleshooting, and safety notes.
- Test on at least: one direct video URL, one supported public website URL, one invalid URL, one unsupported URL, and one blocked private IP.
- No database or external hosted service used.

## 11. Suggested Delivery Milestones

1. **Foundation:** Docker image, Compose, FastAPI app, static UI, health endpoint.
2. **Inspection:** URL validation, SSRF protections, yt-dlp metadata extraction.
3. **Downloads:** Video and MP3 flows, FFmpeg conversion, safe temporary storage.
4. **Experience:** Quality options, accurate status, file delivery, errors and cleanup.
5. **Hardening:** Tests, concurrency limits, size/time limits, security review and README.
6. **Optional Phase 2:** Playwright fallback and multiple embedded-media selection.

## 12. Agent Implementation Instructions

Implement the P0 requirements as a working, clean, locally runnable project. Prioritize a small codebase and reliable UX over unnecessary abstractions. Do not introduce PostgreSQL, Redis, Celery, React, cloud services, logins, or telemetry. Use plain HTML/CSS/JavaScript, FastAPI, yt-dlp and FFmpeg. Playwright is explicitly deferred unless MVP coverage proves insufficient. Provide the complete file tree, source code, Docker configuration, tests, README, and example `.env` values. Run tests and report any unsupported or unverified capabilities accurately. Never claim the downloader works on every website or circumvent protected streams.
