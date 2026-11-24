# Auto Shorts & Reels (Phase 3) — Design

Status: being implemented on `feature/auto-shorts`. Date: 2026-11-24. The code map is in
[AUTO_SHORTS_IMPLEMENTATION.md](AUTO_SHORTS_IMPLEMENTATION.md), and every preset value is in
[RENDERING_PRESETS.md](RENDERING_PRESETS.md).

Phase 3 adds **Auto Shorts** to Clip Studio. You pick clips (by hand or from AI Highlights),
a platform and a template, and SaveMe re-encodes each clip as a finished short video. It
reframes the clip (center, face tracking, speaker focus, split-screen, blurred background
or original), adds subtitles from the transcript, and can add optional branding. Then it
delivers each MP4, or one ZIP. Rendering runs locally with FFmpeg and a small face
detector. OpenRouter stays the only cloud service, and Auto Shorts never calls it.

## 1. Audit of the starting point

### 1.1 Repository state (verified 2026-11-24)

- `main` = `origin/main` = `2edc86b` "Merge branch 'feature/ai-clipping'". Phase 2 is fully
  merged: `feature/ai-clipping` (`fa43b51`) is an ancestor of `main`, and the working tree was clean.
- Two old branches aren't merged into `main`: `feature/download-queue` and
  `feature/security-hardening`. Their unmerged commits change only the journal and a
  download-queue design document. They hold no code, so the baseline doesn't need them.
- `feature/auto-shorts` was cut from `2edc86b`.
- Baseline suite on `feature/auto-shorts` before any change: `.venv/bin/pytest -q` → **932 passed,
  2 skipped, 8 deselected** (108 s), the same as the end of Phase 2.
- **OpenRouter:** there is no `OPENROUTER_API_KEY` in this environment, and `.env` holds only
  `ALLOWED_HOSTS` and `SAVEME_PORT`. The integration is checked by its mocked suite
  (`tests/unit/test_openrouter.py`, `tests/integration/test_ai_*`). A live call can't be made here,
  as in Phase 2. Auto Shorts doesn't depend on OpenRouter. It uses whatever highlights or marks
  the user already has, and the local transcript.

### 1.2 What exists and how Phase 3 uses it

| Area | What exists | Phase 3 use |
| --- | --- | --- |
| `app/jobs.py` `JobManager` | Sources, clips, batches, shared download slots, disk budget, `hold_source`, `add_source_listener`, `reserve_disk`. | Renders *hold* their source and *reserve* disk exactly as analyses do. Renders never take a download slot or queue place. |
| `app/runner.py` `run_worker` | JSON-over-stdio subprocess runner with process-group kill, timeout and workdir size watchdog. | Runs the new renderer worker unchanged. |
| `app/bin/ffmpeg` wrapper | Pins `-protocol_whitelist file,pipe`. | Every renderer FFmpeg call goes through it. |
| `app/transcriber.py` | faster-whisper over 300 s windows, segment timestamps only. | Gains optional **word timestamps** (`WHISPER_WORD_TIMESTAMPS`, default on). Older cached transcripts stay valid. |
| `app/transcript.py` | Cached per source at `<source>/ai/transcript.json`. | Captions come from it: no retranscription when it covers the clip. |
| `app/ai_jobs.py` | Analyses, including `transcribe_only` (works without an OpenRouter key). | The Auto Shorts tab starts a transcribe-only analysis when a clip has no transcript yet. |
| `static/studio.js` bridge | `onSource`, `loadRange`, `addRanges`, `exportRange`, `seek`. | Gains `clips()` and `onClips()` so Auto Shorts can list the clip list's rows. |
| Docker FFmpeg | `--disable-everything`, stream copy, audio decoders, MP3 and PCM encoders, no swscale, no video encoders, `--disable-network`. | Gains exactly what rendering needs. See §3. |

### 1.3 Measurements on this machine (Apple M2, 8 GB)

These cover 30 s of 1080×1920 at 30 fps, cropped from a 1080p source, with 2 encoder threads
(`/usr/bin/time -l`):

| Encoder | Wall time | Peak RSS | Size |
| --- | --- | --- | --- |
| libx264 `ultrafast` CRF 21 | 2.8 s | 162 MB | 31.5 MB |
| libx264 `veryfast` CRF 21 | 5.9 s | 308 MB | 11.2 MB |
| libx264 `faster` CRF 21 | 7.9 s | 376 MB | 11.6 MB |
| libx264 `medium` CRF 21 | 11.1 s | 508 MB | 11.7 MB |
| `h264_videotoolbox` 8 Mb/s | 4.8 s | 183 MB | 28.1 MB (CPU ~1.5 cores vs ~3.6) |

