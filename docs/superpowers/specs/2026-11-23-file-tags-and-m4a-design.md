# File tags, cover art and M4A audio: Design

**Date:** 2026-11-23
**Builds on:** [`2026-11-09-media-url-downloader-design.md`](2026-11-09-media-url-downloader-design.md). This document records only what changes.
**Size:** small change (short design).

## 1. Intent

Saved files carry no information about themselves. An MP3 opens in a music app as "Unknown Artist" with a blank cover, and an MP4 has no title, date or chapters. The only audio choice is MP3, which re-encodes audio that is usually already AAC or Opus, so quality is lost a second time. And "Best" says only its resolution, not whether it will play in QuickTime.

The owner's files go to more than one place: sometimes a music app on a phone, sometimes just a folder. So files should look right in a music app and do no harm in a folder.

Idea source: Ghost-Downloader-3's yt-dlp module (embed metadata, embed chapters, honest format labels). Ideas only; that project is GPLv3 and none of its code is used.

## 2. Goals

- **Tags.** Every MP4, MP3 and M4A gets title, artist (channel/uploader), date and description tags, and chapters where the source has them.
- **Cover art.** Every MP3, M4A and MP4 gets the source's thumbnail as cover art when a JPEG or PNG thumbnail is available.
- **M4A (original).** A new audio choice that keeps the source's AAC audio as it is (stream copy, no re-encode), offered only when the source has AAC audio.
- **Honest "Best" label.** "Best" shows resolution, frame rate when above 30, and codec, e.g. "Best (2160p60 · AV1/VP9)".

## 3. Non-goals

