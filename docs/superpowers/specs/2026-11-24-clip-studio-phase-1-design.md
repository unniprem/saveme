# Clip Studio, Phase 1 (manual clipping): Design

**Date:** 2026-11-24
**Builds on:** [`2026-11-09-media-url-downloader-design.md`](2026-11-09-media-url-downloader-design.md) and [`2026-11-23-file-tags-and-m4a-design.md`](2026-11-23-file-tags-and-m4a-design.md). This document records only what changes.
**Size:** a project (full design).

## 1. Intent

The owner wants SaveMe to grow toward what [AutoClip](https://github.com/zhouxiaoka/autoclip) does: a long video goes in and short clips come out. It should stay built SaveMe's way: local, private, vanilla JavaScript, no database and no paid services. AutoClip's AI steps (transcripts, highlight picking, vertical reframing, burned-in subtitles, covers and captions) are later phases. Its stack (React, Redis, Tauri, cloud models) is ruled out by the owner's constraints, so SaveMe takes ideas from it, not code.

Phase 1 is the manual foundation the later phases build on. You load a video from a link or from your computer, watch it, mark a start and an end, and export that part as a file. Exporting runs as a server job you can follow and cancel, and nothing stays on the server longer than it's needed.

## 2. Goals

- **Two ways in.** Open the studio from a fetched link (the source is downloaded at the chosen video quality) or from a video file on the owner's computer (uploaded to the server).
- **Preview.** The video plays in the page with normal controls and seeking.
- **Start and end.** Set them from the playhead (buttons and the `I`/`O` keys), by typing a time, or by dragging handles on a timeline. "Play clip" plays just the selection.
- **Server-side cut.** FFmpeg cuts the selection by stream copy, with no re-encoding.
- **Export as a job.** Queued with the downloads, with live progress, cancellable, and saved to the device automatically like a download. Several clips can be exported from one source, one after another.
- **Cleanup.** Sources and clips are deleted when they're done with, when unused for a while, on a cap, and on restart. Nothing is kept across restarts.

## 3. Non-goals (later phases)

- Re-encoding: frame-exact cuts in every player, vertical (9:16) reframing, burned-in subtitles, resizing. These need an H.264 encoder in the FFmpeg build (§7).
- Transcripts, AI highlight suggestions, titles, covers and captions.
- Joining several ranges into one file, or exporting several clips at once (one export runs at a time per open studio; separate studios can run in parallel through the queue). *Update:* a clip list exported as one ZIP was added later in Phase 1. See [`2026-11-24-clip-studio-batch-export-design.md`](2026-11-24-clip-studio-batch-export-design.md).
- Audio-only clips (MP3/M4A), and a thumbnail filmstrip on the timeline (that needs a server-side video decoder).
- Keeping a source after a restart, or sharing it between browsers.

## 4. Anti-goals: a failure even if it works

- **Weakening a protection.** The URL checks, the connection guard, the FFmpeg protocol whitelist, the 64 KiB body cap for every other POST, the Origin and Host checks, the worker memory cap, the queue and the disk budget all stay as they are. An upload gets limits of its own (§6.3), not an exemption.
- **FFmpeg or ffprobe outside a worker.** Probing and cutting run in the worker subprocess, with its memory cap, timeout, process-group kill and workdir size watchdog, like downloads.
- **A file stuck on the server.** Every source and clip has an owner who ends it (close, save, cancel) and a timeout that ends it if nobody does.
- **Leaking private details from the owner's own file.** A clip cut from an upload carries no metadata from it (phones write location and device into videos). *(Designer's call.)*
- **Breaking the downloader.** Existing routes, request shapes, error codes and stored jobs keep working. New view fields are additions.

## 5. Approaches

### 5.1 How the cut is made (designer's call, shown to the owner before the design)

| | Approach | Trade-off |
|---|---|---|
| **A** | **Stream copy** (`-ss` before `-i`, `-c copy`) | No change to the FFmpeg build, fast, keeps the original quality. FFmpeg 8 and 9 keep the packets back to the previous keyframe and hide them with an MP4 edit list, so browsers, QuickTime and VLC start exactly at the chosen time (checked on a 20 s test video with keyframes every 5 s: cut at 7.0 s, first shown frame at 0.000, 127 shown frames for 5 s). Players that ignore edit lists show the hidden lead-in, at most one keyframe interval. |
| B | Re-encode with libx264 | Frame-exact everywhere, but needs H.264 and other video decoders, an encoder and swscale in the build: more attack surface for hostile media, a bigger image, GPL licensing for the binary, and slow CPU encoding under the container's CPU limit. |
| C | Smart cut (re-encode only up to the first keyframe) | Exact and mostly fast, but needs everything B needs, plus fiddly splicing. |