So `veryfast` gives the best size and speed balance at about 5× real time. `medium` costs nearly
twice the time for no size gain at this quality. Even `medium` fits a 1.5 GiB worker cap with room
to spare.

## 2. Architecture

```
Browser: Clip Studio "Auto Shorts" tab (static/shorts.js)
  │  GET  /api/shorts/options                    templates, platforms, styles, encoders, limits
  │  GET  /api/sources/{id}/shorts/captions      cues for a range (from the cached transcript)
  │  POST /api/sources/{id}/shorts/assets        logo PNG / intro / outro upload
  │  POST /api/sources/{id}/shorts/previews      a preview still (JPEG) or low-res clip
  │  POST /api/sources/{id}/renders              render 1..20 clips
  │  GET  /api/renders/{id}  (poll)              per-clip stage, measured %, estimated time left
  │  GET  /api/renders/{id}/clips/{n}/file       one MP4 (or .srt / .ass)
  │  GET  /api/renders/{id}/zip                  every finished clip (+ subtitles)
  ▼
FastAPI process: RenderManager (app/render_jobs.py)
  - its own slot (RENDER_MAX_CONCURRENT_JOBS = 1) and queue bound; previews have a separate slot
  - holds the source; reserves disk; clips one at a time; cancel = kill the worker's process group
  - resolves the spec: template ⊕ platform ⊕ user overrides → RenderSpec (pydantic, strict)
  - captions: app/captions.py turns transcript words into cues → ASS (+ SRT)
  ▼ run_worker (JSON over stdio)
Renderer worker: python -m app.renderer   (RLIMIT_DATA cap, nice 10, deny-all connect guard)
  analyze : FFmpeg → sampled frames (rawvideo pipe) → YuNet faces (ONNX Runtime) + mouth motion
            + scene cuts → tracks JSON (cached per source and range)
  render  : app/reframe.py crop path → app/render_graph.py filtergraph → FFmpeg (libx264 or
            VideoToolbox) → MP4, checked with ffprobe
  frame   : the same graph for one frame → JPEG preview
  asset   : probe a logo/intro/outro upload
```

Files: `<source folder>/render/` holds `analysis/<key>.json` (cached face analysis),
`assets/<asset id>.<ext>`, `previews/<id>/`, and `<render id>/` (clip MP4s, subtitles, and a
ZIP when one is asked for). Like AI files, they end with the source. The orphan sweep only
looks at top-level `TEMP_DIR` entries, so it never touches them.

### 2.1 Why a separate worker and manager

- **A separate process** (as with the transcriber) gets a memory cap sized for decoding and
  encoding. The download worker's 1 GiB hard cap can't be raised later, and `medium` x264 at
  1080×1920 peaks at ~0.5 GB. It also gets a connect guard that refuses *every* connection,
  and a lower CPU priority than downloads.
- **A separate manager and semaphore** let a 10-minute batch render never hold up a download,
  and a download queue never block a render. This is the same choice Phase 2 made for analyses.
- **Sequential clips:** one worker run per clip, one clip at a time. This gives per-clip
  status, per-clip retries and per-clip cancellation, and only one FFmpeg encodes at once.

## 3. FFmpeg build: encoders, licensing, size and security

### 3.1 What the Docker build gains

| Component | Why | Licence |
| --- | --- | --- |
| `--enable-gpl --enable-libx264`, encoder `libx264` | H.264 encoding (Debian `libx264-164`). | GPL-2.0+ |
| encoder `aac` (FFmpeg native) | AAC audio. | LGPL-2.1+ |
| swscale (drop `--disable-swscale`) | `scale`, pixel-format conversion. | LGPL |
| `--enable-libass` (+ libfreetype, fribidi, harfbuzz, fontconfig via Debian) | Burned-in ASS subtitles with complex-script shaping (Malayalam). | ISC, FTL, LGPL, MIT |
| `--enable-libdav1d`, decoder `libdav1d` | AV1 sources (uploads; some sites). | BSD-2 |
| decoders `h264, hevc, vp8, vp9, png` | Decoding sources (vp9 was already there) and the logo. | LGPL |
| encoder `mjpeg`, muxer `image2`, demuxer `png_pipe` | JPEG preview stills; a single-image logo input (`png_pipe` reads exactly one image and has no file-pattern logic, unlike `image2` as a demuxer). | LGPL |
| filters | `crop, scale, pad, setsar, fps, format, split, overlay, vstack, boxblur, eq, colorchannelmixer, ass, trim, atrim, setpts, asetpts, concat, color, anullsrc, apad, aresample, aformat, anull, null, settb` | LGPL (+ GPL for some) |

