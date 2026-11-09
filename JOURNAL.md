# Journal

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
