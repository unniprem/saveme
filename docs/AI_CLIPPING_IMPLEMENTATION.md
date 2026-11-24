# AI Smart Clipping (Phase 2): Implementation notes

The design is in [AI_CLIPPING_DESIGN.md](AI_CLIPPING_DESIGN.md), and key and model setup is in
[OPENROUTER_SETUP.md](OPENROUTER_SETUP.md). This file maps the design to the code, says how the
pieces talk, and lists how each part is tested. Branch: `feature/ai-clipping`.

## Code map

| File | What it holds |
| --- | --- |
| `app/transcript.py` | `Segment` and `Transcript` (pydantic, frozen), `WHISPER_LANGUAGES` (100 codes, equal to faster-whisper's), `WHISPER_MODELS`, `clean_text`, `dominant_language`, `parse_subtitles` (SRT/WebVTT), `save_transcript`/`load_transcript` (atomic write, a symlink or bad file reads as none), `reusable` (the cache rule). Imported by both processes; must not import faster-whisper. |
| `app/mediafile.py` | `extract_audio`: FFmpeg through the wrapper writes 16 kHz mono `pcm_s16le` WAV, with progress from `-progress pipe:1` and `-fs` as the length guard (`video_too_long`). |
| `app/transcriber.py` | The transcriber worker (`python -m app.transcriber`): `main` (memory cap by model, `nice(10)`, `block_network`, `HF_HUB_OFFLINE`), `run_transcribe`, `iter_windows`/`quiet_cut` (300 s windows cut at the quietest 100 ms frame in their last 20 s), `load_model` (local cache only; `missing_model` names the install command). |
| `app/whisper_models.py` | `python -m app.whisper_models [--list] [MODEL…]`, used by the Dockerfile and by hand. Tries 4 times. |
| `app/config.py` | `AiSettings`, `load_ai_settings` (separate from `Settings`; the key has `repr=False`, and errors never echo it). |
| `app/openrouter.py` | `OpenRouterClient` (`model`, `key_info`, `complete`), `model_info`/`check_model`, `Pricing` (with long-prompt tiers), `UsageMeter`, `parse_json_object`. |
| `app/highlights.py` | `make_chunks`, `SYSTEM_PROMPT`, `build_messages`, `RESPONSE_SCHEMA`, `parse_answer` (`_AnswerClip`, validated per item), `place`, `rank`, `estimate_from_transcript`/`estimate_from_duration`, `analyze`. |
| `app/ai_jobs.py` | `AiManager` and `Analysis`: lifecycle, slots, holds, cache, spend checks, cancellation and cleanup. |
| `app/ai_routes.py` | The routes, and `read_small_body` for subtitle import. |
| `app/jobs.py` | Added `ready_source`, `hold_source`/`release_source`, `add_source_listener`, `make_room`. `_in_use` counts holds, and `_expire` calls the listeners before removing a source's folder. |
| `app/main.py` | `create_app(..., ai_settings, transcriber_cmd, openrouter_transport)`, `_Runtime.ai`, `_reads_own_body` (the upload and subtitle import routes skip the 64 KiB cap), and validation messages for the AI fields. |
| `app/schemas.py` | `AnalysisRequest`. |
| `app/errors.py` | 18 new codes, from `invalid_subtitles` to `ai_invalid_response`. |
| `static/ai.js` | The AI Highlights tab. |
| `static/studio.js` | `window.SaveMeStudioBridge`: `onSource`, `loadRange`, `addRanges`, `exportRange`, `seek`, `fmtTime`, `parseTime`, `isExporting`. |
| `static/index.html` | Tabs (`#studio-tabs`), `#manual-panel` (the Phase 1 controls, unchanged), `#ai-panel`. |
| `Dockerfile` | FFmpeg gains `pcm_s16le` and `wav`. The model is baked into `/opt/huggingface` (`WHISPER_MODELS` build argument), with `HF_HUB_OFFLINE=1`. |

## Request flow

```
POST /api/sources/{id}/analyses        AiManager.start
  ├─ validate (AnalysisRequest, count ≤ AI_MAX_CLIPS, key unless transcribe_only)
  ├─ one pending analysis per source, JobManager.hold_source
  └─ task _run ─ wait for an AI slot ─ asyncio.timeout(AI_MAX_JOB_SECONDS)
       preparing    source ready, length ≤ AI_MAX_SOURCE_MINUTES, client.model() + check_model
       (cache)      load_transcript(<source>/ai/transcript.json) + reusable(...)
       extracting   make_room(32 kB/s × length + 64 MiB)
       transcribing run_worker(app.transcriber, workdir=<source>/ai/<analysis id>)
                    → transcript.json → validated → saved as the cache
       analyzing    estimate_from_transcript → cost_limit if max_usd > limit
                    analyze(): per chunk, spend check → complete() → parse/validate (1 retry)
       ranking      place → rank
       ready        candidates, notice, usage
  finally: remove the work folder, release_source
```

Cancellation (`POST /api/analyses/{id}/cancel`, a source close or expiry, or shutdown) marks the
analysis `cancelled` at once and cancels its task. `run_worker` then kills the transcriber's
process group, or httpx abandons the request.

## Things that are easy to get wrong

- **The model picks segment numbers.** Never let model output become seconds directly: `place()` derives
  times from segments and clamps to the transcript's duration, which equals the source's probed length.
- **`require_parameters` is always on.** Add a request parameter only behind a check that the model
  lists it (`ModelInfo.parameters`).
- **The transcriber must stay offline.** `block_network()` runs before faster-whisper is imported. Don't add
  `local_files_only=False` anywhere at runtime; install models with `app.whisper_models` instead.
- **Memory.** faster-whisper builds features for the whole input, so keep `iter_windows` feeding it bounded
  windows. 600 s windows peaked at 1.1 GB on `base`.
- **The source folder holds AI files** (`ai/transcript.json`, `ai/<analysis id>/`). The probe and download
  runs clear their workdir only before a source is ready, so they never meet these files.
- **Untrusted text.** Titles, summaries, tags, transcript lines and OpenRouter error messages are cleaned
  (`clean_text`) and only ever set with `textContent`. `test_static_js_never_injects_html` guards this.

## Tests

| Area | File | Notes |
| --- | --- | --- |
| Transcript, subtitles, languages, cache rule | `tests/unit/test_transcript.py` | |
| Model installer | `tests/unit/test_whisper_models.py` | Includes the retry. |
| AI settings, `.env.example`, README table | `tests/unit/test_ai_config.py`, `tests/unit/test_packaging.py` | |
| OpenRouter client | `tests/unit/test_openrouter.py` | `httpx.MockTransport` via `tests/support/openrouter.py`, with catalogue entries shaped like the live ones. |
| Highlight engine | `tests/unit/test_highlights.py` | Chunk coverage property, injection quoting, validation, snapping, dedupe, spend stop. |
| Audio extraction, transcriber worker | `tests/integration/test_transcriber.py` | Real FFmpeg; fake model via `tests/support/transcriber_entry.py`. Opt-in real `base` test (needs the model and macOS `say`). |
| Source holds and listeners | `tests/integration/test_source_holds.py` | |
| `AiManager` | `tests/integration/test_ai_jobs.py` | `TranscriberStub` in `tests/support/ai.py`. Covers stages, cache, cancel at each stage, source close, concurrency, downloads unaffected, spend limit, timeout, disk, errors. |
| HTTP routes and acceptance flow | `tests/integration/test_ai_api.py` | `StudioStub` has a `transcribe` mode. The flow goes from analysis to highlights to a clip and a ZIP through the Phase 1 routes, then a regeneration that reuses the transcript. |
| Page | `tests/unit/test_page.py` | IDs, ARIA roles, pinned privacy and score copy, script order, Tailwind sources. |
| Live OpenRouter | `tests/network/test_openrouter_live.py` | Opt-in (`-m network`, needs `OPENROUTER_API_KEY`). |

Run everything offline with `.venv/bin/pytest -q`. The Docker checks are `scripts/docker-ffmpeg-check.sh`
(codec matrix including the WAV extraction, plus the integration tests inside the image) and
`scripts/docker-smoke.sh` (now also checks `/api/ai/status`, the baked model and `HF_HUB_OFFLINE`).
Never run the smoke test bare next to a live `saveme` container: set `COMPOSE_PROJECT_NAME`,
`COMPOSE_FILE` and `SAVEME_PORT` as described in `JOURNAL.md`.

## Browser walkthrough harness

`JOURNAL.md` describes the session's harness: `serve_ai.py` (the real transcriber plus a simulated
OpenRouter that answers with segment numbers) and `check-ai.js` (Playwright, installed Chrome). It
covers:

- desktop light, and phone 375 px dark;
- the not-configured state;
- editing, preview, load into editor, add to list, ZIP and single export;
- transcript search and seek;
- regeneration, cancel, subtitle import, and a reload.

## Limitations (as built)

- Clip edges follow Whisper segments (no word timestamps), with 0.3 s lead-in and 0.5 s tail.
- No speaker diarisation, no vertical reframing, no burned-in subtitles, no re-encoding (Phase 3).
- The spend limit is a client-side estimate. A cancelled in-flight request may still be billed.
- With `auto` language, each 300 s window detects its own language. A window of music can be mislabelled;
  choose the language to avoid it.
- Analyses, transcripts and results are in memory and in the source folder: a restart, or the source
  closing or expiring, removes them.
- The live OpenRouter test hasn't been run in development (no key was available). Every OpenRouter path is
  tested against a mock shaped like the documented API.