Nothing network-related changes: `--disable-network` stays, the only protocols are still `file`
and `pipe`, and no demuxer that opens other files (HLS, DASH, concat, image2 patterns) is
enabled. The wrapper still pins the protocol whitelist.

**Licensing.** With `--enable-gpl --enable-libx264`, the FFmpeg binary in the image is
GPL-2.0-or-later. SaveMe stays MIT. It runs FFmpeg as a separate program and never links it, so
the two are an aggregate. Building the image yourself creates no obligation. **Publishing the
image** means offering the corresponding source of the GPL parts. The Dockerfile already pins the
exact FFmpeg tarball (SHA-256 checked) and installs x264 from Debian, whose source package
(`x264 2:0.164.3108+git31e19f9-2`) is public. H.264 and AAC are patent-encumbered formats. SaveMe
is meant for personal use, and anyone redistributing encoders commercially should check their own
patent position. **Rejected alternative:** Cisco's OpenH264 (BSD). Its patent cover applies only to
Cisco's own prebuilt binaries, not a source build. It is also Constrained Baseline only, with much
weaker rate control, and needs about 1.5–2× the bitrate for the same quality.

**Native runs** (no Docker) use the system FFmpeg. On macOS, Homebrew's build has libx264,
libass with HarfBuzz, and VideoToolbox. `/api/shorts/options` reports what the running FFmpeg
can do. Auto Shorts turns itself off with a clear message when `libx264` (or VideoToolbox),
`aac` or the `ass` filter is missing, and everything else keeps working.

**Hardware acceleration.** `RENDER_ENCODER=auto` uses `h264_videotoolbox` when the FFmpeg
lists it (macOS native runs) and libx264 otherwise. If a hardware encode fails for any reason,
that clip is re-rendered once with libx264, and the result says which encoder made it.
Docker on macOS can't reach VideoToolbox (the Linux VM has no Apple media engine), and no
VAAPI/NVENC/QSV is enabled. Those would need device passthrough, which `cap_drop: ALL` and
the threat model rule out.

**Size.** Measured after the build (see the journal). Expected: about 5 MB of shared libraries
(x264, libass, freetype, harfbuzz with glib/graphite2, fribidi, fontconfig, dav1d), about 9 MB
of fonts, and a slightly larger static FFmpeg.

### 3.2 Security review of the new attack surface

1. **More decoders on untrusted media** (H.264, HEVC, VP8, AV1, PNG). These are the most
   exercised parts of FFmpeg, but still parsers of hostile input. They run only in the renderer
   worker. That worker has an `RLIMIT_DATA` cap (`RENDER_MAX_MEMORY_MB`, enforced on Linux), a
   deny-all connect guard, its own process group, a timeout and a workdir size watchdog. It runs
   as the unprivileged `app` user in a container with `cap_drop: ALL`, `no-new-privileges` and
   `pids_limit`.
2. **No user text reaches FFmpeg's command line or filtergraph.** Graphs are built only from
   validated numbers and enums. Files are referred to by fixed relative names inside the
   worker's own folder (`subs.ass`, `logo.png`), with the worker's cwd set there, so no path is
   ever escaped into a filtergraph.