**Chosen: A.** It needs nothing new in the build. B's encoder belongs to the phase that needs it for reframing and subtitles, and that phase can bring frame-exact cuts with it.

### 5.2 Where the source comes from (owner's choice)

Links only, or links plus files from the owner's computer. **The owner chose both.**

### 5.3 How the studio fits the code (designer's call)

| | Approach | Trade-off |
|---|---|---|
| **A** | **Sources and clips are new kinds of job in the existing `JobManager`** | One owner for the queue, slots, disk budget, sweep and shutdown, so every limit applies to clips without being copied. `jobs.py` grows. |
| B | A separate `StudioManager` next to `JobManager` | Smaller files, but two managers would have to share the slots and the disk budget, which is where the hardest bugs in this code have been. |
| C | Cut in the browser (ffmpeg.wasm) | No server work, but a ~30 MB WebAssembly download, a CSP change (`wasm-unsafe-eval`), and it can't grow into the server-side AI phases. |

**Chosen: A.**

## 6. Design

### 6.1 Concepts

- **Source:** a video kept on the server while a studio is open. It is a `Job` with `kind="source"`, made by a download (`origin="link"`) or an upload (`origin="upload"`). It is never "delivered". The page streams it for preview with Range requests.
- **Clip:** a `Job` with `kind="clip"` that cuts a range out of a ready source into its own folder. When it's ready it behaves exactly like a finished download: one-shot delivery, then deleted; TTL; ready-file cap.
- Existing jobs are `kind="download"`. Each kind keeps its own ID space in the API: `/api/jobs/{id}` refuses a source ID (`job_not_found`), and `/api/sources/{id}` refuses a job ID (`source_not_found`).

### 6.2 Lifecycles

```text
source (link):   queued → downloading ⇄ converting → ready → expired (closed | idle | replaced)
source (upload): uploading → queued → checking → ready → expired (closed | idle | replaced)
                 any pending state → failed, or → expired (closed) on close
clip:            queued → cutting → ready → expired (delivered | ttl | evicted)
                 queued/cutting → failed | cancelled   (no resume: the folder is deleted)
```

