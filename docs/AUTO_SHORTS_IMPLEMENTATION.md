# Auto Shorts & Reels (Phase 3): Implementation notes

The design is in [AUTO_SHORTS_DESIGN.md](AUTO_SHORTS_DESIGN.md), and every preset value is in
[RENDERING_PRESETS.md](RENDERING_PRESETS.md). This file maps the design to the code, says how the
pieces talk, lists what is easy to get wrong, and says how each part is tested. Branch:
`feature/auto-shorts`.

## Code map

| File | What it holds |
| --- | --- |
| `app/renderer.py` | The renderer worker (`python -m app.renderer`): `main` (memory cap from the request, nice 10, deny-all connect guard), modes `render` (`video` or `frame`), `analyze` (in `app.faces`) and `asset` (probe a video; decode a logo with `app.png`). `probe_video` (displayed size after rotation, SAR, fps, audio), `parse_render` (strict request check), `canvas_size`, `render` (graph, encode, VideoToolbox → libx264 fallback, `_check_output` with ffprobe), `write_captions` (the ASS at the real canvas size). |
| `app/render_graph.py` | Pure filtergraph builder: `CropLayout`, `FitLayout` (blur or colour), `SplitLayout`, `LogoPlan`, `Bumper`, `GraphPlan` → `build()`. Inputs are 0 the source, then the logo (raw RGBA), intro, outro. Outputs `[vout]`/`[aout]`. |
| `app/layouts.py` | `LayoutRequest` (from the worker's JSON), `plan_layout()` (center/manual/blur/original/split halves, or a tracker for face/speaker/split). |
| `app/reframe.py` | Geometry: `crop_window`, `fit_size`, `source_canvas`, `nearly_same_aspect`; `Path` (piecewise-linear keyframes, cuts as repeated times), `path_expr` (a flat FFmpeg expression of `t`), `simplify` (RDP). |
| `app/faces.py` | `Detector` (YuNet on ONNX Runtime) and `decode` (OpenCV's FaceDetectorYN decoding in NumPy), `analyze` (one FFmpeg decode: detection frames plus tiny grey frames for `find_cuts`), track linking, mouth motion, `run_analyze`. Framing: `plan_face_layout`, `plan_split_layout`, `_window_paths` (dead zone, smoothing, speed cap, keep-in clamp, eased switches, jumps at cuts), `tracker_for`. |
| `app/models/` | `face_detection_yunet_2023mar.onnx` (OpenCV Zoo, MIT; SHA-256 in `faces.MODEL_SHA256`) and its licence. |
| `app/captions.py` | `clip_words` (word timestamps, or estimated within segments), `segment`, `balance_lines`, `fit_chars`, `cues_from_edits`, `to_srt`, `to_ass` (per-word highlight events, `\an`+`\pos` inside the safe area, Malayalam family, title bar, watermark), `escape_ass`. |
| `app/caption_styles.py` | The six styles and four font presets, as data. |
| `app/shorts_templates.py` | Platforms (with safe areas), qualities, FPS choices and the six templates, as data. |
| `app/render_spec.py` | Strict request models (`RenderSpec`, `RenderClip`, `Cue`, `RenderBody`, `PreviewBody`), `resolve()` → `ResolvedSpec`, `options()`. |
| `app/png.py` | The strict PNG decoder for logos (FFmpeg never parses an image). |
| `app/encoders.py` | `detect()` (cached `ffmpeg -encoders/-filters`), `Capabilities.missing`, `choose_encoder`, `QUALITIES`, `video_args`, `audio_args`, `scaled_size`, `bitrate_cap_bytes_per_second`. |
| `app/render_jobs.py` | `RenderManager`: renders (`Render`, `ClipState`), previews (`Preview`), brand assets (`Asset`, `check_png`), cached analysis, captions for the editor, delivery (`clip_file`, `zip_file`), pruning. |
| `app/render_routes.py` | The 13 routes and `read_json_body`. `OWN_BODY_PATHS` lists the routes the Guard lets read their own bodies. |
| `app/transcriber.py` | Gains `word_timestamps`, `_words`, and word-edge refinement (`Voicing`, `refine_words`). |
| `app/transcript.py` | `Word`, `Segment.words` (optional), `Transcript.has_words`. |
| `app/config.py` | `RenderSettings`/`load_render_settings` (9 `RENDER_*` variables), `WHISPER_WORD_TIMESTAMPS`. |
| `app/main.py` | `_Runtime.render`, the lifespan (start: capability detection; shutdown before the job manager), `create_app(render_settings=, renderer_cmd=, render_capabilities=)`. |
| `app/filenames.py` | `short_filename`, `shorts_zip_name`. |
| `static/shorts.js` | The Auto Shorts tab. |
| `static/studio.js` | The bridge gains several source listeners, `onClips`, `clips`, `marks`, `currentTime`. |
| `static/ai.js` | Tab switching covers three tabs (Manual, AI Highlights, Auto Shorts). |
| `Dockerfile` | FFmpeg gains libx264 (GPL), aac, swscale, libass, dav1d, h264/hevc/vp8 and rawvideo decoders, mjpeg and rawvideo encoders, image2 and rawvideo muxers, the rawvideo demuxer and the rendering filters. The runtime gains the libraries and fonts. |

## Request flow

```
POST /api/sources/{id}/renders               RenderManager.start_render
  ├─ ready? (settings valid, FFmpeg can render, encoder chosen), source ready, one render per source,
  │  queue bound, clips inside the video and ≤ RENDER_MAX_CLIP_SECONDS, asset ids exist
  ├─ JobManager.hold_source; folder <source>/render/<render id>/
  └─ task _run ─ wait for a render slot ─ per clip, in order (each its own task, so it can be cancelled):
       analyzing   (face/speaker/split, no manual frame) cached <source>/render/analysis/<range>.json,
                   else run_worker(mode=analyze) in analysis/work-<hex>/
       rendering   cues: edited, or clip_words + segment from <source>/ai/transcript.json
                   reserve_disk(render:<id>:<n>, work-NN, cap rate × length × 1.5 + 64 MiB)
                   run_worker(mode=render, captions as data, logo/intro/outro paths)
                   → work-NN/short.mp4 → NN.mp4 (+ NN.srt / NN.ass for export)
       ready | failed | cancelled          (a failed clip doesn't stop the batch)
  finally: work folders removed, disk released, release_source

GET /api/renders/{id}/zip                    zip_file: finished clips (+ subtitles), built on demand
POST /api/sources/{id}/shorts/previews       start_preview: own slot, draft quality, newest wins
POST /api/sources/{id}/shorts/assets         begin_asset → receive_upload → finish_asset
                                             (PNG header check; worker decodes it to .rgba / probes a video)
```

Cancellation (`/renders/{id}/cancel`, a source close or expiry, shutdown) marks the render and its
pending clips cancelled at once, then cancels the task. `run_worker` kills the worker's process group
(FFmpeg included), and the `finally` blocks remove work folders and release holds and reservations.
`/renders/{id}/clips/{n}/cancel` cancels only that clip's task. The loop goes on with the next clip.

## Things that are easy to get wrong

- **No user text in a filtergraph.** Graphs hold only numbers, enums and the fixed relative name
  `subs.ass`. FFmpeg runs with its cwd in the work folder. Don't add a path or a title to a graph.
- **Captions are written by the worker**, at the real canvas size. The manager sends cues and a caption
  plan as data. Only `escape_ass` makes text safe for ASS, so don't bypass `to_ass`.
- **FFmpeg never parses an image.** `test_packaging` enforces it, and so does the FFmpeg check
  ("no image decoders"). Logos go through `app.png` and are fed as `rawvideo`.
- **Crop paths use `t` from 0.** The graph's `fps=…:start_time=0` (or `setpts=…+AT/TB` for stills) makes the
  clip's own time the filter time. Change the prefix and the paths and captions drift.
- **The model picks nothing here.** Auto Shorts never calls OpenRouter. Highlights come from the clip list.
- **Word starts from Whisper are early after pauses.** Keep `refine_words` (in the transcriber, where the
  samples are) if you touch transcription.
- **Speaker focus is visual.** Don't label speakers automatically. Labels only come from the caption editor.
- **Holds and reservations.** Every `hold_source` has a `release_source` in a `finally`, and every
  `reserve_disk` a `release_disk`. A new code path must keep that.
- **Analysis cache keys** include the range, the fps and `ANALYSIS_VERSION`. Bump the version when the
  analysis format changes.

## Tests

| Area | File | Notes |
| --- | --- | --- |
| Settings, capabilities, encoder args, geometry, paths (evaluated against their FFmpeg expressions), layouts, graphs | `tests/unit/test_render_core.py` | |
| Templates, platforms, styles, request validation (28 tampered presets) | `tests/unit/test_render_spec.py` | |
| Captions: words, estimates, segmentation, Malayalam, SRT, ASS, escaping, placement; word-edge refinement | `tests/unit/test_captions.py` | |
| PNG decoder | `tests/unit/test_png.py` | Every colour type and filter against FFmpeg's own decode, plus malformed files and bombs. |
| Presets doc in sync | `tests/unit/test_presets_doc.py` | |
| Renderer with real FFmpeg | `tests/integration/test_renderer.py` | Each aspect, layout (by pixels of colour-coded sources), FPS, rotation, SAR, no-audio sources, still, logo, intro/outro, size limit, hardware fallback, protocol, cancel. |
| Burned captions, styles, Malayalam, real speech | `tests/integration/test_caption_render.py` | The real-speech test is opt-in (macOS `say` and Whisper `base`). |
| Faces and tracked framing | `tests/integration/test_faces.py` | Public-domain portraits; renders checked by finding the faces again. The OpenCV cross-check runs only when `cv2` is installed. |
| Manager and API | `tests/integration/test_render_api.py` | `RendererStub` for the lifecycle; `RealRenderRunner` for two acceptance tests with the real renderer. |
| Page | `tests/unit/test_page.py` | IDs, labels, ARIA, pinned copy, routes, script order, Tailwind sources. |
| Transcriber word timestamps | `tests/integration/test_transcriber.py` | |
| Docker | `scripts/docker-ffmpeg-check.sh`, `scripts/docker-smoke.sh` (+ `scripts/smoke_shorts.py`) | Rendering from AV1/HEVC/VP9/AC-3 sources, a JPEG still, no image decoders; an in-container captioned render. |

Run everything offline with `.venv/bin/pytest -q`, with real FFmpeg/ffprobe on `PATH`. Never run
`scripts/docker-smoke.sh` bare next to a live `saveme` container. Set `COMPOSE_PROJECT_NAME`,
`COMPOSE_FILE` and `SAVEME_PORT` as described in `JOURNAL.md`.

## Browser walkthrough

The journal describes the session's walkthrough. It started SaveMe natively on port 8077 with a
65-second talk (a public-domain portrait with `say` speech) and drove it with Playwright:

- upload, two clips, and the Auto Shorts tab;
- real transcription, word-timed captions, and an edited caption (an injection attempt stayed literal text);
- a preview still with face tracking, and rendering two shorts on VideoToolbox (measured progress,
  "Finding faces", estimates, the ZIP);
- presets: save, load, and a tampered one refused;
- keyboard tab switching, and phone width (375 px) in dark mode.

## Limitations (as built)

- **Speaker focus** is visual (mouth motion during transcript speech). There is no diarisation, and
  speaker labels are never added automatically.
- **No HDR tone mapping.** HDR (PQ/HLG) sources are converted to 8-bit BT.709 without tone mapping and
  look washed out.
- **MPEG-4 Part 2 (old AVI) sources** can be clipped (stream copy) but not rendered: the image's FFmpeg
  has no `mpeg4` decoder.
- **VideoToolbox** works only on native macOS runs. Docker always uses libx264.
- **Caption line fitting** estimates glyph widths. A very wide font at a large size can still run
  close to the safe margins.
- **Renders end with their source** (close, idle, replaced), like AI results. At most 4 finished renders
  are kept per source.
- **Presets** live in one browser, and intro/outro videos aren't saved in them.
