# Journal

---

## 2026-11-10 17:21 IST — Session: README for the public release

### Summary

The owner plans to make the repository public and asked for a professional README that explains why the app is needed and covers installation and removal. The README was rewritten on `feature/security-hardening` (it describes behaviour from this branch, such as one download per file at a time). It adds a "Why SaveMe" section comparing online downloader sites and the yt-dlp CLI, a features list, step-by-step install, update and uninstall sections, a supported-sites table, a security section with a vulnerability-reporting route, a native development run, project layout, responsible-use guidance and contributing notes. The existing settings, troubleshooting and safety content was kept and reorganised.

**Verified:**
- A native run (`.venv/bin/python -m app`) answered `/health` with 200 and served the page on `127.0.0.1:8000`; `docker compose config` is valid.
- Every in-page anchor and local link in the README resolves.
- Claims were checked against the code: startup cleanup of leftover job folders, MP3 VBR quality 2, the default quality tier, and what the build verifies by checksum or digest. The draft said base images were pinned; they aren't (only FFmpeg, Deno and the Tailwind CLI are), so that line was corrected.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `01c7406` | docs | Rewrite README for the public release |
| `603fe8e` | chore | Check out text files with LF line endings on every platform (`.gitattributes`) |

The owner then asked for the `.gitattributes` and a repository description. The description is now set on GitHub: "A private, self-hosted media downloader. Paste a link and save it as MP4 or MP3. Runs locally in Docker, built on yt-dlp and FFmpeg." A clone with `core.autocrlf=true` had 89 CR bytes in `scripts/build-css.sh` at `3b02d7b`. At `603fe8e` that file and both `app/bin` wrappers had none.

### Key Decisions