3. **libass only reads ASS files SaveMe writes.** Transcript and user text is cleaned
   (`clean_text`) and ASS-escaped: `{`, `}` and `\` can't open override blocks or form `\N`/`\h`.
   Font selection uses installed system fonts, or `RENDER_FONTS_DIR`, a path validated at startup
   to contain only safe characters.
4. **Uploads (logo, intro, outro)** have their own small size caps and Content-Type check. The
   logo's PNG signature and IHDR (dimensions ≤ 4096) are checked in the API process before FFmpeg
   sees it. Video assets are probed by the renderer worker like a source upload.
5. **Request bodies.** Render requests may carry edited captions for 20 clips, which can be over
   the Guard's 64 KiB cap. The render route reads its own body with a 2 MiB cap, like the subtitle
   import, then validates it strictly (`extra="forbid"`, bounded lists and strings).
6. **Face detection** runs YuNet (an MIT-licensed ONNX model from OpenCV Zoo, 232 KB, vendored
   with its licence and SHA-256 pinned in a test) in ONNX Runtime. ONNX Runtime is already in
   the image for faster-whisper's VAD. Frames reach it as raw pixels from the hardened FFmpeg,
   never as media files.
7. **Network restrictions are unchanged.** Nothing in Phase 3 opens a connection, and the
   renderer process refuses any it tries.

### 3.3 Face detection: why not the OpenCV or MediaPipe packages

| Option | Verdict |
| --- | --- |
| **YuNet ONNX on ONNX Runtime** (chosen) | Same model as OpenCV's `FaceDetectorYN`, and accurate on frontal and partly turned faces at 640 px. It returns five landmarks (eyes, nose, mouth corners), which the speaker heuristic uses. It adds 0.23 MB and no new package, because ONNX Runtime ships with faster-whisper. The prior decoding (about 40 lines of NumPy) is checked against `cv2.FaceDetectorYN` by an opt-in test when OpenCV is installed. |
| `opencv-python-headless` 5.1 | 36–64 MB wheels (about 100 MB+ installed), and they **bundle their own FFmpeg build**: a second, unhardened copy in the image. Every OpenCV feature we'd use (resize, colour conversion, frame differences) is done by the hardened FFmpeg or by NumPy. Not justified. |
| MediaPipe | Pulls in jax/jaxlib, matplotlib and `opencv-contrib-python` (GUI build, needs libGL in a slim image), several hundred MB, with brittle pins. Not justified. |
| Haar cascades | Frontal-only, many false positives, no landmarks. |

## 4. Rendering engine

### 4.1 Output formats

| Aspect | Default size | Platforms |
| --- | --- | --- |
| 9:16 vertical | 1080×1920 | YouTube Shorts, Instagram Reels, TikTok |
| 4:5 portrait | 1080×1350 | Instagram / Facebook feed |
| 1:1 square | 1080×1080 | Feed, LinkedIn |
| 16:9 landscape | 1920×1080 | YouTube, X |

A custom size can be any even width and height from 240 to 2160 (at most 2160×3840), in one of
the four aspects or the source's own. FPS: `source` (capped at 60) or 24, 25, 30, 50, 60.
Quality presets (RENDERING_PRESETS.md):

| Preset | x264 | VideoToolbox | Audio | Use |
| --- | --- | --- | --- | --- |
| `draft` | `ultrafast` CRF 30, size ÷2 | 2.5 Mb/s at ÷2 | AAC 96k | previews |
| `standard` | `veryfast` CRF 21 | 10 Mb/s (1080×1920) | AAC 160k | default |
| `high` | `medium` CRF 18 | 16 Mb/s | AAC 192k | final uploads |

Every output is H.264 High (`yuv420p`, BT.709 tags), AAC LC 48 kHz stereo, MP4 with
`+faststart`, a 2-second GOP, and `-maxrate`/`-bufsize` caps so platforms don't re-encode
unusually. Each output is checked with ffprobe: codecs, size, duration within one frame plus
0.1 s, and `yuv420p`.

### 4.2 Resource limits

- `RENDER_MAX_CONCURRENT_JOBS` (1): renders at once. A separate single preview slot keeps
  stills responsive while a batch runs.
- `RENDER_CPU_THREADS` (2): passed to the decoder (`-threads`), the filtergraph
  (`-filter_complex_threads`) and x264 (`threads`, `lookahead_threads`). The worker runs at nice 10.
- `RENDER_MAX_MEMORY_MB` (1536): the worker's `RLIMIT_DATA`, inherited by FFmpeg.
- `RENDER_MAX_CLIP_SECONDS` (300), `RENDER_MAX_JOB_SECONDS` (3600) per clip run.
- `RENDER_MAX_QUEUED_JOBS` (4): renders waiting for the slot. More are refused with `busy`.
- Disk: each clip reserves `max(64 MiB, 1.5 × bitrate cap × length)` through
  `JobManager.reserve_disk` while it renders. FFmpeg's `-fs` stops at `MAX_FILE_SIZE_MB`. A
  ZIP reserves the size of its entries.
- Frames are never held in Python beyond the one being analysed. Rendering is FFmpeg streaming
  from disk to disk. Analysis decodes at 5 frames a second into a 640×640 box (1.2 MB a frame).

### 4.3 Progress, cancellation and cleanup

- **Measured:** FFmpeg's `out_time_us` over the clip's output length, for rendering, and frames
  done over frames expected, for analysis.
- **Estimated:** the batch's percent, weighted by clip length, and the time left, extrapolated
  from the measured rate. The API labels each (`percent_measured`, `eta_estimated`).
- **Cancel:** of the batch, or of one queued or running clip. The worker's process group is
  killed. A part file (`*.part.mp4`) is never served and is removed when its clip ends in any
  state. Finished clips stay downloadable.
- **Cleanup:** the render folder goes when the source ends (close, idle, replaced), when the
  render record is pruned, or at shutdown. Each source keeps at most 4 finished renders, and the
  oldest is removed when a fifth starts.

## 5. Reframing (`app/reframe.py`, `app/faces.py`)

### 5.1 Modes

| Mode | What it does | Falls back to |
| --- | --- | --- |
| `center` | Fixed crop of the target aspect at the frame's centre (or at the manual position). | — |
| `face` | Tracks detected faces. If every face fits inside the crop window less the safe margins, they are framed together; otherwise it follows the *primary* face (largest × most persistent × most central, with hysteresis). | the template's fallback (`center` or `blur`) when no face is found for > 1.5 s |
| `speaker` | Like `face`, but while speech is happening (transcript words or segments) it prefers the face whose **mouth region moves most**. A switch needs a clear lead held for ≥ 1 s, and shots last at least 1.5 s. | `face` when only one face is visible or nobody is speaking |
| `split` | Two panels stacked vertically, each following one of the two most persistent faces (left→top, right→bottom). | left/right halves of the frame when fewer than two faces are tracked |
| `blur` | The whole frame scaled to fit, centred over a blurred, darkened, cover-scaled copy. | — |
| `original` | The whole frame scaled to fit, padded with black or the brand colour. With output aspect `source`, there's no padding at all. | — |

**Honesty about speakers.** `speaker` is a *visual* heuristic: the moving mouth during
speech. SaveMe does no diarisation and never matches voices to faces. The UI calls it "Speaker
focus (best effort)", and captions are never labelled with speaker names automatically.
Labels can be typed in the caption editor.

### 5.2 Face analysis

1. FFmpeg decodes `[start, end)` at `RENDER_ANALYSIS_FPS` (5) frames a second, scaled into
   640×640 (letterboxed), as raw BGR into a pipe.
2. A second, cheap pass at the full frame rate, 64×36 grey, finds **scene cuts** from frame
   differences. A cut is a frame whose mean absolute difference from the one before is over a
   threshold and over 3× the local median. Cuts end tracks and let the crop jump exactly at the
   cut, where a jump is invisible.
3. YuNet (score ≥ 0.6, NMS IoU 0.3) gives boxes and 5 landmarks per frame. Faces smaller than
   2 % of the frame height are ignored.
4. Detections are linked into tracks by IoU and centre distance. A track survives gaps of up
   to 1 s.
5. Mouth activity: the mean absolute difference of the grey mouth patch (under the nose,
   between the mouth corners) between consecutive samples, normalised by face size.
6. The result is cached as JSON per (source, start, end, fps, version), so changing templates
   or styles never re-analyses.

### 5.3 Crop path and smoothing

From the per-sample target centre:

- **Dead zone:** the camera doesn't move while the subject stays within the central 30 %
  (`deadzone`) of the window. Most talking-head shots are then perfectly still.
- **Smoothing:** a centred moving average (0.8 s) after a median filter (removes single-frame
  detection jumps), then a **velocity cap** of 35 % of the window width per second and an
  acceleration cap. Easing is never sudden.
- **Segments:** each scene is smoothed on its own. At a cut, the path jumps.
- **Clamping:** the window always stays inside the frame. Faces are kept inside the window less
  the safe margin (`margin`, 0–0.25 of the window, default 0.08) whenever they fit.
- **Simplification:** the path is reduced with Ramer–Douglas–Peucker (tolerance 1 px) to a
  few dozen keyframes, and given to FFmpeg's `crop` as a piecewise-linear expression of `t`
  (a flat sum of `clip()` terms, evaluated per frame).
- **No face:** hold the last position for 1.5 s, then ease to the fallback.
- **Manual override:** `manual: {x, y, zoom}` (0–1 centre and 1–3× zoom) replaces tracking with
  a still crop at that position for the whole clip.
- **Avoid unnecessary cropping:** the crop never zooms in (window height = frame height for
  landscape to vertical). A source already within 10 % of the target aspect is scaled, not
  cropped. When the faces in a shot can't fit (two people far apart in 9:16), `face` follows
  the primary face, and templates for multi-person content default to `split` or `speaker`
  instead.

## 6. Subtitles (`app/captions.py`)

### 6.1 Words and timing

- Whisper transcripts made from now on store **word timestamps** (faster-whisper
  `word_timestamps=True`) as optional `words: [{start, end, text}]` on each segment.
  `WHISPER_WORD_TIMESTAMPS=false` turns them off. Existing cached transcripts (no words) and
  imported subtitles stay valid.
- When a segment has no words, word times are **estimated**: the segment's time is shared out by
  each word's spoken length, approximated by its letters (grapheme count, combining marks
  excluded). The captions response says `timing: "words"` or `"estimated"`, and the UI offers
  "Re-transcribe for exact word timing".
- Only cues overlapping the clip are used, clipped to it and shifted so the clip starts at 0.

### 6.2 Segmentation

Words are grouped into cues by the style's rules: at most `max_words`, at most
`max_chars_per_line` × `max_lines` characters (graphemes), at most `max_duration` (default
3.5 s). A cue breaks at a sentence end, or a pause ≥ 0.6 s. Lines are balanced, so the two
lines of a cue are of similar length. A cue lasts at least 0.6 s, unless the next one starts
first, and never overlaps the next. Malayalam (and other scripts) break only at spaces, never
inside a grapheme cluster. Line widths count graphemes with a per-script width factor.

### 6.3 Formats and placement

- **SRT** (UTF-8, `HH:MM:SS,mmm`), and **ASS** with `PlayResX/Y` equal to the output size, so
  style sizes are in output pixels. `WrapStyle: 2` is set, because SaveMe breaks lines itself.
  `ScaledBorderAndShadow: yes`, `YCbCr Matrix: TV.709`.
- **Position:** bottom, middle or top, plus a vertical offset, inside the platform's safe area.
  9:16 keeps captions clear of the bottom 20 % (platform UI) and the right 12 % (action buttons)
  by default. Split-screen templates put captions on the seam between panels.
- **Burned in, or not:** `captions.burn` (default on). `captions.export` adds `.srt` and/or `.ass`
  files beside each MP4, and inside the ZIP. A subtitle-free export is `enabled: false`.
- **Manual correction:** the editor lists a clip's cues. Text, start and end can be changed,
  and cues can be split or removed. The edited cues travel in the render request, are validated
  (sorted, in range, ≤ 200 characters, no overlap), and replace the automatic ones for that clip.
- **Speaker labels:** only typed by the user (`"speaker"` per cue, ≤ 32 characters). They are
  shown as a prefix in the style's accent colour.
- **Escaping:** text is `clean_text`ed. Then `\` → `/`, `{` → `(`, `}` → `)` before it enters ASS.

### 6.4 Styles

`clean_minimal`, `bold_creator`, `karaoke`, `podcast_classic`, `cinematic`, `high_contrast`. Each
is data: font preset, size (in output px for a 1080-wide frame, scaled to the output), colours
(text, outline, shadow, box, highlight), outline and shadow widths, box (opaque background or
not), uppercase, word highlight (`none`, `color`, `box`), segmentation rules and default
position. Every field can be overridden per request within validated ranges. Word highlighting
(`karaoke`, `bold_creator`) emits one event per word, with the active word recoloured. The cue
stays still, so highlighting never reflows the line.

### 6.5 Fonts

Font *presets* map to families: `sans` → Roboto, `heavy` → Roboto Black, `condensed` → Roboto
Condensed, `serif` → DejaVu Serif, and `malayalam` → Manjari (with Noto Sans Malayalam,
Malayalam Sangam MN and Malayalam MN as fallbacks). A cue whose text is mostly Malayalam uses the
Malayalam family. libass shapes it with HarfBuzz, and fontconfig falls back per glyph.
Licences: Roboto Apache-2.0, DejaVu (Bitstream Vera licence), Manjari OFL-1.1. All are installed
from Debian packages in Docker. No font file is committed and no commercial font is used. Native
runs use whatever fontconfig finds. `RENDER_FONTS_DIR` adds a folder.

## 7. Templates and branding

Templates are declarative data in `app/shorts_templates.py`, served by `/api/shorts/options`,
and resolved server-side: template ⊕ platform ⊕ user overrides. Each defines `aspect`,
`layout` (mode, fallback, margin, deadzone), `captions` (style, position, offset), `safe_area`
and `branding` slots (logo corner, title bar).

| Template | Aspect | Layout | Captions |
| --- | --- | --- | --- |
| `talking_head` | 9:16 | `face` → `center` | `bold_creator`, lower third |
| `interview` | 9:16 | `speaker` → `face` | `podcast_classic`, bottom |
| `podcast_split` | 9:16 | `split` | `podcast_classic`, on the seam |
| `educational` | 9:16 | `blur` (slides stay whole) | `clean_minimal`, bottom |
| `news_commentary` | 9:16 | `blur` + title bar | `high_contrast`, bottom |
| `full_frame_blur` | 9:16 | `blur` | `clean_minimal`, bottom |

Branding is **off by default**, and SaveMe never adds its own watermark. Optional:

- a **logo** (PNG, ≤ 2 MB, ≤ 4096 px), in a corner, at 5–40 % of the width, 10–100 % opacity;
- a **watermark text** (≤ 60 characters, drawn by libass in a corner);
- a **title bar** (`news_commentary`: the clip's title over a brand-colour band);
- a **brand colour** (`#RRGGBB`), used for the padding, the title bar and the default highlight;
- a **font preset**;
- an **intro and outro** (MP4/MOV/WebM, ≤ 100 MB, ≤ 15 s each), scaled and padded to the output,
  with silent audio added if they have none, and joined with FFmpeg's `concat` filter.

