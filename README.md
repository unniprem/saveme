<p align="center">
  <img src="static/brand/icon.svg" alt="SaveMe logo" width="88" height="88">
</p>

<h1 align="center">SaveMe</h1>

<p align="center">
  <strong>A private, self-hosted media downloader.</strong><br>
  Paste a link, see what's there, and save it as an MP4 video, an MP3 or an M4A, or cut a clip from it, all on your own machine.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB.svg">
  <img alt="Runs in Docker" src="https://img.shields.io/badge/runs%20in-Docker-2496ED.svg">
  <img alt="Status: v0.1" src="https://img.shields.io/badge/status-v0.1-orange.svg">
</p>

---

SaveMe is a small web app that runs in Docker on your computer. It puts a simple browser page in front of [yt-dlp](https://github.com/yt-dlp/yt-dlp) and FFmpeg: paste a link to a video or audio page, check the title, length and available qualities, then download it. There are no accounts, no history, no database and no telemetry, and files are deleted from the server as soon as your browser has saved them.

> [!IMPORTANT]
> SaveMe is built for **one person on their own computer**. It has no login, so it only listens on `127.0.0.1`. Do not put it on the internet or your local network. See [Security](#security).

## Contents

- [Why SaveMe](#why-saveme)
- [Features](#features)
- [Installation](#installation)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Usage](#usage)
- [Clip Studio](#clip-studio)
- [Supported sites](#supported-sites)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Development](#development)
- [Responsible use](#responsible-use)
- [Contributing](#contributing)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## Why SaveMe

Saving a clip you're allowed to keep (your own upload, a lecture, a Creative Commons video, a talk you want to watch offline) usually leaves you two choices, and both have problems.

**Online "video downloader" websites** are full of ads, pop-ups and fake download buttons. Every link you paste goes to someone else's server, and the files they hand back are often re-encoded, size-capped or bundled with something you didn't ask for.

**yt-dlp on the command line** is excellent, and SaveMe is built on it. But it means a terminal, installing FFmpeg yourself and remembering format flags. Its default "best quality" is often VP9 or AV1, which QuickTime and some phones can't play.

SaveMe sits in between: yt-dlp's reach, behind a one-page interface, running on hardware you control.

- **Private.** Links and files never leave your machine, except to fetch the media from its source. Nothing is logged to a third party, and nothing is kept once you've saved it.
- **Plays everywhere.** Downloads default to H.264 video with AAC audio in MP4, so they open in QuickTime, Windows Media Player, phones and TVs. "Best" is there when you want the highest quality regardless of codec.
- **Honest.** The quality menu lists only what the source actually offers, progress is real and never estimated, and errors say what went wrong ("requires login", "unsupported", "busy") rather than "something went wrong".
- **Safe by design.** A pasted link can't make SaveMe reach into your home network or router (see [Security](#security)), and the container runs locked down.
- **Easy to run.** One command to start, one to stop, and one to remove it completely.

## Features

- Inspect a link before downloading: title, length, thumbnail and the qualities that exist
- Video as MP4: **Best**, **1080p**, **720p** or **480p**
- Audio as **MP3** (LAME VBR, about 190 kbps), or as **M4A** that keeps the source's original AAC audio with no re-encoding, when the source has it
- Files are tagged with the title, artist, date, description and chapters, and get the source's thumbnail as cover art when it's a JPEG or PNG
- Live progress, including the separate video and audio tracks many sites send
- **Cancel** a download and **resume** it within 5 minutes, even after a page reload
- A bounded queue, with limits on file size, job time and disk use
- Automatic cleanup: files are deleted after you save them, or after a timeout
- **Clip Studio**: open a link or a video from your computer, preview it, mark a start and an end, and export just that part as MP4. Or collect up to 20 parts in a clip list and export them all as one ZIP. Cutting runs on the server with no re-encoding, with progress and **Cancel**
- Light and dark mode, and a layout that works on a phone
- No database, accounts, cookies, analytics or external services

## Installation

### Requirements

| | |
|---|---|
| **macOS or Windows** | [Docker Desktop](https://www.docker.com/products/docker-desktop/) (a current version) |
| **Linux** | Docker Engine 25 or newer and the Docker Compose v2 plugin |
| **Disk** | About 350 MB for the image, plus room for the files you download |
| **Network** | Internet access during the build and when downloading |

Docker Engine 25 is the minimum because the Compose health check uses `start_interval`. Check what you have with `docker version` and `docker compose version`.

### Steps

**1. Get the code**

```sh
git clone https://github.com/unniprem/saveme.git
cd saveme
```

> [!NOTE]
> Windows with Docker Desktop hasn't been tested yet. The repository keeps Unix line endings on every platform, so a normal clone should build; reports are welcome.

**2. (Optional) Change the settings**

Every setting has a sensible default. To change one, copy the template and edit it:

```sh
cp .env.example .env
```

See [Configuration](#configuration) for what each setting does.

**3. Build and start**

```sh
docker compose up -d --build
```

The first build compiles a minimal FFmpeg from source, so it takes a few minutes. Later starts take seconds.

**4. Open it**

Go to **<http://localhost:8000>**. To check that it's running:

```sh
docker compose ps          # STATUS should show "healthy"
```

**Stop and start**

```sh
docker compose stop        # stop, keeping the container
docker compose start       # start it again
```

The container restarts automatically with Docker unless you stop it.

## Updating

Pull the latest code and rebuild:

```sh
cd saveme
git pull
docker compose up -d --build --force-recreate
```

Sites change often, and most "this used to work" problems are fixed by a newer yt-dlp. See [Updating yt-dlp](#updating-yt-dlp) to update it on its own.

## Uninstalling

SaveMe keeps nothing on your computer outside its folder and its Docker image. It uses no volumes, and its working files live inside the container, so removing the container removes them too.

**1. Stop it and remove the container and image**

```sh
cd saveme
docker compose down --rmi all
```

**2. Delete the folder** (this also removes your `.env`, if you made one)

```sh
cd ..
rm -rf saveme
```

**3. (Optional) Reclaim build space**

Docker keeps a build cache and the base images it pulled (`python`, `debian`, `denoland/deno`). Other projects may share them, so remove them only if you don't need them:

```sh
docker builder prune       # clear the build cache
docker image ls            # find base images you no longer use, then: docker image rm <image>
```

Files you already downloaded are in your browser's downloads folder and are not touched.

If you ran SaveMe without Docker (see [Development](#development)), deleting the folder also removes its `.venv`. Its job folder is `/tmp/media-jobs` unless you set `TEMP_DIR`, and you can delete that too.

## Usage

1. **Paste a link and choose Fetch.** SaveMe shows the title, length, thumbnail and the formats available.
2. **Pick a format** (a video quality, **MP3 audio** or **M4A audio (original)**) and choose **Download**.
3. **Save the file.** When it's ready, your browser saves it straight away. If the browser holds it back, use the **Save file** link.

**Video qualities.** **1080p**, **720p** and **480p** pick the highest H.264 version at or below that size, so the file plays almost everywhere. The default is the highest of these the source offers. **Best** is the highest resolution regardless of codec; it may be VP9 or AV1, which VLC and browsers play but QuickTime may not.

**Best label.** The **Best** option shows what you'd get, for example `Best (2160p60 · AV1/VP9)`. That is the short side of the picture (2160p), the frame rate when it's above 30 (60), and the codec families available at that size and frame rate (AV1 or VP9). If the source doesn't say what a codec is, the codec part is left out, as in `Best (1080p)`.

**M4A audio (original).** Shown after **MP3 audio**, and only when the source says its audio is AAC. SaveMe copies that audio into an `.m4a` file as it is, so it's quick and loses no quality. If the source has no AAC audio, or doesn't say what its audio is, the choice doesn't appear; use **MP3 audio** instead.

**Tags and cover art.** Every download is tagged with the title, artist, date, description and chapters when the source provides them. Cover art is best effort: when the source has a JPEG or PNG thumbnail, it's embedded in MP3, M4A and MP4 files. The file is saved without a cover if there's no such thumbnail, if it can't be fetched within 15 seconds, or if it isn't a usable image. A video delivered as MKV or WebM (because it couldn't be converted to MP4) never gets a cover. The link a file came from is never written into it.

**Saving.** Each file can be saved once, by one download at a time. It's deleted after it's saved, or after `JOB_TTL_MINUTES` (30 by default) if it never is. If a save is interrupted, or the browser takes nothing for a minute, the server stops sending and keeps the file, so **Save file** works again.

**Cancel and resume.** Choose **Cancel** to stop a download. The partial download stays on the server for 5 minutes, and **Resume download** continues where it stopped, even after a page reload. After 5 minutes it's deleted. SaveMe keeps one partial download per place in the queue (`MAX_ACTIVE_JOBS` + `MAX_QUEUED_JOBS`); cancelling more than that deletes the oldest first.

## Clip Studio

Clip Studio cuts a part out of a video. It's the first step toward automatic clipping; for now you choose the part yourself.

1. **Open a video.** After **Fetch**, pick a video quality and choose **Clip**. Or choose **Clip a video from your computer** under the link field to upload a file. Uploads take MP4, MOV, MKV, WebM, TS, FLV and AVI files up to `MAX_FILE_SIZE_MB`.
2. **Mark the part you want.** Drag the two handles on the timeline, type a start and an end (`65.5`, `1:05.5` or `1:02:05`), or play the video and choose **Set to playhead**. The `I` and `O` keys set the start and end at the playhead. **Play clip** plays just your selection.
3. **Export clip.** The clip is cut on the server, with progress and **Cancel**, and saved like a download, named like `Talk (clip 1m05s-1m40.5s).mp4`. Then mark another part, or choose **Close**.
4. **Or build a clip list.** **Add to list** keeps the current start and end, up to 20 clips. The list is sorted by start, and adding the same part twice is refused. **Load** puts a clip back in the editor to preview, adjust or export alone, and **Remove** drops it. **Export N clips as ZIP** cuts them all in one job, with one progress bar ("Cutting clip 2 of 5") and **Cancel**. It saves one file, named like `Talk (5 clips).zip`, that holds `01 Talk (clip 0m05s-0m12.5s).mp4`, `02 …` and so on. The list stays until you remove its clips or choose **Close**, and a page reload keeps it.

**How cutting works.** Clips are cut without re-encoding (a stream copy), so they keep the original quality and are quick to make. A video can only start at a keyframe, so the clip carries the stretch from the keyframe before your start, hidden by an MP4 edit list. Browsers, QuickTime, VLC and most phones honour it and start exactly where you chose. A few players ignore edit lists and show that stretch too: at most the gap between two keyframes, usually a few seconds. MKV and WebM have no edit lists, so a clip cut from one (a file that couldn't be converted to MP4) always shows that stretch. Clips from links keep the title, artist and date tags; clips from your own files carry no tags or metadata from them (phones write the location into videos). Chapters are left out of clips because their times would be wrong.

**Preview.** If your browser can't play a video (some HEVC or MKV files), the studio says so; the timeline and typed times still work, and the export is unaffected. If it can't play the file at all, **Set to playhead**, **Play clip** and the `I`/`O` keys are turned off, because the playhead can't move.

**Size.** A clip, or a whole ZIP, must stay under `MAX_FILE_SIZE_MB`. A ZIP needs room for two files while it's made (the ZIP and the clip being added), like a download, so at the smallest disk budget a ZIP may be refused while single clips still work.

**What's kept, and for how long.** The video you're clipping stays on the server while the studio is open. It's deleted when you choose **Close**, after `JOB_TTL_MINUTES` without use (an open, visible tab counts as use), when you open more than `MAX_CLIP_SOURCES` videos (the least recently used goes first), or when SaveMe restarts. Exported clips and ZIPs are deleted once saved, like downloads.

## Supported sites

SaveMe works with sites [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), with the limits below. Only the first two rows have been checked end to end.

| Source | Status | Notes |
|---|---|---|
| YouTube | ✅ Verified | Inspect, 480p MP4 (H.264), MP3 and M4A download, with tags and cover art. `youtu.be` short links: inspect verified. |
| Direct media links (e.g. `.mp4`) | ✅ Verified | Inspect and download. |
| Facebook, Instagram, X/Twitter | ⚠️ Best effort | yt-dlp has extractors, but they're untested here and often need a login. |
| Reddit | ⚠️ Often fails | Reddit refused the test post with 403 ("requires a login"). The likely cause is that Reddit refuses clients that don't look like a browser: yt-dlp asks for browser impersonation for Reddit, which needs `curl_cffi`, and SaveMe deliberately leaves that out (see [Security](#security)). |
| JavaScript file hosts (Streamtape, LuluVid, …) | ❌ Not supported | yt-dlp has no extractor for them. A headless-browser fallback is planned. |
| DRM-protected video, anything behind a login, playlists, live streams | ❌ Not supported | One public video at a time. |

Two safety checks are verified too: a page with no media (example.com) returns "unsupported", and a private address (192.168.1.1) is refused with "blocked_address".

## Configuration

Settings are environment variables read from `.env` (copy `.env.example` to start). All of them are optional; a blank or missing value means the default. Apply changes with `docker compose up -d`.

An invalid value doesn't crash the app. It runs in a degraded mode: the page shows a banner naming the variable, and `/health` returns 503.

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `SAVEME_PORT` | `8000` | 1-65535 | Port on `127.0.0.1` the app is published on (read by Compose, not the app) |
| `MAX_ACTIVE_JOBS` | `2` | 1-16 | Downloads that run at the same time |
| `MAX_QUEUED_JOBS` | `4` | 0-100 | Downloads that may wait; more are refused with "busy" |
| `MAX_ACTIVE_INSPECTIONS` | `4` | 1-32 | Inspections that run at the same time |
| `INSPECT_TIMEOUT_SECONDS` | `30` | 5-300 | Seconds before an inspection gives up |
| `MAX_FILE_SIZE_MB` | `2048` | 1-102400 | Largest file one download may produce |
| `MAX_JOB_SECONDS` | `1800` | 10-86400 | Seconds before a download is stopped |
| `JOB_TTL_MINUTES` | `30` | 1-1440 | Minutes a finished file waits to be saved |
| `MAX_READY_FILES` | `6` | 1-100 | Finished files kept at once; the oldest goes first |
| `MAX_TOTAL_DISK_MB` | `10240` | 1-10485760 | Disk budget for `TEMP_DIR`; at least 2 × `MAX_FILE_SIZE_MB` |
| `MAX_CLIP_SOURCES` | `2` | 1-20 | Videos open in Clip Studio at once; opening another closes the least recently used |
| `TEMP_DIR` | `/tmp/media-jobs` | absolute path | Job workspace inside the container (writable by uid 10001) |
| `LOG_LEVEL` | `info` | debug, info, warning, error | Log level |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | comma-separated | Host names the browser may use to reach the app |

### Updating yt-dlp

To update yt-dlp for good, bump the `yt-dlp` pin in `requirements.in`, recompile `requirements.txt`, update the default `YTDLP_VERSION` in the `Dockerfile` to match, and rebuild. For a quick one-off override:

```sh
docker compose build --no-cache --build-arg YTDLP_VERSION=<new-version>
docker compose up -d
```

## Troubleshooting

| Problem | What to do |
|---|---|
| The container is unhealthy or won't start | Run `docker compose logs saveme`. A bad setting appears in the log, and as a banner on the page, naming the variable. |
| "The host is not allowed" | Open the app at `localhost` or `127.0.0.1`, or add your host name to `ALLOWED_HOSTS`. |
| "This video requires login" | The site wants an account. Logins aren't supported. |
| Rate limits or "try again later" | The site is throttling your IP address. Wait and retry, or update yt-dlp. |
| A site that used to work now fails | [Update yt-dlp](#updating-yt-dlp). |
| Stuck on "Downloading video track (1 of 2)" | Many sites, YouTube included, send video and audio as separate tracks, which are joined at the end. At 1080p or Best the video track alone can be several hundred MB. The MB count under the bar shows it's moving; choose **Cancel** to stop. |
| "Busy" or disk full | Finished files take space until saved or expired. Lower `MAX_READY_FILES` or `JOB_TTL_MINUTES`, or raise `MAX_TOTAL_DISK_MB` if the disk allows. |
| Clip Studio says "Your browser can't preview this video" | The browser can't decode it (often HEVC or MKV). Type the start and end instead; the clip is cut the same way. |
| "These clips add up to more than the … limit" | A ZIP must stay under `MAX_FILE_SIZE_MB`. Export fewer or shorter clips (or export them one at a time), or raise the limit. |
| A clip starts a little early in one app | That app ignores the MP4 edit list that hides the keyframe lead-in. See [How cutting works](#clip-studio). |
| Port 8000 is already in use | Set `SAVEME_PORT` in `.env` to a free port, then run `docker compose up -d`. |

## Security

SaveMe fetches whatever URL it's given, so it's designed so that a link can't be turned against you.

**Local only.** SaveMe is a personal tool that runs on your own computer, so it has no login or accounts by design. Compose publishes the app on `127.0.0.1` only, and `python -m app` (outside Docker) also listens on loopback by default. Inside the container the app listens on `0.0.0.0`, which the port mapping needs; that address stays inside the container's network. **Never expose SaveMe publicly**: no port forwarding, no `0.0.0.0` binding on the host and no public reverse proxy.

**Links can't reach your network (SSRF protection).**
- The API checks each URL first (scheme, no embedded credentials, length) and resolves its host. Every address must be public.
- Each inspection and download runs in its own worker process, which checks **every** socket connection against the same public-address rule. Redirects, DNS rebinding and sub-requests are covered.
- FFmpeg and ffprobe are wrapped to run with `-protocol_whitelist file,pipe`, so they only read local files.
- FFmpeg is built from source with only what the app needs: no network protocols, no HLS/DASH/concat demuxers and few decoders. The `Dockerfile` lists exactly what's included.
- Proxies are disabled, and `curl_cffi` isn't installed, because its networking runs in C and would bypass the guard.
- Deno, which yt-dlp needs for YouTube, runs without network permission.

**Cover art is handled outside FFmpeg.** The worker fetches the thumbnail through the same guard as every other request. It accepts JPEG or PNG only (judged by the file's first bytes), at most 5 MB, and tries at most three addresses, each request with a 5-second network timeout and the whole fetch within 15 seconds. The image is then written into the file by mutagen, a Python tag library, so FFmpeg never reads it and the FFmpeg build has no image decoders. A failed cover never fails a download.

**Uploads have limits of their own.** The Clip Studio upload (`POST /api/sources/upload`) is the only request whose body isn't capped at 64 KB. It must be the raw file (`application/octet-stream`) with a `Content-Length` no larger than `MAX_FILE_SIZE_MB`. Before a byte is written, it must fit the queue, `MAX_CLIP_SOURCES` and the disk budget. It's cut off after 60 seconds without data or `MAX_JOB_SECONDS` in all, and anything it wrote is removed. The Host and Origin checks apply as for every other request. An uploaded file is opened only by ffprobe and FFmpeg inside a worker process, with the same memory cap and local-files-only wrappers as downloads; the build's few demuxers limit what can be opened at all. Cutting is a stream copy in a worker too, so the FFmpeg build gains nothing for it.

**A ZIP of clips is built from names the server chose.** Its entries are named from the checked times and the source title, through the same file-name cleaning as downloads, and the worker refuses any name with a folder part, a control or Windows-forbidden character, or a leading dot. Python's own `zipfile` writes it inside the worker, uncompressed, so nothing new is added to the image. `zip` is not one of the types a download can be delivered as, so a link to a `.zip` file still can't come out of the downloader.

**Files never carry their source link.** The page address and any `comment` tag the source file arrived with are blanked before tagging. The only addition to the FFmpeg build for tags is the `ffmetadata` demuxer, which reads the chapter list.

**One bad link fails only its own job.** A worker reads at most 16 MB from any page, manifest or API response (media downloads aren't affected). Each worker process, including the Deno and FFmpeg it starts, is limited to 1 GB of memory, so a hostile page can't use up the memory the rest of the app shares. The memory limit applies in the Docker image; macOS doesn't allow it, so a native macOS run goes without. The API also caps request bodies at 64 KB, counts link lookups against the inspection and queue limits, and accepts a POST only from the page's own origin.

**A locked-down container.** It runs as a non-root user with all Linux capabilities dropped, `no-new-privileges`, and memory, CPU and process limits. The app code is read-only to the app user. The build verifies what it downloads: the FFmpeg source by SHA-256, the Deno image by digest and the Tailwind CLI by checksum.

### Reporting a vulnerability

Please **don't** open a public issue for a security problem. Use GitHub's private vulnerability reporting instead (**Security → Report a vulnerability** on this repository). Include the steps to reproduce it and what an attacker could do. Known issues are tracked under the [`security` label](https://github.com/unniprem/saveme/issues?q=label%3Asecurity).

## Development

### How it works

```text
Browser ──▶ FastAPI server ──▶ worker process ──▶ FFmpeg ──▶ temp job folder ──▶ Browser
            validates the URL    yt-dlp, with every    local files
            and runs the queue   connection checked    only
```

The server never fetches a remote URL itself; every network request happens in a guarded worker. Clip Studio's probing, cutting and ZIP building run in the same kind of worker, on local files only. Job state lives in memory and files live in `TEMP_DIR`, so nothing is kept across restarts. Any files left behind by a crash are cleaned up on the next start.

**Stack:** Python 3.12, FastAPI, Uvicorn, yt-dlp, FFmpeg (minimal build), Deno, Tailwind CSS v4 and vanilla JavaScript.

### Project layout

```text
app/          FastAPI app, job manager, worker, network guard, settings
app/bin/      ffmpeg/ffprobe wrappers that allow local files only
static/       the page: HTML, JavaScript, Tailwind source and brand assets
scripts/      CSS build, Docker smoke test and FFmpeg check
tests/        unit, integration and opt-in real-site tests
docs/         design spec and implementation plan
```

### Run without Docker

Docker is the recommended way to run SaveMe. A native run skips the container hardening and uses your system FFmpeg, so use it for development only.

You need Python 3.12+, `ffmpeg` and `ffprobe` on your `PATH`, [Deno](https://deno.com) on your `PATH` (for YouTube), and `curl` (to fetch the Tailwind CLI). The CSS build runs on macOS and Linux.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
scripts/build-css.sh                 # builds static/dist/styles.css
.venv/bin/python -m app              # serves http://127.0.0.1:8000
```

The native run doesn't read `.env`; set settings as environment variables in your shell instead.

### Tests

```sh
# Offline unit and integration tests (the default; network tests are skipped)
.venv/bin/pytest -q

# Docker smoke test: builds, waits for healthy, checks the API and the image hardening
scripts/docker-smoke.sh

# The minimal FFmpeg: a codec/container matrix plus the integration tests, run inside
# a copy of the built image. Run it after changing the FFmpeg configure flags.
scripts/docker-ffmpeg-check.sh

# Real-site checks: need the container running and internet access
docker compose up -d --build
.venv/bin/pytest -m network -v
```

The network tests read `SAVEME_BASE_URL` (default `http://127.0.0.1:8000`) and skip if `/health` can't be reached. Their URLs live in `tests/network/urls.py`; if a site removes a video, swap it there.

## Responsible use

SaveMe is for media you have the right to save: your own uploads, public-domain or openly licensed work, and content whose terms allow downloading. It doesn't circumvent DRM, paywalls, logins or CAPTCHAs, and it never will. You're responsible for complying with copyright law and each site's terms of service. The authors don't endorse using SaveMe to infringe anyone's rights.

## Contributing

Bug reports, site compatibility reports and pull requests are welcome.

- **A site doesn't work?** Check that it also fails with the latest yt-dlp. Most extraction problems belong [upstream](https://github.com/yt-dlp/yt-dlp/issues); open an issue here if SaveMe handles the failure badly.
- **Pull requests:** keep each one focused on a single change, add tests for new behaviour, and make sure `.venv/bin/pytest -q` passes. For changes to the Docker image, also run `scripts/docker-smoke.sh`.
- **Larger changes:** open an issue first so we can agree on the approach.
- **Out of scope:** logins, user accounts and hosting SaveMe for other people. It's a single-user, local tool by design.

## Acknowledgements

SaveMe stands on [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org), [FastAPI](https://fastapi.tiangolo.com), [Deno](https://deno.com) and [Tailwind CSS](https://tailwindcss.com).

## License

[MIT](LICENSE) © 2026 unni.dev
