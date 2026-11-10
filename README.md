# saveme

A small web app that runs on your own machine in Docker. Paste a link to a video or audio page, check what it finds, then save it as an MP4 video or an MP3 audio file. It is built on FastAPI and [yt-dlp](https://github.com/yt-dlp/yt-dlp), and it is for one person on one computer.

## Start it

You need Docker Engine 25 or newer (the compose health check uses `start_interval`, which older versions don't know) and Docker Compose v2.

```sh
docker compose up -d --build
```

Then open <http://localhost:8000>. Stop it with `docker compose down`.

Settings are optional. To change any, copy `.env.example` to `.env`, edit it and run `docker compose up -d` again.

## Use it

1. Paste a link and choose **Inspect**. You see the title, length, thumbnail and which outputs are available.
2. Pick **Video (MP4)** with a quality, or **Audio (MP3)**, and start the download.
3. When the file is ready, save it. Each file can be fetched once; it is deleted after you save it, or after `JOB_TTL_MINUTES` if you never do.

Video qualities are Best, 1080, 720 and 480. The default is the highest H.264 tier, so the file plays almost everywhere.

## What works and what doesn't

**Verified** (checked with `pytest -m network` against the running container):

- YouTube: inspect, 480p MP4 (H.264) and MP3 download.
- YouTube `youtu.be` short link: inspect only.
- A direct public MP4 link: inspect and download.
- A page with no media (example.com) returns "unsupported".
- A private address (192.168.1.1) is refused with "blocked_address".

**Tried and failed:** Reddit. The test post was refused with "requires a login": Reddit answered 403 and yt-dlp reported that account authentication is required. Treat Reddit as not verified.

**Not verified:** Facebook, Instagram, Twitter/X and every site not listed above.

**Best effort:** Facebook, Instagram and Twitter/X. yt-dlp has extractors for them, but they were not tested here and often need a login.

**Reddit** may fail. The likely cause is that Reddit refuses clients that don't look like a browser: yt-dlp asks for browser impersonation when it sets up a Reddit session, and impersonation needs `curl_cffi`. That package is deliberately not shipped (see Safety notes).

**Not supported:**
- File-hosting sites that hide the file behind JavaScript, such as Streamtape and LuluVid. yt-dlp has no extractor for them, so they are not supported in Build 1; a headless-browser fallback is planned for Build 2. From the test host these sites were unreachable, so the exact error you see is not verified (likely "unsupported" or "unreachable").
- DRM-protected video.
- Anything that needs a login.
- Playlists (one video at a time).
- Live streams.

## Environment variables

Every variable is optional; a blank or missing value means the default. An invalid value puts the app in a degraded mode: the page shows a banner naming the variable and `/health` returns 503.

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `MAX_ACTIVE_JOBS` | 2 | 1-16 | Downloads that run at the same time |
| `MAX_QUEUED_JOBS` | 4 | 0-100 | Downloads that may wait; more are refused with "busy" |
| `MAX_ACTIVE_INSPECTIONS` | 4 | 1-32 | Inspections that run at the same time |
| `INSPECT_TIMEOUT_SECONDS` | 30 | 5-300 | Seconds before an inspection gives up |
| `MAX_FILE_SIZE_MB` | 2048 | 1-102400 | Largest file one download may produce |
| `MAX_JOB_SECONDS` | 1800 | 10-86400 | Seconds before a download is stopped |
| `JOB_TTL_MINUTES` | 30 | 1-1440 | Minutes a finished file waits to be saved |
| `MAX_READY_FILES` | 6 | 1-100 | Finished files kept at once; the oldest goes first |
| `MAX_TOTAL_DISK_MB` | 10240 | 1-10485760 | Disk budget for `TEMP_DIR`; at least 2 x `MAX_FILE_SIZE_MB` |
| `TEMP_DIR` | `/tmp/media-jobs` | absolute path | Job workspace inside the container (writable by uid 10001) |
| `LOG_LEVEL` | `info` | debug, info, warning, error | Log level |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | comma-separated | Host names the browser may use to reach the app |

## Updating yt-dlp

Sites change often, so yt-dlp needs regular updates. To make the change permanent, bump the `yt-dlp` pin in `requirements.in`, recompile `requirements.txt`, and rebuild. For a quick one-off override:

```sh
docker compose build --no-cache --build-arg YTDLP_VERSION=<new> && docker compose up -d
```

(Also update the default `YTDLP_VERSION` in the `Dockerfile` so the pin in the repo matches.)

## Troubleshooting

- **The container is unhealthy or won't start.** Run `docker compose logs saveme`. A bad setting shows up as a config banner on the page and in the log, naming the variable.
- **"This video requires login."** The site wants an account. Logins aren't supported.
- **Rate limits or "try again later".** The site is throttling your IP. Wait and retry, or update yt-dlp.
- **A site that used to work fails.** Update yt-dlp (above).
- **Disk full / "busy".** Finished files take space until saved or expired. Lower `MAX_READY_FILES` or `JOB_TTL_MINUTES`, or raise `MAX_TOTAL_DISK_MB` if the disk allows.
- **The page says the host is not allowed.** Open it at `localhost` or `127.0.0.1`, or add your host name to `ALLOWED_HOSTS`.

## Safety notes

- The app has no login. Compose binds it to `127.0.0.1:8000` only. **Never expose it publicly** (no port forwarding, no `0.0.0.0` binding, no public reverse proxy).
- The container runs as a non-root user with all capabilities dropped, `no-new-privileges`, and memory, CPU and process limits.
- **SSRF model.** A pasted link must not make the app reach into your network.
  - The API checks each URL first (scheme, no embedded credentials, length) and resolves its host; every address must be public.
  - Each download or inspection runs in its own worker subprocess. That worker checks every socket connection against the same public-IP policy, so redirects, DNS rebinding and sub-requests are covered.
  - ffmpeg and ffprobe are wrapped so they run with `-protocol_whitelist file,pipe` and can only read local files.
  - ffmpeg is also built from source with only what the app uses, so there are no network protocols, no HLS/DASH/concat demuxers, and few decoders. The `Dockerfile` lists exactly what is included.
  - Proxies are disabled, and `curl_cffi` is not installed, because it does its networking in C and would bypass the guard. This appears to be why some Reddit links fail.
- Deno (needed by yt-dlp for YouTube) runs without network permission.

## Running the tests

```sh
# Offline unit and API tests (the default; network tests are deselected)
.venv/bin/pytest -q

# Docker smoke test: builds, waits for healthy, checks the API basics and image hardening
scripts/docker-smoke.sh

# The image's minimal ffmpeg: a codec/container matrix plus the integration tests, run
# inside a copy of the built image. Run it after changing the ffmpeg configure flags.
scripts/docker-ffmpeg-check.sh

# Real-site checks: need the container running and internet access
docker compose up -d --build
.venv/bin/pytest -m network -v
```

The network tests read `SAVEME_BASE_URL` (default `http://127.0.0.1:8000`) and skip if `/health` is unreachable. Their URLs live in `tests/network/urls.py`; if a site removes a video, swap it there.

## License

See `LICENSE`.