**Saved presets are local**: the browser keeps them (`localStorage`, `saveme.shorts.presets.v1`,
at most 20, with the logo as a data URL of ≤ 512 KB). They can be exported to and imported from
a JSON file. The server never stores presets. It validates every render request as untrusted,
so a tampered preset can only produce a refused request. Intro and outro files aren't saved in
presets (too large for browser storage). A preset remembers that it had them, and asks for
them again.

## 8. API contracts

All routes sit behind the existing Guard (host allow-list, same-origin POST, security headers).
Errors use the standard `{"error": {"code", "message"}}` shape.

| Route | Body / query | Answer |
| --- | --- | --- |
| `GET /api/shorts/options` | — | `{available, error, encoder: {name, hardware}, templates[], platforms[], qualities[], styles[], fonts[], limits{}}` |
| `GET /api/sources/{id}/shorts/captions` | `start, end, style?` | `{captions: null}` with no transcript, else `{timing, language, cues:[{start,end,text,words:[…]}]}` |
| `POST /api/sources/{id}/shorts/assets?kind=logo\|intro\|outro` | raw bytes | `{asset_id, kind, width, height, duration}` |
| `POST /api/sources/{id}/shorts/previews` | `{clip:{start,end}, spec, at, kind: "frame"\|"clip"}` | 202 `{preview_id}` |
| `GET /api/shorts/previews/{id}` | — | `{status, stage, progress_percent, error}` |
| `GET /api/shorts/previews/{id}/file` | — | `image/jpeg` or `video/mp4` |
| `POST /api/sources/{id}/renders` | `{clips:[{start,end,title?,captions?,manual?}], spec}` | 202 `{render_id}` |
| `GET /api/renders/{id}` | — | status, clips[] (stage, percent, `percent_measured`, `eta_seconds`, `eta_estimated`, encoder, size, width, height, duration, error, files), totals |
| `POST /api/renders/{id}/cancel` | — | the view |
| `POST /api/renders/{id}/clips/{n}/cancel` | — | the view |
| `GET /api/renders/{id}/clips/{n}/file?kind=mp4\|srt\|ass` | — | attachment |
| `GET /api/renders/{id}/zip?subtitles=true` | — | attachment `application/zip` |