- An audio language picker, subtitles, playlists, clips.
- Codec or frame-rate labels on the 1080p, 720p and 480p choices. They already prefer H.264 by design.
- Converting non-AAC audio to AAC. That would be lossy-to-lossy, like MP3, and needs an AAC encoder the build doesn't have.
- Cover art for the MKV or WebM fallback files (the "couldn't convert to MP4" case). mutagen can't write those.
- Settings or toggles. Tags and covers are always on. *(Designer's call: SaveMe has no settings page, and these do no harm in a folder.)*

## 4. Anti-goals: a failure even if it works

- A missing, blocked, oversized or broken thumbnail fails the download. Cover art is best effort: on any problem the file is delivered without a cover, and the job doesn't fail or change its error code.
- FFmpeg reading an image. The build gains no image demuxers, decoders or encoders.
- Any fetch outside the worker's connection guard. The thumbnail is fetched by yt-dlp inside the worker, like everything else.
- The source link in the file. yt-dlp writes `webpage_url` to the `purl` and `comment` tags by default. SaveMe must not, because a shared file would reveal where it came from. *(Designer's call.)*
- Breaking existing clients or stored jobs. A request with `output: "audio"` still means MP3, and a job stored in `sessionStorage` before the upgrade still loads.

## 5. Approaches (owner chose B)

| | Approach | Trade-off |
|---|---|---|
| A | Tags and chapters only, no cover art | Smallest. Music apps show a blank cover. |
| **B** | **Tags and chapters, plus cover art embedded by SaveMe's own small step using mutagen** (already installed as a yt-dlp dependency). JPEG or PNG thumbnails only. | One small module and its tests. FFmpeg never reads an image. |
| C | Tags and chapters, plus yt-dlp's built-in `EmbedThumbnail` | Least code, but FFmpeg would need JPEG, PNG and WebP demuxers and decoders and would parse images from the internet in the worker. That goes against the minimal FFmpeg build that README → Security relies on. |

**Chosen: B**, because it gets cover art without widening what FFmpeg parses.

## 6. Design

### Tags and chapters
- Run yt-dlp's `FFmpegMetadata` postprocessor (`add_metadata`, `add_chapters`; no info JSON) on every download.
- Blank the source-link tags, e.g. by setting `meta_purl` and `meta_comment` to empty strings so FFmpeg is told to clear them (yt-dlp's documented `--parse-metadata ":(?P<meta_purl>)"` technique). The description stays in `description`/`synopsis`.
- Chapters need FFmpeg's `ffmetadata` demuxer, which reads a text file yt-dlp writes in the job folder. Add `ffmetadata` to `--enable-demuxer` in the `Dockerfile`, and a chapters case to `scripts/docker-ffmpeg-check.sh`.
- The MP4 remux fallback (`_to_mp4`) must keep tags and chapters. FFmpeg copies them by default; a test proves it.

### Cover art
- After the download, fetch the thumbnail with the worker's guarded `ydl.urlopen` and keep it in memory. Read at most 5 MB, and try at most 3 URLs, best first.
  - *Changed while planning:* yt-dlp's `writethumbnail` streams to disk with no size cap, so an oversized "thumbnail" could push the job over its disk limit.
- Only thumbnails whose URL **path** ends in `.jpg`, `.jpeg` or `.png` are tried. YouTube's thumbnail URLs carry query strings. If none qualify, nothing is fetched.
- After the final file is known (after `_to_mp4`), a new step embeds the image with mutagen: ID3 `APIC` (front cover) for MP3, and `covr` for MP4 and M4A. Before embedding, check the bytes really are JPEG (`FF D8 FF`) or PNG (`89 50 4E 47`); otherwise skip.
- Nothing is written to the job folder for the cover.
- Any failure (no thumbnail, fetch blocked or failed, wrong type, mutagen error) is logged at debug level and skipped. A blocked thumbnail fetch must not turn a later, unrelated job error into `blocked_address`.
- A phase message, e.g. "Adding cover art", is optional and the builder's call.

### M4A (original)
- **Inspect:** the payload's `audio` object gains a flag (e.g. `m4a: true`) when any kept format has audio whose `acodec` starts with `mp4a`. When the codec is unknown, it's false.
- **Request:** the builder picks the API shape. The obvious one is a third `output` value, `"m4a"`. `output: "audio"` keeps meaning MP3.
- **Download:** select AAC audio only (e.g. `ba[acodec^=mp4a]/b[acodec^=mp4a]`), then `FFmpegExtractAudio` with `preferredcodec: "m4a"`, which stream-copies AAC into the `ipod` muxer, already in the build. If no AAC format can be selected, the job fails with `no_audio`.
- **Delivery:** `app/filenames.py` gains `m4a` → `audio/mp4`. The worker's `media.<ext>` rule already allows it.
- **Page:** a "M4A audio (original)" format choice after "MP3 audio", disabled or hidden when the flag is false, using the same chip template. The job kind label shows "M4A". `loadJob` accepts the new output value. The phase text is something like "Preparing M4A" instead of "Converting to MP3".

### "Best" label
- Computed at inspect from the kept video formats: the top short side, then the top frame rate at that size. The frame rate shows only when above 30 (e.g. "60", "50").
- The codec part lists the codec families present at that size and frame rate, in a fixed order, joined by "/" (AV1, VP9, H.265, H.264). Leave it out when any codec there is unknown.
  - YouTube 4K: "Best (2160p60 · AV1/VP9)"
  - A direct MP4: "Best (1080p · H.264)"
  - An unknown codec: "Best (1080p)"
- This is honest even when several codecs exist, because yt-dlp picks among exactly those.

## 7. Constraints

- No new dependencies: mutagen is already installed through `yt-dlp[default]`. No new FFmpeg components except the `ffmetadata` demuxer.
- The page's CSP rules (`tests/unit/test_page.py`) still apply. Any new Tailwind class must appear literally in `index.html` or `app.js`, and `static/dist/styles.css` must be rebuilt.
- **The planned download queue** (`feature/download-queue`, not yet built) rewrites the format and job-card code in `static/app.js`. Its card "format tag" must include M4A when it's built. Its non-goal "changing the job API" predates this change and isn't affected: it means the queue itself adds no API.

## 8. Docs

`README.md`:
- Features: tags, cover art and M4A.
- Usage: the M4A choice and the "Best" label.
- Security: note that the thumbnail is fetched through the same guard, is embedded without FFmpeg, and that the source link isn't written into files.
- Dockerfile: the FFmpeg list gains `ffmetadata`.

## 9. How to check it works

- **Unit tests:**
  - inspect flags M4A only for `mp4a` audio
  - the "Best" label cases above
  - `m4a` filename and media type
  - the cover step embeds JPEG and PNG, skips WebP and junk bytes, and never raises
- **Integration tests** (local media server, as in `tests/integration/test_worker_download.py`):
  - an MP4 and an MP3 have title and artist tags and no `purl` or `comment` source link
  - chapters survive into MP4
  - M4A from AAC is a stream copy (codec `aac`, same bitrate as the source)
  - M4A from an Opus-only source fails with `no_audio`
  - a cover is present when a JPEG thumbnail is served
  - a 404 or blocked thumbnail still delivers the file without a cover
- `scripts/docker-ffmpeg-check.sh` passes with the chapters case.
- **Real sites** (`pytest -m network`): a YouTube MP3 and M4A open in Apple Music or a phone player with title, artist and cover.