- A link source runs the existing download worker request (`output: "video"`, the chosen quality) and takes a slot like a download. Its download result now includes `duration` (from ffprobe of the final file).
- An upload source is written to `TEMP_DIR/<id>/upload.bin` by the request handler while the status is `uploading` (internal; the client has no ID until the upload ends). Then a `probe` worker run (status `checking`, in a slot) checks it and makes it `media.<ext>` (§6.5).
- **Idle TTL.** A ready source expires (`idle`) `JOB_TTL_MINUTES` after its last use. Use means a status poll, a media request, a clip submit, or the end of one of its clips. A source with a pending clip never expires idle. The page polls the source every 60 s while the tab is visible, so an open studio stays alive and a forgotten hidden tab lets go.
- **Cap.** At most `MAX_CLIP_SOURCES` (new setting, default 2, range 1-20) sources are open (not ended). Opening one more first closes the least recently used ready source with no pending clip (`replaced`). If none can be closed, the request gets `busy`.
- **Close.** `POST /api/sources/{id}/close` ends the source in any state. It cancels its pending clips and any running download or check, and deletes its folder; an upload in progress stops at its next chunk. It answers with the source view, and a repeat close is harmless.
- **Clip cancel.** The existing cancel route. Clips get no resume window: the run is killed and the folder deleted. The existing `resume` route answers `not_resumable`.
- Sources are not counted against `MAX_READY_FILES` and are never evicted to make disk room. They are freed only by close, idle or the cap, so a studio isn't pulled out from under the owner mid-edit. A job that doesn't fit gets `disk_full` as today. *(Designer's call.)*

### 6.3 API (all new routes under `/api`, behind the existing Guard)

| Route | Body | Answer |
|---|---|---|
| `GET /api/studio` | | `{"max_upload_bytes", "idle_minutes", "min_clip_seconds"}`, so the page can refuse an oversized file before sending it |
| `POST /api/sources` | `{"url", "quality"}` (same checks as `POST /api/jobs`: syntax, queue room, DNS inside a held place) | 202 `{"source_id", "status"}` |
| `POST /api/sources/upload` | the raw file (§ below) | 202 `{"source_id", "status"}` once the whole file has arrived |
| `GET /api/sources/{id}` | | source view |
| `GET /api/sources/{id}/media` | | the source file, `Range` supported (206), `Cache-Control: no-store`; 409 `source_not_ready` before it's ready |
| `POST /api/sources/{id}/clips` | `{"start", "end"}` in seconds | 202 `{"job_id", "status"}`; then the existing `/api/jobs/{id}`, `/cancel` and `/file` routes |
| `POST /api/sources/{id}/close` | `{}` | source view |

Close is a POST, not a DELETE, because the Guard's Origin check covers POST.

**Source view:** `source_id`, `status`, `origin` (`link`/`upload`), `title`, `duration_seconds`, `detail`, `progress_percent`, `downloaded_bytes`, `total_bytes`, `notice`, `error`, `reason`.
**Job view:** gains `kind` (`download`/`clip`). Nothing else changes.

**Upload request.**
- `POST /api/sources/upload` with `Content-Type: application/octet-stream`, a `Content-Length`, and an optional `X-SaveMe-Filename` (the file name, percent-encoded UTF-8, at most 1,024 characters). Raw bytes, not multipart, so no form parser is added and nothing else is buffered.
- The Guard skips its 64 KiB body read for this one path and method. The Host check, the Origin check and degraded mode still apply.
- The handler checks, in order:
  - the content type (otherwise 415 `invalid_request`), which also makes a cross-site request need a CORS preflight that the app never grants;
  - `Content-Length` (missing or chunked: 411 `invalid_request`; zero: 400; over `MAX_FILE_SIZE_MB`: 413 `too_large`);
  - then queue room, the source cap and disk room, counting active jobs.
- It then streams the body to disk in 1 MiB writes, off the event loop. It stops with `timeout` if nothing arrives for 60 s or the upload passes `MAX_JOB_SECONDS`, with `upload_incomplete` on a short body or a hang-up, and with `invalid_request` on bytes past `Content-Length`. On every failure the folder and record are removed.
- The title is the file name without its extension, or "Uploaded video". It is only ever shown as text and passed through `sanitize_filename`.

**Clip request.** `start` and `end` are JSON numbers (strict: no strings or booleans, finite, `start ≥ 0`). Rules:
- `end - start ≥ 0.1` s (`MIN_CLIP_SECONDS`).
- When the duration is known: `start < duration`, and `end` is at most `duration + 0.05` (then clamped to the duration).
- Values are rounded to milliseconds.
- Errors are 400 `invalid_clip` with a message that names the rule. A source that isn't ready gets 409 `source_not_ready`.

**New error codes:** `source_not_found` (404), `source_not_ready` (409), `invalid_clip` (400), `unreadable_media` (422, "SaveMe can't read this file as a video. Try an MP4, MOV, MKV or WebM file."), `upload_incomplete` (400), `clip_failed` (422).

### 6.4 Resources and limits

- Sources and clips share the downloads' `MAX_ACTIVE_JOBS` slots and `MAX_QUEUED_JOBS` queue. An upload holds a queue place while it transfers but no slot; its check takes a slot.
- **Disk.** Every new job reserves the existing `2 × MAX_FILE_SIZE_MB`. That covers an upload plus its MP4 remux copy, and a clip, which is at most about its source's size. Ready sources count as used space. An upload is admitted only if it fits while counting active jobs, because its bytes land before any slot is taken.
- **Workers.** `probe` and `clip` run as new worker modes. They get the same memory cap, `MAX_JOB_SECONDS` timeout, process-group kill and workdir watchdog. They never touch the network, and the guard is installed anyway. FFmpeg also gets `-fs MAX_FILE_SIZE_MB`; a clip that reaches it fails with `too_large`.

### 6.5 Worker modes

Local-media helpers move to a new module, `app/mediafile.py`:
- `probe()` (ffprobe JSON);
- `remux_to_mp4()` (today's `_to_mp4`);
- the container-to-extension mapping;
- `cut()`.

`app/worker.py` dispatches `inspect`, `download`, `probe` and `clip`. Only `inspect` and `download` need a URL.

- **`probe`** (`{"mode": "probe", "source", "workdir"}`):
  1. ffprobe the upload.
  2. It must have a non-cover video stream (else `no_video`), and a duration over 0 that ffprobe can read (else `unreadable_media`).
  3. Map the container: mov/mp4 family → `mp4`; matroska/webm → `webm` if every stream is VP8/VP9/AV1/Opus/Vorbis, else `mkv`; `mpegts` → `ts`; `flv` → `flv`; `avi` → `avi`. Anything else is `unreadable_media`.
  4. Rename the upload to `media.<ext>`. For anything but `mp4`, remux to MP4 (better preview, especially in Safari). If that fails, keep `mkv`/`webm`; any other container is `unreadable_media`.
  5. Return `{"file", "ext", "duration", "notice"}`.

  The build's demuxers (mov, matroska, mpegts, flv, avi, ogg, …) bound what can be opened.
- **`clip`** (`{"mode": "clip", "source", "workdir", "start", "end", "keep_tags", "max_bytes"}`):
  1. Run `ffmpeg -ss S -i SRC -t D -map 0:V:0 -map 0:a:0? -c copy -map_chapters -1 [-map_metadata -1] [-movflags +faststart] -fs MAX -progress pipe:1 -nostats OUT`. `0:V` excludes cover art; chapters would carry wrong times.
  2. Emit `progress` events: percent is `out_time_us / D`, and bytes are `total_size`, with the detail "Cutting the clip".
  3. Check the output: it must have a video stream and a duration over 0 (else `clip_failed`), and it must be below the size limit (else `too_large`).
  4. Return `{"file", "ext"}`.

  `OUT` is `media.<source ext>`. Tags are kept for link sources (title, artist, date) and dropped for uploads (§4).

`JobManager` builds the clip's filename: `"<title> (clip 1m05s-1m40.5s).<ext>"` through `sanitize_filename`. Times are formatted by the server from the validated numbers.

### 6.6 Page

- **Scripts.** Shared helpers move from `app.js` to `static/common.js` (`window.SaveMe`: `api`, `ApiError`, formatting, `setText`/`show`/`setLine`). The studio is `static/studio.js` (`window.SaveMeStudio`). All three are `defer`, in the order common, studio, app. No new libraries, and still no `innerHTML`.
- **Ways in.**
  - The result card gets a **Clip** button next to Download. It is enabled when Download is and a video quality is chosen, and it opens the studio with that link and quality.
  - Under the link field, a "Clip a video from your computer" control (a styled label for a hidden file input, `accept="video/*"` plus the usual extensions) opens the studio with the picked file.
- **Page state while the studio is open.** The link form, Clip and the file control are disabled, and "How it works", the result card and the job panel are hidden. The status line says to close the studio to fetch another link. Closing returns to the result card if there was one, otherwise to the empty page. *(Designer's call: no hidden loss of the studio's work.)*
- **Studio card:**
  - **Title row:** the source's title and **Close**.
  - **Loading area:** "Uploading" (XHR upload progress, since fetch can't report it), "Downloading" or "Converting" (polled), then "Checking the video", with a progress bar, a percent when known, and **Cancel**.
  - **Editor:**
    - `<video controls preload="metadata" playsinline>` with `src` set to the media route.
    - A timeline: a track you click to seek, the selected range, the playhead, and two slider handles (`role="slider"`). The handles move by pointer, or by keyboard: arrows ±0.1 s, Shift ±1 s, PageUp/PageDown ±10 s, Home/End.
    - Start and End text fields that accept `ss(.s)`, `m:ss(.s)` or `h:mm:ss(.s)`, each with a "Set to playhead" button.
    - The clip length.
    - **Play clip** (plays from the start and pauses at the end).
    - **Export clip**.
  - **Export area:** headline, percent, bar, "MB written", a "Save clip" fallback link, error text, and **Cancel**. A ready clip is saved automatically once, then the page watches for "delivered", as `app.js` does.
- **Defaults.** Start at 0 and end at `min(duration, 60 s)`. *(Designer's call: a common short length.)*
- **Preview fallback.** If the browser can't play the source (some MKV or HEVC files), the editor says so and still works with typed times and the timeline, using the server's duration.
- **Reload.** `sessionStorage` (`saveme.studio`) keeps the source ID, title, start, end and the latest export's ID, so a reload comes back to the studio. A gone source clears it and says so. An upload in progress can't survive a reload.
- **Accessibility.** Live announcements go through the existing `#announcer`. Keyboard shortcuts are `I`/`O` when focus isn't in a text field. Touch targets are at least 40 px.
- **Copy.**
  - A line under the editor: "Clips are cut without re-encoding, so they keep the original quality. A few players may show up to a few seconds before your start."
  - After a save: "Clip saved. Mark another part, or close the studio."

### 6.7 Docs and checks

- README: Features, Usage (Clip Studio), Configuration (`MAX_CLIP_SOURCES`), Security (uploads, cutting), How it works.
- `.env.example`: the new setting.
- `scripts/docker-ffmpeg-check.sh`: a stream-copy cut and a TS-to-MP4 cut in the matrix. The integration tests it runs inside the image cover `probe` and `clip` against the minimal FFmpeg.

## 7. Open for later phases

- An H.264 encoder (libx264 or OpenH264) in the build would unlock frame-exact cuts, 9:16 reframing and burned-in subtitles. It needs its own security and licensing review.
- Speech-to-text with a local model, AI highlight suggestions with a local LLM, and audio-only clips. (The clip list is done: see the batch export design.)

## 8. Left to the builder

The exact Tailwind classes and wording beyond the copy above, the module boundaries inside `app/jobs.py`, and test names.