`spec` (all optional except `platform` or `aspect`):

```json
{"template": "talking_head", "platform": "youtube_shorts", "aspect": "9:16",
 "width": 1080, "height": 1920, "fps": "source", "quality": "standard",
 "layout": {"mode": "face", "fallback": "center", "margin": 0.08, "deadzone": 0.3},
 "captions": {"enabled": true, "burn": true, "export": ["srt"], "style": "bold_creator",
              "font": "heavy", "size": 72, "color": "#FFFFFF", "highlight": "#FFD400",
              "outline": 6, "shadow": 2, "position": "bottom", "offset": 0.0,
              "uppercase": true, "max_words": 3},
 "branding": {"logo": "<asset id>", "logo_position": "top-right", "logo_scale": 0.15,
              "logo_opacity": 0.9, "watermark": "", "brand_color": "#E5484D",
              "intro": null, "outro": null, "title_bar": false}}
```

## 9. Lifecycle

```
render: queued → (per clip: waiting → analyzing → rendering → ready | failed | cancelled)
        → ready (every clip ended; some may have failed) | failed (none ready) | cancelled
preview: queued → analyzing → rendering → ready | failed | cancelled
```

A render holds its source from start to end. Closing the source cancels the render, and its
listener runs before the folder is removed. At most one render is pending per source, and
previews are separate. Shutdown cancels everything. Records live in memory: at most 50, each
kept an hour after it ends, and a restart forgets them (the files go with `TEMP_DIR`).