- **"Public" means the repository, not hosting.** The app still has no login (#2), so the README keeps a prominent warning never to expose it to a network.
- **Uninstall uses `docker compose down --rmi all`.** The compose file sets `image: saveme:local`, a custom tag, so `--rmi local` would leave the image behind.
- **Native run documented under Development only.** It skips the container hardening and uses the system FFmpeg, so Docker stays the recommended install.
- **`* text=auto eol=lf` for the whole repo, not just `*.sh`.** The `app/bin` wrappers have no extension, and every tracked file was already LF in the index, so the blanket rule changed no file contents. The README's Windows note dropped its `core.autocrlf=false` clone flag; Windows is still marked untested.

### Issues Encountered

- Other uncommitted work on this branch (`app/`, `tests/`) was left untouched; only `README.md` was committed.
- Follow-ups before going public: enable private vulnerability reporting (the README points to it), and consider merging or closing the security fixes, since issues #1–#8 become public with the repo.

---

## 2026-11-10 16:50 IST — Session: Security review follow-up

### Summary

The owner asked me to go through the security review in `/tmp/saveme-security-review.md`, then to file every finding as a GitHub issue and fix the two quick ones. Each finding was checked against the code at `bd527ec` and all seven held up. They are filed as issues #1–#8 on `unniprem/saveme` under a new `security` label; the review's finding 1 is split into #1 (native bind) and #2 (no authentication). Fixed on `feature/security-quick-fixes` (not merged or pushed):
- **#7:** `.gitignore` now covers `.superpowers/` (it held a local session token one `git add -A` away from a commit), `.env.*` (with `.env.example` kept), `.playwright-mcp/` and `.worktrees/`.
- **#1:** `python -m app` listens on `127.0.0.1` by default; `--host` sets another address, and the Dockerfile passes `--host 0.0.0.0`. The README safety note says so.

**Verified:**
- 375 offline tests pass. The new entrypoint and Dockerfile `CMD` tests failed first, for the missing feature.
- `git check-ignore` now matches all five paths and not `.env.example`; `git status` is clean.
- A native run listened on `127.0.0.1:8000` only: `/health` answered on loopback, and the LAN address refused the connection.
- An image built from the branch under a throwaway tag became healthy and answered `/health` through a `127.0.0.1:18000` mapping, with uvicorn on `0.0.0.0` inside. The tag and container were removed; the running `saveme-saveme-1` was left alone.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `a69084f` | chore | Ignore local tool state and `.env` variants (closes #7) |
| `6fdcfee` | fix | Listen on loopback by default when run natively (closes #1) |

### Key Decisions

- **A `--host` flag, not an environment variable.** Compose loads `.env` into the container, so a `.env` copied from `.env.example` carrying a loopback default would override the container's bind and make it unreachable.
- **Finding 1 split into two issues.** The bind is fixed now; authentication and job ownership stay open until public hosting is planned, and are tracked separately so closing #1 doesn't hide them.
- **Gitignore change verified with `git check-ignore`, not a test.** It is configuration; the before/after check shows what it ignores.

### Issues Encountered

- The review's scan left a repo copy in `/tmp/saveme-secret-scan-1nhdr259/`, including `.env` and the session token. Its folder is owner-only, but it should be deleted once the review is no longer needed.
- The running container still uses the old image. It behaves the same (it bound `0.0.0.0` inside before too), so it wasn't rebuilt.

---

## 2026-11-10 14:53 IST — Session: Cancel and Resume

### Summary

The owner reported a download that looked stuck on "Downloading video track (1 of 2)" and asked for a Cancel button, since there was no way to stop a job. They then asked that a cancelled download stay on the server for 5 minutes so it can be resumed. The download wasn't stuck: it was a 312 MB 1080p video track moving at about 0.5 MB/s, and it finished and saved itself at 09:01 UTC. Built on `feature/cancel-download`, merged into `main` (local, not pushed), and the container was recreated from `main`:
- **Server:** `POST /api/jobs/{id}/cancel` kills the worker and keeps the partial files for 5 minutes. `POST /api/jobs/{id}/resume` runs the job again in the same folder, where yt-dlp continues the `.part` file. The job view gains `resume_seconds`, and the sweep deletes the partial files when the window ends.
- **Page:** a Cancel button while a job is queued or running. After a cancel, a Resume download button and a "Kept on the server for 4:59" countdown, which still work after a reload. When the window ends, the page says the partial download was removed.
- **README:** Cancel and Resume, plus a troubleshooting note explaining "video track (1 of 2)".

**Verified:**
- 372 offline tests pass, on the branch and again on the merged `main`. New tests cover cancel, resume, the window ending, queue limits on resume, a resumed run waiting for the cancelled worker to die, shutdown during a cancel, and a real worker resuming a partial file with a `Range` request.
- In Chromium against the branch app and the local media server: Cancel, Cancelled with countdown, reload, Resume, then automatic save. The saved file was byte-identical to the source. The window running out (shortened to 6 s) was checked too, on phone width in dark mode. No console errors.
- On the recreated container: a real YouTube job (`C0DPdy98e4c`) was cancelled at 63 KB, resumed from 64 KB rather than 0, and saved as an MP4 with AV1 video and Opus audio.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `82b2bd7` | feat | Cancel a job and resume it within 5 minutes (API, job manager, runner and worker fixes) |
| `311f107` | feat | Add Cancel and Resume buttons to the download panel |
| `c325e04` | docs | Describe Cancel and Resume, and the two-track download step |
| `2e9acd7` | merge | Merge `feature/cancel-download` into `main` |

### Key Decisions

- **POST `/cancel` and `/resume`, not DELETE.** The Guard's Origin check only covers POST, and the tests already pin `DELETE /api/jobs/{id}` as 405.
- **A new terminal status, `cancelled`.** "failed" would show an error, and "expired" means the file is gone. `resume_seconds` tells the page whether Resume is still possible.
- **Resume reuses the job folder instead of starting a new job.** yt-dlp's `continuedl` (on by default) continues a `.part` file with a `Range` request and reuses tracks that already finished. The worker has no SIGTERM handler, so the kill leaves those files in place. A resume costs one re-extract, not a full re-download.
- **A resumed run waits for the cancelled run to finish.** Otherwise two workers could write the same `.part` file during the 5 s SIGTERM grace. It waits with `asyncio.wait`, not `gather`, so cancelling the new run never cancels the old one a second time.
- **Shutdown skips runs a user cancel already stopped (`Task.cancelling()`).** A second `cancel()` cut run_worker's kill short before its SIGKILL. A test with a worker that ignores SIGTERM failed without this.
- **The window is a constant (`RESUME_WINDOW_SECONDS = 300`), not an environment variable.** The owner asked for 5 minutes; a setting can come later if they want one.
- **Resume goes through the same busy and disk checks as a new job.** A resumed download takes a queue slot like any other.
- **Cancelling a ready job discards the file and can't be resumed.** The page only offers Cancel while a job is queued or running, so this happens only when the job finishes just as Cancel is clicked.

### Issues Encountered

- **The original "stuck" report** was a large video track on a slow source, not a hang. The UI showed the MB count moving, but nothing explained why there were two tracks, so the README now does.
- **`docker compose up -d --build` rebuilt the image but left the old container running.** It needed `--force-recreate`.
- **A killed worker's `.part` file is smaller than its last progress event**, because buffered writes are lost. This is harmless: the resume asks for the file's actual size.
- **The previous session's deferred minor is fixed:** "A cancel during the runner's 1 s stderr drain skips task cleanup." User cancels made it reachable.
- **A track kept from a cancelled run was invisible to the size cap.** yt-dlp reports it only as `total_bytes`, so the progress hook now counts that.
- **Known limitation:** a cancel during post-processing of a single-file download (not two tracks) re-downloads that file on resume. yt-dlp runs with `overwrites: True` and deletes the finished file. Two-track downloads keep their finished tracks.
- **Resumable partial files are not evicted under disk pressure.** They count toward `MAX_TOTAL_DISK_MB` until their window ends (at most about 6 minutes, with the 60 s sweep).

---

## 2026-11-10 14:08 IST — Session: SaveMe Brand Redesign

### Summary

The owner shared a SaveMe logo sheet and asked for the page to be redesigned around it, keeping it simple and effective: the old page "doesn't look professional". From browser mockups they chose a purple header band (direction B) and a single row of format buttons. They also asked for a better word than "Inspect", for video and MP3 to be one step, and for the file to save without asking. Built on `feature/brand-redesign`, merged into `main` (local, not pushed), and the container was rebuilt from `main`:
- **Logo:** redrawn as vector SVGs: a white lockup for the header and the square app icon as the tab icon.
- **Page:** restyled with a dark mode. The button is now "Fetch", and one Format radio group with a single Download button replaces the quality menu and two buttons.
- **Saving:** a ready file is saved automatically. "Didn't start? Save file" is the fallback link.

**Verified:**
- 354 offline tests pass, on the branch and again on the merged `main`.
- On the rebuilt container at `http://127.0.0.1`: the YouTube test video saved itself as a real MP4, and the MDN CC0 clip saved itself as an MP3 (`file` confirmed both). No console errors.
- Every state was checked in Chromium against a throwaway stub API that sends the real CSP: start, fetching, fetch error, inspected, no audio or thumbnail, downloading, converting, saving, saved, failed, and reload while ready. Checked on phone and desktop, in light and dark, with keyboard arrows on the format buttons. No console errors.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `b919980` | feat | Add SaveMe logo lockup and app icon SVGs |
| `2344eb8` | feat | Redesign the page around the SaveMe brand (Fetch, one format choice, automatic save) |
| `4a418fa` | test | Cover the brand assets and the single format choice |
| `e47e588` | docs | Describe the Fetch, Format and automatic-save flow in the README |
| `640f2dd` | merge | Merge `feature/brand-redesign` into `main` |

### Key Decisions

- **Wordmark outlined into the SVG, not a web font.** The CSP allows only same-origin assets, and a font file would be one more download. The letters come from Nunito Black (weight 900), converted with fontTools outside the repo.
- **Dark text on the coral Fetch button.** White on coral `#fe5d3b` is about 3:1 contrast, below AA for body-size text. Dark indigo on coral is about 5.7:1.
- **Formats are radio inputs inside labels, cloned from a `<template>`.** This gives keyboard arrows and screen-reader grouping for free, and the markup stays in `index.html` (no `innerHTML`).
- **Auto-save once per job, recorded in sessionStorage (`autoSaved`).** A reload after the first attempt shows "Ready to save" with the Save file link, instead of starting a second download unasked.
- **"Didn't start? Save file" shows immediately.** Some browsers (Chrome from the second automatic download) ask before allowing it, so the fallback should not wait for the 60-second delivery timeout.
- **Kept the pinned copy.** The saved headline is now "Saved", with "File removed from server." underneath; the full pinned sentence is still what screen readers hear.

### Issues Encountered

- An `<svg>` has no `hidden` property, so `node.hidden = …` on one does nothing. The old status spinner never showed for that reason. Toggled icons are now wrapped in `<span>`s.
- A browser test download overwrote `.playwright-mcp/Sunset-over-the-harbour.mp4` (a leftover from an earlier session; ignored by git) with 17 fake bytes.
- `.superpowers/` (brainstorm mockups) shows as untracked; it is not in `.gitignore` yet.
- `docker compose up -d --build` built the new image but left the old container running ("Running", not "Recreated"). `docker compose up -d --force-recreate` put the new image live.

---

## 2026-11-10 11:46 IST — Session: Security Review and Always-On Deployment

### Summary

The owner asked for a security review of `feature/slim-ffmpeg`, then for the app to run in the background all the time and restart on its own. The review found no high- or medium-severity vulnerabilities, because the minimal build removes network protocols and the playlist demuxers. It did find issues to follow up (below). Both unmerged branches went into `main` locally (not pushed), and the container was rebuilt from `main`:
- **Image:** about 311 MB, FFmpeg 9.0.2.
- **URL:** still `http://127.0.0.1` (port 80, from `SAVEME_PORT` in `.env`).
- **Restart policy:** `unless-stopped`.

**Verified:**
- 348 offline tests pass on the merged `main`.
- The container reports healthy, and `/health` returns 200 from the host.
- It runs as uid 10001, and `ffmpeg -version` reports 9.0.2.

### Changes Made

| Commit    | Type  | Description |
| --------- | ----- | ----------- |
| `b570247` | merge | Merge `feature/saveme-local-url` (configurable host port) into `main` |
| `469d125` | merge | Merge `feature/slim-ffmpeg` (minimal ffmpeg build) into `main` |

### Key Decisions

- **Merge both branches before deploying.** The slim build is on `feature/slim-ffmpeg`, but `SAVEME_PORT` is on `feature/saveme-local-url`. Building from either branch alone would have dropped one of them: the slim ffmpeg, or the port-80 URL.
- **Keep `restart: unless-stopped`, not `always`.** The container restarts after a crash or a Docker restart, but stays stopped if the owner stops it on purpose.

### Issues Encountered

- After the first `docker compose up -d`, the container was healthy inside, but Docker Desktop reset every host connection to 127.0.0.1:80. Another container on the same network could reach it. `docker compose down && docker compose up -d` recreated the network and fixed it.
- Reboots: the container comes back only if Docker Desktop starts at login. macOS blocked reading Docker Desktop's settings file, so this is not verified (Settings → General → "Start Docker Desktop when you sign in").
- Review follow-ups, not yet fixed:
  - `test_ffmpeg_build_reads_local_files_only` misses `ffmpeg` re-added on an `apt-get install` continuation line, and checks only the first `--enable-demuxer=`.
  - The ffmpeg build has no hardening flags (`--toolchain=hardened`).
  - FFmpeg security releases now need a manual version and checksum bump.
  - `.mpg`/`.vob` remux, and MP3 from WMA, AIFF or AMR, no longer work.

---

## 2026-11-10 11:35 IST — Session: Shrink the Docker Image (Minimal ffmpeg)

### Summary

The owner asked how to cut the 709 MB image and whether all of it is needed. Per-layer sizes showed `apt-get install ffmpeg` was 411 MB. The ffmpeg binaries themselves were under 1 MB; the rest was Debian's dependency tree (LLVM 120 MB via Mesa, a speech synthesizer, x265/aom/svt-av1 encoders, X11). The app only stream-copies video (merge, remux, yt-dlp fixups), decodes audio for MP3 and runs ffprobe. The image now builds FFmpeg 9.0.2 from a signature-checked, SHA-256-pinned source release with only those components. Results:
- **arm64:** 709 MB → 311 MB.
- **amd64:** 298 MB.
- **ffmpeg + ffprobe:** about 12 MB together.

**Verified:**
- A new `scripts/docker-ffmpeg-check.sh`, on both arm64 and amd64 (amd64 under emulation). It runs a 17-case codec/container matrix and the 187 integration tests inside the image.
- `pytest -m network` against a separate container on port 8123: 5 passed. Reddit failed with `login_required` at inspect, the known issue.
- Manual jobs: YouTube Best (AV1 + Opus → MP4), 720 → 480p H.264, and direct MP4 → MP3.
- 347 offline tests pass.

**Not run:** `scripts/docker-smoke.sh`. It would have recreated the owner's running port-80 container, so its checks (health, uid 10001, no curl_cffi, deno and ffmpeg run) were done by hand on the test container instead.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `886e687` | feat | Build a minimal ffmpeg instead of Debian's package |
| `e6f6668` | test | Add docker-ffmpeg-check.sh for the minimal ffmpeg build |

### Key Decisions

- **Build ffmpeg from source, not a prebuilt static binary.** The registry showed `mwader/static-ffmpeg`'s arm64 layer is 255 MB, which only gets the image to about 550 MB, and it means trusting third-party binaries. A source build with `--disable-everything` came to about 12 MB. It also drops network protocols and the HLS/DASH/concat demuxers, which backs up the `-protocol_whitelist file,pipe` wrapper.
- **The VP9 decoder stays in, although no video is decoded.** Without it, VP9 → MP4 (remux or merge) wrote an MP4 that wouldn't open, and ffmpeg still exited 0 ("Unsupported pixel format (-1)"). The muxer's vpcC box needs the pixel format, and only the decoder derives it while probing. Debian's full ffmpeg was the control: it handled the same input.
- **No subtitle codecs.** yt-dlp passes `-c:s mov_text` only in postprocessors the app doesn't enable (metadata/subtitle embedding, concat). The fixups it does run stream-copy subtitles, which needs no codec.
- **The check script verifies outputs, not exit codes.** Because of the VP9 case, each matrix output must open in Debian's full ffprobe with exactly the expected streams. Run against the build without the VP9 decoder, it failed both VP9 → MP4 cases.
- **Packaging tests lock in the pin and the hardening.** The checksum, `--disable-network`, `--enable-protocol=file,pipe` and the absence of `apt-get install ffmpeg` are now tested. A mutation check confirmed each new test fails when its guard is removed.
- **Branched from `main`, not `feature/saveme-local-url`.** The port change is unrelated. Both branches touch `tests/unit/test_packaging.py`, but in different tests.

### Issues Encountered

- ffmpeg.org dropped the tarball download once (TLS EOF after 128 s). The Dockerfile's curl now uses `--retry 5 --retry-all-errors`.
- The first amd64 build failed in the existing `css` stage: the Tailwind CLI download hit curl exit 56. A retry passed. Under emulation the build is slow (its apt step alone took about 12 minutes).
- The first matrix run flagged the timestamp fixup as dropping video. That came from the test clip: one keyframe, and yt-dlp's `-ss 0.001` with stream copy waits for the next one. Debian's full ffmpeg did the same. The clip now has a keyframe every 5 frames.
- Pulling and running `mwader/static-ffmpeg` was blocked by the permission classifier, so its size came from the registry manifest only.

---

## 2026-11-10 00:06 IST — Session: Build 1 (MVP) Implementation

### Summary

The owner asked for the Build 1 plan to be executed end to end, with non-colliding tasks run in parallel, and everything pushed to main afterwards. All 15 plan tasks were implemented by subagents, using git worktrees for parallel work. Each task went through a spec-and-quality review; seven needed fix rounds. A final whole-branch review was followed by one fix wave. The result is a working local Docker app ("saveme"): a FastAPI API with host/origin checks and degraded mode, a guarded per-job worker subprocess (connect-time socket guard, native downloaders only, ffmpeg/ffprobe pinned to local files), an in-memory job manager (queue, disk budget, TTL, cleanup), a vanilla-JS + Tailwind page, and a hardened image. The offline suite has 345 tests passing. The Docker smoke test passed after a clean rebuild. The browser walkthrough checks M1–M6 all passed against the real container.

**Sites verified against the container:**
- YouTube: 480p MP4 (h264) and MP3.
- youtu.be short link: inspect.
- Direct MP4.
- Unsupported-URL handling (example.com).
- Private-IP blocking.

**Tried and failed:**
- Reddit: `login_required`. Reddit blocks non-browser clients, so this likely needs curl_cffi impersonation, which is banned by the spec.

**Not verified:**
- Facebook, Instagram, Twitter/X.
- Streamtape, LuluVid: unreachable from the test host, and not supported in Build 1 anyway.

### Changes Made

| Commit    | Type  | Description |
| --------- | ----- | ----------- |
| `f5f652e` | chore | Scaffold project, settings and request schemas |
| `ee5e85d` | feat  | Filename sanitization and content disposition |
| `aa7eab3` | feat  | Error catalogue and yt-dlp error classification |
| `dc956d3` | feat  | ffmpeg/ffprobe wrappers restricting inputs to local files |
| `5c62093` | chore | pytest-asyncio default fixture loop scope |
| `3acbf02` | feat  | IP policy and URL pre-validation |
| `fbdfabf` | fix   | Harden URL pre-validation (IDNA, share-text punctuation, fec0::/10, SIIT) |
| `efd393b` | feat  | yt-dlp option building, format safety and inspect mapping |
| `5379162` | fix   | Infer missing format protocols before the native-only filter |
| `676049f` | feat  | Worker runner with timeout, size and cancellation kill |
| `e333894` | fix   | Map every runner failure to AppError |
| `99c702a` | feat  | Single-page UI with polling, resume and accessible states |
| `85f297f` | feat  | Connect-time socket guard |
| `93e4924` | fix   | Worker importable from its job directory without trusting it (`-P`) |
| `cb86a45` | feat  | Docker image, compose setup and smoke test |
| `dba4b17` | feat  | Worker subprocess with guarded inspect mode |
| `924f34f` | fix   | Classify yt-dlp errors without matching URL text |
| `f889d88` | fix   | Ignore host and port text when classifying yt-dlp errors |
| `0e10c7a` | feat  | Worker download mode with progress, limits and MP3/MP4 output |
| `dc1d60b` | fix   | Detect missing tracks from yt-dlp's original message only |
| `10b175b` | feat  | In-memory job manager with queue, disk budget, TTL and cleanup |
| `fba8d99` | fix   | Only evict ready files when eviction makes room |
| `0d120a8` | feat  | HTTP API with streamed delivery, host/origin checks and degraded mode |
| `57a0203` | fix   | Pin Deno by digest, drop PYTHONPATH, fail smoke test fast |
| `44a48f3` | docs  | README and real-site verification suite |
| `533120c` | docs  | Correct unverified site claims in README |
| `ff975bd` | fix   | Skip network tests per test so offline runs stay clean |
| `2ae06c7` | fix   | Log worker failure code and stderr tail for every failure |
| `b5b3e48` | fix   | Unknown-details notice only for an unknown video codec |
| `330fce1` | fix   | Thumbnail fallback to the thumbnails list |
| `081137a` | fix   | Exempt GET /health from the host check (custom ALLOWED_HOSTS stay healthy) |
| `30e2566` | fix   | Drop lone surrogates from download filenames |
| `b4891c8` | fix   | Whitespace-only ALLOWED_HOSTS means default |
| `2ba3029` | docs  | State the Reddit/curl_cffi cause as likely, not fact |
| `df1cdcd` | chore | Disable the Deno update check in the runtime image |
| `d44d685` | fix   | Replace lone surrogates in inspect metadata |

### Key Decisions

- **Parallel waves in isolated worktrees.** Tasks with disjoint files ran at the same time, each in its own `.worktrees/tN` branch, and were cherry-picked onto the feature branch after review. Some tasks started before their predecessor's review ended (T8 on T4, T12 on T9 and T11, T15 early), because each consumed only stable interfaces. This cut the critical path from 15 stages to about 10.
- **Disk budget accounting deviates from the literal spec formula, on purpose.**
  - At submit time, other active jobs' reservations are not counted. Under the defaults the literal formula would return 503 for the third submission, making the 4-slot queue useless.
  - At start time, each active job counts as max(actual usage, reservation) rather than usage plus reservation, which double-counted.
  - Ready files are evicted only when eviction can actually make room. The spec's stated intent ("10 GB fits two concurrent max-size merges") won over its formula.
- **Workers run as `python -P -m app.worker` with `PYTHONPATH=<project root>`.** The worker's cwd is the job directory, so it can't import `app` from there. `-P` keeps that untrusted directory off `sys.path`.
- **The error classifier ignores URL, id, host and port text.** yt-dlp echoes URLs into its errors, so a port containing "429" was being classified as rate-limited. Keyword rules now run on cleaned text, and the blocked marker is checked first on the raw text.
- **`GET /health` is exempt from the Host check.** Otherwise a custom `ALLOWED_HOSTS` without 127.0.0.1 would leave the container permanently unhealthy. The endpoint reveals only status or a config message.
- **The Task 13 page and the Task 14 Docker files were pulled forward and split in two.** Page-contract tests read the files from disk, and the HTTP-level tests moved into the API task. Task 14 ran as 14a (image, early) and 14b (smoke run and walkthrough, after the API existed).

### Issues Encountered

- **Worker import bug.** The worker couldn't import `app` from its job directory, which would have failed every real job as `internal`. It was found during Task 8 integration and fixed in the runner (`-P` plus PYTHONPATH).
- **Classifier misclassified from URL text.** It read substrings of the echoed URL and made one test flaky (~0.5–1%). Fixed in the classifier itself.
- **FFmpeg already refuses the canary.** FFmpeg 8 (Homebrew) and FFmpeg 7.1 (Debian) default local inputs to `file,crypto,data`, so the plan's control canary fetched nothing. The control now passes an explicit permissive whitelist, and the test proves the wrappers override it.
- **YouTube test video swapped.** The plan's video (`jNQXAC9IVRw`) only offers 240p, so the network suite now uses `C0DPdy98e4c`. The JS file hosts (Streamtape, LuluVid, Doodstream) returned 502 from the test host, so the unsupported probe uses example.com.
- **Reddit fails with `login_required`.** It can't be fixed without curl_cffi, which the spec bans. This is a product decision for the owner.
- **Subagent co-author trailers.** Some subagents used a different Co-Authored-By trailer; it was normalised to the project's trailer at cherry-pick, with message-only changes.
- **Deferred minors.** Final review triage left these minor findings for later (not fixed):
  - A large playlist URL inspect pages until the 30 s timeout (playlists are a non-goal).
  - ALLOWED_HOSTS entries with a port or wildcard never match (fails closed).
  - The smoke script takes its restart-count baseline on the first poll.
  - A cancel during the runner's 1 s stderr drain skips task cleanup.
  - No final size check after MP3 conversion (the parent's 2× workdir kill is the cap).
  - Raw yt-dlp RequestErrors map to `internal` rather than a specific code.
  - Assorted test-coverage gaps.

---

## 2026-11-09 21:19 IST — Session: BRD Review, Design and Build Plan

### Summary

The owner asked to read the Media URL Downloader BRD and start building. We talked through which sites matter (YouTube, Facebook, Instagram, Reddit, Twitter/X, Streamtape, LuluVid, "anything"). We checked yt-dlp 2026.10.26 against that list, then wrote and approved a Build 1 (MVP) design and a 15-task implementation plan. The owner asked to wait for them before any building starts, so no product code was written this session.

### Changes Made

| Commit    | Type | Description                                        |
| --------- | ---- | -------------------------------------------------- |
| `1922b14` | docs | Add business requirements document                 |
| `3dc0732` | docs | Add Build 1 (MVP) design for media URL downloader  |
| `134c1e7` | docs | Add Build 1 (MVP) implementation plan              |

### Key Decisions

- **Two builds.** yt-dlp has no extractor for Streamtape, LuluVid or similar JS file hosts. Supporting them needs a headless-browser fallback, which brings its own security work, so the owner chose to ship the BRD's P0 MVP first and add the fallback as Build 2.
- **"Best" means highest resolution in any codec, and the 1080/720/480 tiers prefer H.264 MP4.** This gives the owner a choice between maximum quality and "plays everywhere".
- **Quality tiers measure the short side.** Vertical Shorts and Reels would otherwise only offer "Best (1920p)", often in VP9/AV1, which QuickTime can't play.
- **Two-layer SSRF defence.** The API pre-validates the URL and its DNS answers. Each worker subprocess then checks every socket connection against the IP policy at connect time. This covers redirects, DNS rebinding and segment URLs.
- **The FFmpeg side door is closed with wrapper binaries.** They pin `-protocol_whitelist file,pipe`, because yt-dlp's direct ffprobe calls don't accept postprocessor args.
- **Each job runs in its own worker subprocess and process group.** This makes hard kills on timeout or size breach possible, and keeps the socket guard away from the API server.
- **Soft disk budget of 10 GB, with 2×file-size reservations per active job.** A merge holds the parts and the output at once. Unsaved ready files are evicted before new jobs are refused.
- **Degraded mode instead of exiting on a bad config.** This avoids crash loops under `restart: unless-stopped`.
- **Host-header allowlist and Origin check added.** The service is unauthenticated on localhost, and these block DNS-rebinding and CSRF attacks against it from malicious web pages.

### Issues Encountered

- yt-dlp now needs Deno for full YouTube support, so the image must include it. We verified that yt-dlp runs Deno without network permission (`--no-remote`, no `--allow-net`) and that `remote_components` is empty by default.
- None blocking. Nothing has been built yet; implementation waits for the owner's go-ahead.