## 10. UI (Clip Studio → "Auto Shorts" tab)

1. **Clips:** the clip list's rows (from Manual or AI Highlights) with checkboxes, plus "Use
   current marks". It's empty until clips exist, and says how to add them.
2. **Format:** platform, template, quality, FPS.
3. **Framing:** mode (the template's by default), safe margin, and a manual position override
   (X, Y, zoom).
4. **Captions:** on/off, style, size, colours, position, uppercase, words per cue, burn in or
   files only. Transcript status, with "Transcribe" when none exists. A cue editor per clip.
5. **Branding** (collapsed, off): logo, watermark, brand colour, font, intro/outro.
6. **Presets:** save, load, delete, export, import (local only).
7. **Preview:** a server-rendered still at a chosen moment (exactly what the render does,
   Malayalam shaping included), and "Preview clip" (a low-res draft MP4).
8. **Render:** per-clip status rows with stage, measured % and estimated time left, Cancel, and
   per clip MP4/SRT downloads, then "Download all (ZIP)".

Text from the server is set only with `textContent`. Controls are labelled, progress uses
`role="progressbar"`, the layout works at 375 px, and dark mode follows the page.

## 11. Configuration (new, all optional)

Loaded apart from the download settings. An invalid value turns off Auto Shorts only.

| Variable | Default | Range |
| --- | --- | --- |
| `RENDER_MAX_CONCURRENT_JOBS` | 1 | 1–4 |
| `RENDER_MAX_QUEUED_JOBS` | 4 | 0–20 |
| `RENDER_CPU_THREADS` | 2 | 1–16 |
| `RENDER_MAX_MEMORY_MB` | 1536 | 256–16384 |
| `RENDER_ENCODER` | auto | auto, libx264, h264_videotoolbox |
| `RENDER_MAX_CLIP_SECONDS` | 300 | 5–1800 |
| `RENDER_MAX_JOB_SECONDS` | 3600 | 60–86400 |
| `RENDER_ANALYSIS_FPS` | 5 | 1–15 |
| `RENDER_FONTS_DIR` | (none) | absolute path of `[A-Za-z0-9_./-]` |
| `WHISPER_WORD_TIMESTAMPS` | true | true/false |

## 12. Testing

- **Unit:** geometry for every aspect × source orientation, crop bounds and margins, smoothing
  (no step larger than the velocity cap, except at cuts), RDP, expression evaluation,
  segmentation (word limits, line balance, Malayalam graphemes, pauses), SRT/ASS output and
  escaping, styles, template resolution and validation, invalid presets, unsafe values, YuNet
  decoding against fixtures, encoder argument selection, settings.
- **Integration (real FFmpeg, checked with ffprobe):** each aspect's size, codecs, `yuv420p`,
  duration, and FPS; each layout; burned captions visible inside cue times and absent outside;
  Malayalam rendering where a Malayalam font exists (no "glyph not found" from libass, ink in
  the caption box); logo overlay; intro/outro concat; cancellation leaving no part files; disk
  limits; face analysis on fixtures with zero, one and two faces (public-domain portraits);
  previews; the renderer's network block.
- **Manager and API:** lifecycle, sequential clips, per-clip and batch cancel, source close,
  ZIP integrity (`testzip`, entries, sizes), missing transcripts, a transcript reused with no
  retranscription, bodies over the cap, unsafe asset uploads, the downloader, manual clipping
  and AI regressions (the existing suites).
- **Timing against real speech (opt-in, macOS `say` + Whisper `base`):** words spoken one by one
  with known gaps, so the ground-truth onsets come from the audio itself. Whisper word starts
  must be within 0.25 s median and 0.5 s worst. Then the rendered MP4 must show the caption on
  frames inside each word's cue.
- **Docker:** the FFmpeg check gains rendering cases (x264+AAC, libass Malayalam, dav1d, png logo,
  JPEG still). The smoke test renders a 9:16 short in the container and checks it with ffprobe.

## 13. Non-goals and limitations

- No diarisation or voice-to-face matching. Speaker focus is visual and best effort.
- No GPU encoding in Docker. VideoToolbox is for native macOS runs only.
- No animated caption effects beyond word highlighting (no bounce or emoji insertion).
- Presets are per browser, and not synced.
- Renders end with their source, like AI results.
