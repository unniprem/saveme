# Journal

---

## 2026-11-24 13:29 IST — Session: Phase 2, AI smart clipping (OpenRouter)

### Summary

The owner sent the Phase 2 brief: AI highlight detection in Clip Studio, with local faster-whisper transcription and OpenRouter as the only AI provider, built test-first in small commits on `feature/ai-clipping`, with this journal updated after every task. The brief was taken as a go-ahead without a question round; design calls are under Key Decisions.

**Status: Phase 2 complete. At the owner's request, `feature/ai-clipping` was pushed to GitHub, then merged into `main` with `--no-ff` and pushed.** All 9 tasks are done. The 12 acceptance criteria were shown in Chrome against the real transcriber with a simulated OpenRouter. Most were also shown in-process against a mocked OpenRouter, and transcription in the hardened container. The one thing not exercised is a real OpenRouter call: no key exists in this environment. An independent review found 2 important and 8 minor problems, all fixed with tests.

### Progress by task

- [x] **Task 1, audit.** Phase 1 is on `main` at `206d26b`. Integration points, dependencies and risks are in §1 of `docs/AI_CLIPPING_DESIGN.md`. Measured on this M2 (8 GB), 20 min of speech, `base`, int8, 2 threads: 600 s windows peaked at 1.10 GB RSS, 300 s at 0.74 GB, 120 s at 0.62 GB; VAD on took 76–81 s, VAD off 228 s.
- [x] **Task 2, design.** `docs/AI_CLIPPING_DESIGN.md`: architecture, API contracts, schemas, lifecycle, limits.
- [x] **Task 3, transcription.** `app/transcript.py` (schema, SRT/WebVTT import, language codes, cache file and reuse rule), `app/transcriber.py` (worker: FFmpeg → 16 kHz WAV → faster-whisper over 300 s windows), `app/whisper_models.py` (model installer), `extract_audio` in `app/mediafile.py`. Dependencies and the Docker build are updated; the image isn't built yet (Task 8).
- [x] **Task 4, OpenRouter.** `AiSettings`/`load_ai_settings` in `app/config.py` (separate from the download settings; the key is kept out of `repr` and errors), `.env.example` documents every AI variable, `app/openrouter.py` (catalogue and capability check, pricing tiers, chat completions, retries, key check, `UsageMeter`), and 9 new error codes.
- [x] **Task 5, highlight engine.** `app/highlights.py`: chunks (≤ 6,000 estimated tokens, ≤ 20 min, overlap max(60 s, max clip length)), prompts, strict JSON schema, per-item pydantic validation, snapping and length rules, dedupe (> 30 % of the shorter clip), ranking, estimates, and `analyze()` with the spend guard. Also `tests/network/test_openrouter_live.py` (opt-in, needs a key).
- [x] **Task 6, job integration.** `JobManager` hooks: `hold_source`/`release_source`, `add_source_listener` and `make_room`. `app/ai_jobs.py` `AiManager` runs the stages with its own semaphore. `app/ai_routes.py` adds the routes. `app/main.py` wires them in, lets the subtitle import read its own body, and gives AI fields their own validation messages.
- [x] **Task 7, UI.** A Manual / AI Highlights tab switch in Clip Studio (`static/index.html`) and `static/ai.js`. `window.SaveMeStudioBridge` in `static/studio.js` lets the AI tab reuse the editor, clip list and export. The design is in §11 of the design doc.
- [x] **Task 8, integration testing.** The whole suite, the image's FFmpeg check, the Docker smoke test, a real transcription and a cancel in the hardened container, and Chrome walkthroughs of AI Highlights, Phase 1 clipping and the downloader.
- [x] **Task 9, docs and review.**
  - Docs: `README.md`, `docs/OPENROUTER_SETUP.md`, `docs/AI_CLIPPING_IMPLEMENTATION.md`, plus the design doc aligned with the code.
  - An independent read-only review (a fresh general-purpose agent, given the code and the requirements, not my conclusions) found 2 important and 8 minor problems. All 10 are fixed (`2bb763e`–`9cf5b3a`). Final verification is below.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `a8a035d` | docs | Add the AI smart clipping design |
| `9641cd6` | feat | Add the transcript schema and SRT/WebVTT import |
| `3c4c909` | feat | Add the local transcriber worker |
| `3429ad1` | feat | Add a command that installs Whisper models |
| `ee273d4` | test | Check a cancelled transcription by the worker's own PID |
| `9434809` | build | Add faster-whisper, httpx, a baked Whisper model and WAV output |
| `693274c` | feat | Decide when a cached transcript can be reused |
| `3c6c61a` | feat | Add AI Highlights settings, apart from the download settings |
| `f69c266` | feat | Add the OpenRouter client with retries and usage accounting |
| `9a7fd4b` | feat | Add the highlight engine |
| `a4e34c2` | test | Add an opt-in live OpenRouter analysis check |
| `300e5bf` | feat | Let an analysis hold a source and hear when it ends |
| `a2a6ec9` | feat | Run AI Highlights analyses as their own job lifecycle |
| `dba2550` | feat | Add the AI Highlights API routes |
| `16d59eb` | fix | Answer a missing transcript with null instead of 404 |
| `a4134c3` | feat | Add the AI Highlights tab to Clip Studio |
| `e2829d2` | fix | Retry a Whisper model download that fails for a moment |
| `802b769` | test | Check AI Highlights in the Docker smoke test |
| `55a8acd` | docs | Describe AI Highlights in the README |
| `0dccfe4` | docs | Add the OpenRouter setup guide |
| `7049622` | docs | Add AI clipping implementation notes and align the design's API |
| `c284abe` | fix | Let the smoke test read the whole model list |
| `2bb763e` | fix | Refuse a malformed OPENROUTER_BASE_URL as a setting error |
| `258ea37` | fix | Scrub the OpenRouter key before cutting a message to length |
| `0bafe02` | fix | Keep OpenRouter settings out of every worker's environment |
| `e717a6e` | fix | Refuse an incomplete Whisper model folder before loading it |
| `0bf48fb` | fix | Refuse odd subtitle times as invalid instead of failing with 500 |
| `a618027` | fix | Refuse a model whose answer length can't be capped |
| `10266b1` | fix | Make the spending bound a true upper bound for any tokenizer |
| `4cba86d` | fix | Count possibly billed failed attempts and check the limit before a retry |
| `9cf5b3a` | fix | Hold an analysis's disk reservation until it ends |
| `04d43d5` | docs | Describe the true spending bound, held disk reservation and network block |

### Test Results

- Task 3: `tests/unit/test_transcript.py` 28 passed, `tests/unit/test_whisper_models.py` 6 passed, `tests/integration/test_transcriber.py` 26 passed (real FFmpeg 8.0.1; fake Whisper model through `tests/support/transcriber_entry.py`). Each new file failed first on the missing module. The opt-in real-model test also passed: `base` (installed with `python -m app.whisper_models base` into `~/.cache/huggingface`) transcribed macOS `say` speech as English and found "intelligence" and "vehicles".

- Task 4: `tests/unit/test_ai_config.py` 33 passed, `tests/unit/test_packaging.py` 16 passed (now also checks the AI variables in `.env.example`, with only the key blank), `tests/unit/test_openrouter.py` 37 passed against `httpx.MockTransport` (`tests/support/openrouter.py` holds catalogue entries shaped like the live ones). Each failed first: missing module, missing `AiSettings`, missing `UsageMeter`.

- Task 5: `tests/unit/test_highlights.py` 43 passed (it failed first on the missing module). One expectation was changed before implementing: when padding would break `max_seconds`, the padding shrinks and speech is never cut. The live test `tests/network/test_openrouter_live.py` is **not run**: no `OPENROUTER_API_KEY` exists in this environment or in `.env`. It skips without one and is deselected by default (`-m 'not network'`).

- Task 6: `tests/integration/test_source_holds.py` 10 passed, `tests/integration/test_ai_jobs.py` 32 passed, `tests/integration/test_ai_api.py` 27 passed. Each failed first on the missing method, module or route. `test_ai_api.py` runs the acceptance flow in-process: source, then analysis, then highlights, then one clip and a 2-clip ZIP through the Phase 1 routes, then a second analysis that reuses the transcript.
- **Whole suite at `dba2550`: 908 passed, 2 skipped, 8 deselected (109 s).** The baseline was 673 passed, and every Phase 1 and downloader test still passes.

- Task 7: `tests/unit/test_page.py` 17 passed. The new AI markup and copy tests failed first. Real-browser walkthrough `check-ai.js` (installed Chrome, Playwright) against `serve_ai.py`, both in `$CLAUDE_JOB_DIR/tmp/dev/ai/`. It runs the **real transcriber** (Whisper `base`, Homebrew FFmpeg) on a 100 s `say`-generated talk, with a **simulated OpenRouter**, because no key exists here. It showed:
  - **Desktop 1200 px, light:**
    - ArrowRight switches to the AI tab. The estimate reads "about $0.0007, at most $0.0013, from the video's length".
    - Find highlights moved focus to the progress panel, and stages advanced with real percentages. Ready took 11–25 s.
    - 3 cards. A bad end time showed "Type a time like 1:05 or 65.5." with `aria-invalid`. Preview played from 0:05.7. Load into editor switched to Manual with the marks set and focus on Start.
    - Add selected put 3 rows in the list; a second press said "3 were already in it". The ZIP held 3 entries, and Export on a card saved one clip.
    - Transcript search "electric" found 3 lines, and clicking a time seeked to 57.6 s.
    - Regenerating skipped extracting and transcribing. Cancelling a re-transcription showed where it stopped and kept the earlier highlights.
    - Subtitle import worked, and a reload restored the AI tab with its highlights.
  - **Phone 375 px, dark:** `scrollWidth` 375, card buttons 40 px.
  - **No key** (second server): the setup message shows, Find highlights and Test connection are disabled, and Transcribe only is enabled.
  - Screenshots are in `$CLAUDE_JOB_DIR/tmp/dev/ai/shots/`. Console errors: none in the final run.
- Issues the walkthrough found, now fixed before the UI commit:
  - a reload lost the AI state;
  - a 404 console error came from the "any transcript yet?" probe (`16d59eb`);
  - stopped runs didn't show where they stopped;
  - a cancelled run hid the earlier highlights.

- **Task 8:**
  - **Phase 1 regression in Chrome.** `check-zip.js` against `serve.py` (in `$CLAUDE_JOB_DIR/tmp/dev/`), with no console problems:
    - three clips added and sorted, and a repeat refused;
    - Load, Remove, and a reload restoring the list;
    - a ZIP of 3 stored H.264+AAC entries;
    - cancel winning the race with no download;
    - Close clearing storage;
    - phone 375 px, dark, with no horizontal scroll and 40 px row buttons, and a 2-clip ZIP from an upload.
  - **Downloader in Chrome.** `check-download.js`: a video and an MP3 both reached "Saved", with no console problems.
  - **Docker.** `saveme:ai-clipping` built. The first build failed on a transient DNS error fetching the Whisper model ("Name or service not known"); DNS worked from containers straight after, and the rebuild passed. `e2829d2` makes the installer retry.
  - **`scripts/docker-ffmpeg-check.sh saveme:ai-clipping`: FFMPEG CHECK OK.** Every matrix case passed, the 4 new WAV cases included (AAC MP4, Opus WebM, AC-3 MKV to 16 kHz mono PCM), and **535 integration tests passed inside the image** against the minimal FFmpeg 9.0.2. One test skipped: the real-Whisper test, which needs macOS `say`.
  - **`scripts/docker-smoke.sh`: SMOKE OK**, isolated as `COMPOSE_PROJECT_NAME=saveme-ctest` with an override setting `image: saveme:ai-clipping` and `SAVEME_PORT=8001`. The new AI step's `grep -q` caused a harmless BrokenPipe traceback, fixed in `c284abe`. The live `saveme-saveme-1` wasn't touched, and `saveme-ctest` was taken down afterwards.
  - **Hardened container** (`container-ai.sh`, `container-ai-cancel.sh` in `$CLAUDE_JOB_DIR/tmp/dev/ai/`):
    - Analysis without a key was refused with 503 `ai_not_configured`.
    - Uploading the 100 s talk and running Transcribe only, with the baked `base` model, finished in 11 s. It found English and 20 segments, with correct text ("…now I use an AI assistant every single day…"), and the container's memory peaked near 364 MiB.
    - The source folder held only `media.mp4` and `ai/transcript.json`. No folders were left after Close.
    - Cancelling during transcription answered `cancelled`. Within 6 s, 0 transcriber processes remained (1 while it ran) and the `ai/` folder was empty. 0 folders were left after Close.
  - **Whole suite at `c284abe`: 911 passed, 2 skipped, 8 deselected** (113 s, idle machine).
  - One earlier whole-suite run, during the Docker build's FFmpeg compile (177 s instead of ~110 s), failed the **existing** `tests/integration/test_api.py::test_aborted_download_keeps_file`. It passed 5/5 alone and 102/102 in its file twice once the machine was idle. Cause: after aborting a download the test sleeps a fixed 1 s, then requests the file again. Under heavy CPU load the server may not have noticed the disconnect yet, so the file is still claimed (409). This predates this branch, and delivery wasn't changed. It's left as is and noted under Pending Work.

- **Review fixes (Task 9).** Each fix had a test that failed first. The findings, and their tests:
  - **Important.** `OPENROUTER_BASE_URL=https://[openrouter.ai/…` raised `UnboundLocalError`, and `https://openrouter.ai:abc/…` passed validation then broke the httpx client. Either stopped the whole app, downloads included. Both are now `ConfigError`s; 4 new cases plus a startup test.
  - **Minor, spending:**
    - retried completions weren't checked against the limit;
    - possibly billed failed attempts, and replies without usage, counted $0;
    - a model that can't cap answer length was accepted;
    - the "upper bound" (bytes/3) wasn't one for byte-fallback tokenizers (Malayalam). It now counts a token per byte.
  - **Minor, other:**
    - the key could survive scrubbing at the 300-character cut;
    - the key reached every worker's environment;
    - an incomplete model folder made faster-whisper's Rust tokenizer go to the network;
    - subtitle times with over 4,300 hour digits, or an end that rounds onto its start, gave 500;
    - the WAV's disk reservation wasn't held.
  - The reviewer's checks that passed: the key stays out of responses and logs, only transcript text is sent, the guard order, paths, model output handling, clip times, cancellation, slots.
- **Final verification at `04d43d5`:**
  - **Whole suite: 932 passed, 2 skipped, 8 deselected** (110 s, idle machine).
  - **Rebuilt `saveme:ai-clipping`:**
    - `docker-ffmpeg-check.sh`: FFMPEG CHECK OK, **539 integration tests passed in the image**, 1 skipped (`say`).
    - Isolated `docker-smoke.sh`: SMOKE OK, with no traceback.
    - In the hardened container: Transcribe only finished in 11 s (English, 20 segments, peak about 496 MiB). The baked model passed the new completeness check. Without a key the analysis got 503. Cancelling during transcription left 0 processes and an empty `ai/`, and 0 folders remained after Close. `saveme-ctest` was taken down, and `saveme-saveme-1` was untouched.
  - **Chrome `check-ai.js` on the final code:** every flow passed as before, with **no console problems**.

### Key Decisions

- **Default model `anthropic/claude-haiku-5.5`**, picked from OpenRouter's live catalogue on 2026-11-24 (474 models): $0.10/$0.50 per million tokens, 1M context, structured outputs on its providers, reasoning optional. It lists no `temperature`, so the client sends only parameters a model lists (with `require_parameters: true`, an unlisted one would exclude every provider).
- **Prompt injection:** the instruction and transcript sit in `<instruction>`/`<transcript>` blocks, and `<`/`>` inside them become `‹`/`›`, so neither can close its block. The system prompt says both are data. Whatever the model answers, the server enforces segment ranges, lengths, counts, scores and text caps.
- **Unusable answers:** retried once, then that part is skipped with a notice. The analysis fails (`ai_invalid_response`) only if every part fails.
- **The model returns segment numbers, not seconds.** The server maps them to real segment times, so clips start and end on speech boundaries and can't point outside the transcript.
- **Own transcription worker** (`python -m app.transcriber`) run by the existing `run_worker`: a model-sized memory cap (the download worker's 1 GiB hard cap can't be raised), a connect guard that refuses every connection, and nice(10).
- **300 s windows with VAD**, from the measurements above.
- **AI work doesn't share download slots or the queue.** It has its own semaphore (`AI_MAX_CONCURRENT_JOBS`), and an invalid AI setting turns off only AI.
- **An analysis holds its source from start to end.** The source then can't idle out or be replaced, and closing it cancels the analysis through a listener that runs before the folder is removed. Work files live in `<source>/ai/<analysis id>/`, so the sweep never sees them as orphans.
- **The model is checked before transcribing**, so a bad `OPENROUTER_MODEL` fails in seconds, not after minutes of CPU. The spending limit is checked after transcribing, against the real transcript.
- **Transcript cache in the source's folder** (`ai/transcript.json`), so it ends with the source.
- **PyAV is installed (a faster-whisper dependency) but never given media.** Audio is decoded by the hardened FFmpeg wrapper.
- **Retries:** network errors, timeouts, 408, 429, 5xx and errors inside a 200 are retried 3 times (1 s, 2 s, 4 s plus jitter, or `Retry-After` up to 60 s). 401, 402 and other 4xx fail at once. OpenRouter's own messages are shown only for "refused" (4xx), cleaned and scrubbed of the key. A 401's message is never shown.
- **Usage:** OpenRouter returns `usage.cost` by default (the docs say `usage: {include: true}` is deprecated), so it isn't requested. A call without a reported cost is priced from tokens × catalogue price, and the total is then labelled an estimate.
- **Find highlights is `type="button"`**, like every studio button except Fetch. Enter in a number field never starts a paid run.
- **No transcript yet is `200 {"transcript": null}`**, not a 404, because the page asks every time a video opens.
- **The AI tab drives the existing editor through a small bridge** (`loadRange`, `addRanges`, `exportRange`, `seek`). Highlights become ordinary clip-list rows and exports, with no second export path.
- **The spending bound counts a token per byte.** No tokenizer makes tokens smaller than a byte, so this bound holds for any model. The page still shows a typical estimate at 3 bytes a token beside it. A retry happens only if it fits. Failed attempts that reached the model (a read timeout, a broken connection after sending, an error inside a 200) count at their usage or bound. A refused connection, or a 429/5xx from OpenRouter, counts as nothing.
- **The disk reservation is held** (`reserve_disk`/`release_disk`, replacing `make_room`). It counts like an active job's: the larger of what its folder holds and what it reserved.
- **No OS-level network isolation for the transcriber.** The Python guard, offline HF settings and the model-folder check cover the known paths. A network namespace would need container changes beyond this phase.
- **`provider.data_collection: "deny"` by default**, so transcripts only go to providers that don't store or train on prompts.

### Issues Encountered

- faster-whisper computes features for the whole input at once, so memory grows with length. Windowing fixes this.
- The cancel test first used `pgrep`, which the slim image lacks (`scripts/docker-ffmpeg-check.sh` runs the integration tests inside it). It now checks the worker's own PID.
- The first Docker build failed fetching the Whisper model: "Name or service not known", a transient DNS error in Docker Desktop's build network. DNS worked from containers moments later, the rebuild passed, and `e2829d2` adds retries.
- The smoke test's `grep -q` made `whisper_models --list` die on a broken pipe (`c284abe`).
- The Chrome walkthrough found four UI bugs before the UI commit:
  - a reload lost the AI state;
  - a 404 console error;
  - stopped runs didn't show their stage;
  - a cancelled run hid the earlier highlights.
- `tests/integration/test_api.py::test_aborted_download_keeps_file` (existing, not changed) failed once under heavy CPU load. See Test Results.
- **No OpenRouter key exists in this environment**, so no real completion was made. Every OpenRouter path is tested against a mock shaped like the live API (the catalogue entries are copied from `GET /api/v1/models` on 24 Nov 2026), and the browser walkthrough used a simulated OpenRouter.

### Pending Work

- **Run the live check once with a real key** (well under $0.01): `OPENROUTER_API_KEY=sk-or-... .venv/bin/pytest -m network tests/network/test_openrouter_live.py -v`. Then try the AI tab in the live app.
- **Rebuild the live container** to run Phase 2: `docker compose up -d --build`. `main` is merged and pushed; the live `saveme-saveme-1` still runs the image built before Phase 2.

  Notes for the rebuild:
  - The image grows from 311 MB (`saveme:local`) to 738 MB: faster-whisper, CTranslate2, onnxruntime, PyAV, NumPy, and the `base` model (141 MB).
  - The build downloads the model from Hugging Face.
  - `.env` needs `OPENROUTER_API_KEY` for analysis. Transcription works without it.
- **For bigger Whisper models,** rebuild with `--build-arg WHISPER_MODELS="base small"` and raise `mem_limit` in `compose.yaml`. The transcriber allows 2.5 GB for `small`, and the container has 2 GB. Only `base` was measured.
- **Known limitations** (also in `docs/AI_CLIPPING_IMPLEMENTATION.md`):
  - clip edges follow Whisper segments, with no word timestamps and no diarisation;
  - the spending limit is a client-side guard, and a cancelled in-flight call may still be billed;
  - with `auto`, each 300 s window detects its own language;
  - the transcriber's network block is Python-level, not OS-level;
  - results end with the source.
- **Pre-existing:** `test_aborted_download_keeps_file` waits a fixed 1 s after aborting, and can fail under heavy CPU load. Polling for the delivery claim to end would fix it.
- **Leftover images:** `saveme:ai-clipping` (738 MB) and `saveme-ffmpeg-check` (1.17 GB). Remove them with `docker image rm` when done.
- **Phase 3** (not started): 9:16 reframing, burned-in subtitles and re-encoding. These need an H.264 encoder in the FFmpeg build, and a security and licensing review.

### Handoff

- `main` now holds Phase 2: `feature/ai-clipping` was merged with `--no-ff` on top of `206d26b`. Both branches are pushed to `origin`. Start new work on a new branch from `main`.
- **Where to look:**
  - `docs/AI_CLIPPING_DESIGN.md` for the why;
  - `docs/AI_CLIPPING_IMPLEMENTATION.md` for the code map, the request flow, the "easy to get wrong" list and the test map;
  - `docs/OPENROUTER_SETUP.md` for the key, the model, what is sent, and costs.
- **Environment:** `.venv` is synced to `requirements-dev.txt` (`uv pip sync --python .venv/bin/python requirements-dev.txt`). The `base` model is in `~/.cache/huggingface` (`.venv/bin/python -m app.whisper_models base`), which also enables the opt-in real-Whisper test.
- **Tests:** `.venv/bin/pytest -q` (offline, about 110 s, 932 tests). Real FFmpeg/ffprobe must be on `PATH` (Homebrew here).
- **Dev harness** (temporary, in `$CLAUDE_JOB_DIR/tmp/dev/ai/`; recreate it from this description if gone):
  - `serve_ai.py <repo> <dir> <port> configured|unconfigured`: the real transcriber with a simulated OpenRouter;
  - `check-ai.js <media URL>`: the Chrome walkthrough, run as `PWCORE=~/.npm/_npx/<id>/node_modules/playwright-core node …`;
  - `tech-talk.mp4`: a 100 s `say` talk;
  - `container-ai.sh`, `container-ai-cancel.sh` and `final-docker.sh` for the container checks;
  - `compose.aitest.yaml`.
- **Never run `scripts/docker-smoke.sh` bare.** It would replace the live `saveme-saveme-1`. Set `COMPOSE_PROJECT_NAME=saveme-ctest`, `COMPOSE_FILE=compose.yaml:<override with image saveme:ai-clipping>` and `SAVEME_PORT=8001`, then run `docker compose down` with the same variables.
---

## 2026-11-24 11:13 IST — Session: Clip Studio, Phase 1 completion (clip list and ZIP export)

### Summary

The owner re-sent the full Phase 1 brief. `feature/clip-studio` was already merged into local `main` (`b661127`, not pushed), so this session checked the brief's ten features against what shipped. Two were missing, because the first design listed them as non-goals: **#4, several clips from one source as a list**, and **#6, ZIP batch export**. This session adds both on `feature/clip-studio` (fast-forwarded to `main` first). The design is `docs/superpowers/specs/2026-11-24-clip-studio-batch-export-design.md`. The other eight features were already in place, and links and nearly every upload already come out as MP4. Cheap fixes for four of the deferred review minors are also planned. The owner's brief was taken as a go-ahead to proceed without a question round; the design calls are listed under Key Decisions.

**Status: Phase 1 complete, merged into local `main` with `--no-ff` at the owner's request (not pushed).** All ten features in the brief are in place, verified by the whole suite, the image's minimal-FFmpeg check, the Docker smoke test, a ZIP export in the hardened container and real-browser checks. An independent review found nothing important, and its fixable minors are done.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `938a3ff` | docs | Add clip list and ZIP export design for Clip Studio |
| `6900fb9` | feat | Add a worker batch mode that cuts several clips into one ZIP |
| `4f7554c` | feat | Run batch exports as a job kind in the job manager |
| `3665971` | feat | Add the batch export route for Clip Studio |
| `cdf75e7` | feat | Add a clip list and ZIP export to Clip Studio |
| `3b60205` | fix | Compare clip lengths in whole milliseconds on both sides |
| `2115d88` | fix | Poll an open studio's source at a quarter of its idle time |
| `a5d3d82` | fix | Turn off playhead controls when the browser can't play the source |
| `daa8c8a` | fix | Tell a too-large clip to choose a shorter part |
| `1f31a98` | docs | Describe the clip list, ZIP export and their limits |
| `eaff720` | fix | Tidy the clip list on restore and once the video's length is known |
| `d181237` | fix | Keep keyboard focus when a studio control disables itself |

### Test Results

- Baseline on `feature/clip-studio` at `b661127`: `.venv/bin/pytest -q` gave 599 passed, 2 skipped, 7 deselected (138 s).
- `6900fb9`: `tests/unit/test_filenames.py` plus the worker clip, probe and batch tests gave 108 passed. The new batch tests, using the real worker and FFmpeg 8.0.1, check:
  - 3 ranges into one stored ZIP, with entries in order, H.264+AAC, the right number of shown frames, and the first shown frame at 0;
  - progress over the whole batch ("Cutting clip n of 3"), with rising percent and bytes;
  - tags kept or dropped, and MKV entries from an MKV source;
  - `too_large` when each clip fits but the ZIP doesn't;
  - 21 malformed requests refused as `internal`, including names with `/`, `\`, `:`, NUL, a leading dot, the wrong extension, 300 bytes, or a repeat;
  - a failing second cut reported as "Couldn't cut clip 2" (in-process).
- `4f7554c`: `test_sources.py` plus `test_jobs.py` gave 94 passed. The 10 new batch manager tests failed first (no `submit_batch`). They cover:
  - the request with numbered entry names, then progress, the ZIP name and type, one-shot delivery, and the source staying ready;
  - errors naming the clip, and 0 or 21 clips refused;
  - end clamping and needing a ready source;
  - cancel (no resume, folder gone), closing the source cancelling it, and the source not expiring while a batch is pending;
  - upload batches dropping tags;
  - a download that returns a ZIP failing as `internal`, and a batch returning an MP4 too;
  - the 2× reservation (`disk_full` at the smallest budget while a single clip still fits).
- `3665971`: `test_studio_api.py` plus `test_api.py` gave 155 passed, and `tests/unit` gave 234 passed. The 16 new API tests failed first (404, no route). They cover:
  - the batch flow end to end with the stub: 202, kind `batch`, the ZIP filename, `application/zip`, the `Content-Disposition`, one-shot delivery, and the source staying ready;
  - 12 validation cases, each with its message (for example "Clip 2: start must be a number of seconds, 0 or more", "Send 1 to 20 clips.");
  - 409 before the source is ready, 404 for a job ID, 403 for a foreign Origin;
  - cancel through `/api/jobs/{id}/cancel` with no resume and no folder left;
  - `max_batch_clips` in `/api/studio`.
- `cdf75e7`: `tests/unit/test_page.py` gave 15 passed (new IDs, copy, the hidden list, and the `/batch` call are pinned). Then a real-browser check (`check-zip.js` against `serve.py`, both in `$CLAUDE_JOB_DIR/tmp/dev/`, in the installed Google Chrome). No console errors or warnings, and it showed:
  - **Desktop 1200 px, light, from a link:**
    - Three ranges added out of order, then listed sorted. A repeat was refused with "This part is already in the clip list."
    - Load put 0:03.3–0:07.3 back in the fields and moved the video to 3.3 s. Remove moved focus to the next row's Remove.
    - A reload restored 3 rows.
    - **Export 3 clips as ZIP** auto-saved `long (3 clips).zip`. It holds `01 …`, `02 …` and `03 …` stored entries, all H.264+AAC, and the page read "ZIP saved".
    - Cancel pressed right after a second export read "Export cancelled. Nothing was kept on the server.", with no download.
    - Close cleared `sessionStorage`.
  - **Phone 375 px, dark, from an uploaded `Beach day.mp4`:** two clips listed, row buttons 40 px tall, `scrollWidth` 375 (no horizontal scroll), and `Beach day (2 clips).zip` saved with both entries.
  - Screenshots are in `$CLAUDE_JOB_DIR/tmp/dev/shots/zip-*.png`. Row times are kept unbroken (`whitespace-nowrap`) after the first phone screenshot split "· 0:02.0".
- Deferred-minor fixes:
  - `3b60205`: a new manager test failed first, because the server refused 1.1–1.2 s (`1.2 - 1.1` is `0.0999…`), and now passes.
  - `daa8c8a`: the clip `too_large` test now checks the message, and failed first on the old download advice.
  - `test_sources.py` 49 passed, and the worker clip plus batch tests 44 passed.
  - Real-browser check `check-minors.js` (Chrome), against the restarted `serve.py` on 8123 and `serve-ttl.py` (`JOB_TTL_MINUTES=1`) on 8124, with no console problems:
    - A 1.1–1.2 s range enabled Export and saved `long (clip 0m01.1s-0m01.2s).mp4`.
    - An MPEG-4 Part 2 upload (`Old camera.mp4`, which Chrome can't play: `error.code` 4) showed the note, disabled Set to playhead ×2 and Play clip, and ignored `I`. Typed times still exported it.
    - With a 60 s idle time, the keep-alive polled at 15 s gaps (6 in 100 s), and the source was still `ready` after 100 s.
- Whole suite at `1f31a98`: `.venv/bin/pytest -q` gave **673 passed, 2 skipped, 7 deselected** (118 s), against the 599-passed baseline. The downloader's tests all still pass.
- **Docker, at `4aaa323`.** The image was rebuilt as `saveme:clip-studio`. The live `saveme-saveme-1` (`saveme:local`) wasn't touched and stayed up.
  - `scripts/docker-ffmpeg-check.sh saveme:clip-studio`: every matrix case passed, and **441 integration tests** passed inside the image against the minimal FFmpeg 9.0.2 (387 before), including every batch worker test.
  - `scripts/docker-smoke.sh` gave SMOKE OK. It ran in an isolated Compose project: `COMPOSE_PROJECT_NAME=saveme-ctest`, `COMPOSE_FILE=compose.yaml:<override with image saveme:clip-studio>`, `SAVEME_PORT=8001`.
  - In that hardened container (`container-zip.sh`), an upload of `Beach day.mp4`, then a 2-clip batch, gave `Beach day (2 clips).zip`:
    - `application/zip`, with 2 stored entries (54,256 and 90,802 bytes) and `testzip` clean;
    - the job `expired`/`delivered` once saved;
    - 0 job folders left in `/tmp/media-jobs` after Close.
  - The `saveme-ctest` project was then removed.
- **Independent review** of `b661127..HEAD` (a fresh general-purpose agent, read-only, given the code and not my conclusions) found no important defects. It confirmed:
  - no ZIP can come out of the downloader, and zip slip is blocked;
  - the size and disk limits hold, and the 2× peak sits inside the reservation and the watchdog;
  - cleanup on cancel and failure, the batch counting as a use of its source, and worker messages reaching the page;
  - the millisecond rule (it fuzzed `setMark` with 2M values in node).

  Its minors:
  - The list wasn't clipped when only the browser knew the length, and a restored list was only loosely checked. Fixed in `eaff720`.
  - Focus dropped to the page when Export (clip or ZIP) or a playhead button disabled itself. Fixed in `d181237`. Export clip had this too.
  - Playhead controls stay off after any video error. Kept on purpose; see Key Decisions.
  - A second export during the 60 s "Saving…" window drops the first one's tracking. This already existed; see Pending Work.
- Review fixes in Chrome (`check-review.js`, no console problems):
  - Export clip and Export ZIP started with Enter both left focus on `#export`.
  - A tampered restore list (`[5,7.0004]`, a repeat, `[3,3.05]`, `[-1,2]`, `[9.5,99]`, `[12,13]`, junk) came back as 1–2, 5–7 and 9.5–10 s.
  - A video error with focus on Set to playhead moved focus to the preview note.
- **Final whole suite at `d181237`: 673 passed, 2 skipped, 7 deselected** (104 s).

### Key Decisions

- **One `batch` job cuts every range and writes `media.zip`.** The other options were N clip jobs zipped at delivery (only 1× disk, but N queue places and a new delivery lifecycle) and zipping in the browser (memory, plus a ZIP writer in JS). One job reuses the queue, slot, progress, cancel, one-shot delivery, TTL and cleanup as they are.
- **The ZIP is written by Python's `zipfile`, stored (no compression).** No FFmpeg build change and no new dependency. Video doesn't compress further.
- **`zip` is not a download media type.** `ZIP_MEDIA_TYPE` is separate from `_MEDIA_TYPES`, so a link to a `.zip` file still can't be delivered by the downloader.
- **Entry names come from the server and are checked again in the worker.** They're numbered (`01 <title> (clip …).mp4`) so they sort in list order and stay unique. `is_plain_filename` refuses separators, control and Windows-forbidden characters, leading dots, over 255 bytes, and repeats (case-insensitive).
- **The batch's size limit covers the whole ZIP.** Each cut's `-fs` is the room left, with 1 KiB counted per entry for headers, and the finished ZIP is checked again.
- **The list lives in the page, not on the server.** It's kept in `sessionStorage` with the marks, sorted by start so the ZIP follows the video. Duplicates are refused in the page. The server would accept them, and numbering keeps their names apart.
- **Playhead controls stay off for the rest of that studio after an `error` event.** A media element in error doesn't recover without its source being loaded again. Close and reopen brings them back.
- **Playhead controls are turned off only after a media `error` event,** not whenever the picture size is 0. Chromium plays the sound of an HEVC file it can't show, and that playhead moves, so the controls still help there.
- **Keep-alive is `idle_minutes / 4`, clamped to 15–60 s,** and rescheduled once `/api/studio` answers. The default (30 min) still polls every 60 s.
- **A refused ZIP request shows its error in the export area under the list,** next to the button that was pressed, not in the message line under the editor.
- **A batch reserves `2 × MAX_FILE_SIZE_MB`, like a download.** The ZIP and the part being added sit on disk together. At the smallest allowed budget, with a source open, a batch gets `disk_full` while single clips still work. That's accepted, and the test documents it.

### Issues Encountered

- With `-read_intervals %+#5`, a frame check saw nothing: the hidden keyframe lead-in uses the first packets. It now reads 30, as the clip test does.

### Pending Work

- **Ship `main`.** The owner asked for the merge, and `feature/clip-studio` was merged into local `main` with `--no-ff`. The merged tree is identical to the tested branch tip. Local `main` is not pushed: it is ahead of `origin/main` by the earlier Clip Studio merge and this one. To ship:
  1. `git push origin main`
  2. Rebuild the live container with `docker compose up -d --build`. It still runs the image built 16 hours earlier, before Clip Studio.
- **Known limitations:**
  - Clips are stream copies, with no re-encode. MKV and WebM sources (rare; most become MP4) show the keyframe lead-in, and players that ignore MP4 edit lists show it too.
  - At the smallest disk budget (`MAX_TOTAL_DISK_MB` = 2 × `MAX_FILE_SIZE_MB`), a ZIP is refused with `disk_full` while a source is open. Single clips still work.
  - A second export during the 60 s "Saving to your device…" window replaces the first in the page. If the first auto-save was blocked, its file waits on the server until `JOB_TTL_MINUTES`.
  - Export Cancel was tried in Chrome only on short test cuts. It won the race once, and the API and manager tests cover it.
- **Deferred minors still open (the owner's call):** `/media` has no time limit; a ProRes MOV is accepted, then every cut fails; the source cap replaces an idle source before the disk check; no test closes an upload during its check.
- **Leftover images:** `saveme:clip-studio` (rebuilt today) and `saveme-ffmpeg-check`. Remove them with `docker image rm` when done.
- **Phase 2** (transcripts, highlight suggestions, 9:16 reframing, burned-in subtitles) needs an H.264 encoder in the FFmpeg build, and its own security and licensing review first. No AI or transcription work was started.

### Handoff

- Branch `feature/clip-studio` at the last journal commit. Each step is one commit plus a journal update.
- Designs:
  - `docs/superpowers/specs/2026-11-24-clip-studio-phase-1-design.md`, the base. Its non-goals now point to the addendum below.
  - `docs/superpowers/specs/2026-11-24-clip-studio-batch-export-design.md`, the clip list and ZIP.
- Code map for the batch export:
  - `app/mediafile.py` `cut_batch`, `MAX_BATCH_CLIPS`;
  - `app/worker.py` `run_batch`, `_batch_args`;
  - `app/jobs.py` `submit_batch`, plus `_CUT_KINDS` and the `batch` branches in `_clip` and `_make_ready`;
  - `app/studio_routes.py` `create_batch`, `app/schemas.py` `BatchRequest`, `app/main.py` `_validation_message`;
  - `app/filenames.py` batch names and `is_plain_filename`;
  - `static/studio.js`, in the "clip list" section and `startExport(kind)`.
- Browser and container checks:
  - The scripts live in `$CLAUDE_JOB_DIR/tmp/dev/` (`/Users/unnikrishnan/.claude-max/jobs/0dd87ae1/tmp/dev/`), which is temporary and deleted with this job: `serve.py`, `serve-ttl.py`, `check-zip.js`, `check-minors.js`, `check-review.js`, `container-zip.sh` and `compose.ctest.yaml`.
  - Run each with `PWCORE=<~/.npm/_npx/*/node_modules/playwright-core> node <script> <media URL> …`. The URL is the one `serve.py` prints.
  - Recreate them from this description if they're gone.
- Tests: `.venv/bin/pytest -q`. Real FFmpeg and ffprobe must be on `PATH` (Homebrew on this Mac).
- Never run `scripts/docker-smoke.sh` bare. It runs plain `docker compose`, which would replace the live `saveme-saveme-1`. Set `COMPOSE_PROJECT_NAME=saveme-ctest`, `COMPOSE_FILE=compose.yaml:<override setting image saveme:clip-studio>` and `SAVEME_PORT=8001`, then run `docker compose down` with the same variables.

---

## 2026-11-24 06:58 IST — Session: Clip Studio, Phase 1 (manual clipping)

### Summary

The owner asked for Phase 1 of an integrated Clip Studio, on the way to an [AutoClip](https://github.com/zhouxiaoka/autoclip)-style tool: load a video, preview it, pick a start and end, cut it on the server with FFmpeg as a cancellable job with progress, export it, and clean up after. The existing downloader, UI conventions, URL protections, limits and tests must stay, and there must be no React, no database and no paid services. They chose to accept videos both from links and from their own computer. Work is on `feature/clip-studio`, following the design (`docs/superpowers/specs/2026-11-24-clip-studio-phase-1-design.md`) and plan (`docs/superpowers/plans/2026-11-24-clip-studio-phase-1.md`), one task at a time, with a journal update after each task.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `b899daf` | docs | Add Clip Studio phase 1 design |
| `b040d4d` | docs | Add Clip Studio phase 1 implementation plan |
| `7adb750` | refactor | Move local media helpers to `app/mediafile.py` and report duration |
| `46f9878` | feat | Add a worker probe mode that checks and remuxes uploaded videos |
| `f7bcebd` | feat | Add a worker clip mode that cuts by stream copy with progress |
| `06424ea` | feat | Keep clip sources in the job manager with close, idle expiry and a cap |
| `8e53efa` | feat | Run clip and upload jobs in the job manager |
| `b2dd526` | feat | Add Clip Studio API routes for sources, preview and clips |
| `049063b` | feat | Accept video uploads for Clip Studio with their own limits |
| `2e7be93` | refactor | Share the page's API and DOM helpers through `common.js` |
| `9cc0546` | feat | Add the Clip Studio page |
| `ae92308` | docs | Describe Clip Studio and check it in the image scripts |
| `f0c4c75` | test | Read whole replies in the raw-socket upload tests |
| `8ddb5b8` | fix | Count a job's own files inside its disk reservation |
| `9e6e62c` | fix | Accept WebM recordings that carry no duration |
| `24e6cb5` | fix | Close late sources, drop gone videos, keep unsaved files on the page |

### Test Results

- Baseline on `main` (`bea4549`): `.venv/bin/pytest -q` gave 471 passed, 2 skipped, 7 deselected (82 s).
- Task 1 (`7adb750`): 475 passed, 2 skipped, 7 deselected.
- Task 2 (`46f9878`): 495 passed, 2 skipped, 7 deselected. The new probe tests cover MP4 kept, MKV and TS remuxed to MP4, WebM, audio-only (`no_video`), random bytes (`unreadable_media`), a source outside the folder or symlinked (`internal`), and the container mapping.
- Task 3 (`f7bcebd`): 511 passed, 2 skipped, 7 deselected. A 3.3–7.3 s cut of a 10 s video (keyframe every second) shows 102 frames (4.08 s at 25 fps), its first shown frame is at 0, and its audio is 4.01 s long.
- Task 4 (`06424ea`): 526 passed, 2 skipped, 7 deselected. 15 new source tests: link source to ready, progress and failure, separate ID spaces, not delivered or counted as a ready file, not evicted for disk room, idle expiry and touch, close in each state, cap replace and busy, queue places, and `MAX_CLIP_SOURCES` bounds.
- Task 5 (`8e53efa`): 551 passed, 2 skipped, 7 deselected. New tests: clip request and progress, delivery and naming; each range rule; end clamping; no resume; closing a source cancels its running and queued clips; a pending clip keeps its source alive; upload clips drop tags; the upload lifecycle, failure, abort, close-then-finish, disk admission counting active jobs, and the source cap.
- Task 6 (`b2dd526`): 569 passed, 2 skipped, 7 deselected. 18 new API tests: limits; the link source flow with Range (206) and `no-store`; 409 before ready; a clip end to end (stub) with its download name; eight request-validation cases plus `Infinity`; separate ID spaces on every route; close needs a matching Origin, DELETE is 405, a repeat is harmless; a source is gone after a restart; URL and address checks; a source takes a queue place.
- Task 7 (`049063b`): 589 passed, 2 skipped, 7 deselected. 20 new upload tests:
  - Header checks: content type 415, chunked 411, empty 400, too large 413.
  - Only the upload path skips the 64 KiB cap. Origin, Host and degraded mode still apply.
  - Title parsing.
  - Through a real uvicorn server: a client that leaves mid-body frees its folder and queue place; an idle client gets 504 `timeout`; a source closed mid-transfer gets 404 and is removed.
- Task 8 (`2e7be93`): 590 passed, 2 skipped, 7 deselected. In headless Chromium, a direct MP4 from the local media server fetched, downloaded (198,451 bytes) and showed "Saved", with no console errors.
- Task 9 (`9cc0546`): 593 passed, 2 skipped, 7 deselected. Browser checks in the installed Google Chrome (a temporary profile) and Playwright's Chromium, desktop at 1200 px and phone at 375 px in dark mode, none with console errors:
  - Link → Clip → typed marks, an out-of-order start refused with a message, handle arrows and Shift, `I` at the playhead → Export → auto-saved `long (clip 0m01s-0m06s).mp4` (H.264+AAC) → "Clip saved". A reload restores the marks; Close unlocks the link field and clears storage.
  - Uploads: a TS file is remuxed and its clip carries no tags; random bytes show "SaveMe can't read this file as a video…"; an HEVC MOV previews in Chrome. In Chromium (no HEVC) the fallback note shows and a clip still exports from typed times.
  - Cancel while a link source downloads: the server has the source `expired`/`closed`, and the result card returns.
  - Server restart, then a reload: "This video is no longer on the server…", storage cleared, no horizontal scroll at 375 px.
- Task 10 (`f0c4c75`): 595 passed, 2 skipped, 7 deselected.
  - `scripts/docker-smoke.sh` passed on the branch image in an isolated Compose project (`saveme-ctest`, port 8001, image tag `saveme:clip-studio`, so the live `saveme-saveme-1` and its `saveme:local` tag weren't touched). That includes the new check: `/api/studio`, and an upload of random bytes refused as `unreadable_media` by the probe inside the hardened container. The project was then removed.
  - `scripts/docker-ffmpeg-check.sh saveme:clip-studio` passed every matrix case, including the new MP4 and MKV cuts, and 383 integration tests inside the image against the minimal FFmpeg 9.0.2. The clip test's "first shown frame at 0" holds on that build.
- Whole-branch review by a fresh reviewer (Opus): "ready after fixes". It found nothing critical and accepted every ruling in the ledger. The fix pass (`8ddb5b8`, `9e6e62c`, `24e6cb5`) ended at 599 passed, 2 skipped, 7 deselected; each fix's test failed first. Then:
  - A browser check of the three page fixes passed, and every earlier browser check was rerun and still passes.
  - The image was rebuilt (`saveme:clip-studio`), and `docker-ffmpeg-check.sh` passed again with 387 integration tests in the image.

### Key Decisions

- **Cuts are stream copy, not re-encodes.** The Docker FFmpeg has no video encoder and only one video decoder, by design. A test cut with FFmpeg 8 shows the keyframe lead-in hidden by an MP4 edit list: a 7.0 s start on a video with keyframes every 5 s showed its first frame at 0.000. So browsers, QuickTime and VLC start exactly where the owner chose, and the build stays as it is. Re-encoding (and with it vertical reframing and burned-in subtitles) is left to the phase that needs an encoder.
- **Sources and clips are new kinds of job in `JobManager`.** That way they share the queue, slots, disk budget, sweep and shutdown, and no limit is copied or bypassed.
- **Uploads are raw `application/octet-stream` bodies with a `Content-Length`.** Only this path skips the Guard's 64 KiB read. Host, Origin, degraded mode, the size cap, the disk check and the timeouts still apply, and no form parser is added.
- **A source is closed only by the owner, idle time or the source cap, never to make disk room.** Evicting it would pull the video out from under someone mid-edit, so a job that doesn't fit gets `disk_full` instead, as today. The cap (`MAX_CLIP_SOURCES`, default 2) replaces the least recently used idle source.
- **The preview fallback also fires when the video has no picture size.** Chromium played an HEVC file's sound and skipped its picture without an error event. Every source is checked to have video, so a zero `videoWidth` means the browser can't decode it.
- **An upload is admitted only if it fits the disk budget counting active jobs.** Its bytes land before any slot is taken, so the lighter check that downloads get at submit time would let several big uploads overrun the budget.
- **A clip reserves `MAX_FILE_SIZE_MB` of disk, not twice that.** It's a stream copy that `-fs` stops at that size. With the old 2× reservation, nothing could be exported at the smallest budget the settings accept (`MAX_TOTAL_DISK_MB` = 2 × `MAX_FILE_SIZE_MB`), because a ready source can't be evicted.
- **A job about to start counts its own files inside its reservation** (max of what it holds and its reservation), so an admitted upload, or a resumed download, isn't charged twice when it asks for a slot.
- **Clips cut from uploads carry no metadata,** because phones write location and device details into videos. Clips cut from links keep title, artist and date, and chapters are dropped because their times would be wrong.

### Issues Encountered

- `tests/support/media.py`'s `junk.jpg` isn't junk to a full ffprobe: it opens the file as a one-frame image because of the `.jpg` name. Junk-input tests use `big.mp4` (random bytes) instead.
- **The final review found one Important bug and twelve Minor issues.**
  - The Important bug was a disk double count: an admitted upload stalled on "Waiting for storage space" at small budgets, and clips couldn't be exported at all at the minimum budget.
  - Five Minor issues were re-graded Important by their effect and fixed: a stuck record when an upload's folder can't be made; a gone video still streaming a deleted file; live WebM recordings refused; a source left downloading after an early Close; and a ready, unsaved file dropped when opening the studio.
  - The rest are listed under Pending Work.
- **The plan's "cut H.264 TS into MP4" image check failed.** A TS has no index, so a seek lands after the keyframe; with one keyframe the video track disappears, in Debian's full FFmpeg and the minimal build alike. The app never cuts a TS (uploads are remuxed to MP4 first), so the case became an MKV cut. Single-keyframe MP4, MKV and remuxed-TS cuts were checked in the image. An MKV or WebM source shows the keyframe lead-in (no edit lists), and the README now says so.
- Two raw-socket upload tests failed once in the full run: one `recv()` returned only the headers. Fixed in `{fix}` by reading to `Content-Length`; stable over 8 reruns.
- The first fallback check failed: an undecodable HEVC picture raised no `error` event, so the note never showed. Fixed before the commit by checking `videoWidth` once metadata loads.
- A stream-copy cut's container and track durations read longer than the cut (4.22 s for 4.0 s), because they count the hidden keyframe lead-in. The clip test checks what a viewer gets instead: shown frames, the first shown frame at 0, and the audio length.

### Pending Work

- **Deciding how to integrate `feature/clip-studio`.** It isn't merged or pushed, and the live container still runs `main`. After a merge, rebuild the live container with `docker compose up -d --build`.
- **Deferred minors from the final review,** for the owner to rule on:
  - `/media` responses have no idle or total time limit, unlike `/file`.
  - A ProRes (MP4-incompatible) MOV is accepted, then every cut fails with "Try a different start or end". Remux MOV like the other containers, or check codecs at probe time.
  - Exactly-0.1 s ranges: the page enables Export, but the server can refuse after rounding. Compare in whole milliseconds on both sides.
  - The source cap replaces an idle source before the disk check, which can then still fail.
  - With no preview, Set to playhead, Play clip and the `I`/`O` keys act on a playhead stuck at 0.
  - The keep-alive polls every 60 s, so `JOB_TTL_MINUTES=1` can expire a visible studio. Derive it from `idle_minutes`.
  - A too-large clip's message suggests "a lower quality or audio only".
  - No test closes an upload while its check runs, or keeps a source alive through API status polls.
- Leftover images from the checks: `saveme:clip-studio` and `saveme-ffmpeg-check` (`docker image rm` them when done).
- Export Cancel hasn't been tried in a browser; harness cuts finish in under a second. The manager and API cancel tests cover it.
- Browser checks run through `/tmp/claude-501/saveme-dev/` (outside the repo):
  - `serve.py` runs SaveMe on `127.0.0.1:8123` with loopback allowed, plus the test media server, whose URL it prints.
  - The `check-*.js` scripts drive headless Chromium with the `playwright-core` from `~/.npm/_npx`.

  The Playwright MCP browser is locked by a Chrome started on 23 Nov at 18:26 that this session left alone.
- Known gap: an upload source that fails while waiting for a slot (`disk_full` at its deadline) leaves its folder until the next sweep (at most `sweep_interval`, 60 s), not at once.

### Handoff

- All 10 plan tasks and the review fix pass are done. The deviations from the plan are recorded above:
  - Under Issues Encountered: the clip test measuring shown frames instead of container duration, the MKV image-check case instead of TS, and the `videoWidth` preview fallback.
  - Under Key Decisions: the disk reservations.
  - Under Pending Work: the deferred review minors.
- Browser checks used scripts in `/tmp/claude-501/saveme-dev/` (outside the repo, temporary): `serve.py` is a loopback-allowed dev server; `check-studio-*.js` and `check-review-fixes.js` drive Chrome through `playwright-core`. Recreate them from this description if `/tmp` is cleared.
- Phase 2 (AutoClip-style: transcripts, highlight suggestions, 9:16 reframing, burned-in subtitles) needs an H.264 encoder in the FFmpeg build and its own security and licensing review first. See §7 of the design doc.
- Tests: `.venv/bin/pytest -q`. Real FFmpeg and ffprobe must be on `PATH` (Homebrew on this Mac).

---

## 2026-11-23 19:21 IST — Session: File tags, cover art and M4A audio

### Summary

The owner asked me to execute the latest plan (`docs/superpowers/plans/2026-11-23-file-tags-and-m4a.md`). It was run task by task with a fresh implementer and a reviewer per task, then a whole-branch review and one fix wave, all on `feature/file-tags-m4a`. Saved files now carry title, artist, date, description and chapters, never the source link (`purl`/`comment` are blanked). They also get the source's JPEG or PNG thumbnail as cover art. The worker fetches it through its guarded `urlopen` (at most 5 MB, at most 3 URLs, 5 s per request, 15 s overall) and mutagen embeds it, so FFmpeg never reads an image. A new "M4A audio (original)" choice (`output: "m4a"`) stream-copies AAC and appears only when the source really has AAC; `"audio"` still means MP3. "Best" now reads like `Best (2160p60 · AV1/VP9)`. The FFmpeg build gained only the `ffmetadata` demuxer.

**Verified:**
- Offline suite at `a57f600`: 471 passed, 2 skipped, 7 deselected.
- `scripts/docker-ffmpeg-check.sh` on an image built from `a57f600`: every case passed, including "chapters via ffmetadata", and 270 integration tests passed against the minimal FFmpeg (Linux).
- At `0176c82`, in an isolated Compose project on port 8001: the smoke test passed, and `pytest -m network` passed 6 tests, including the new YouTube M4A/tags/cover test. Reddit failed with `login_required` as before. That project was then removed.
- A real-browser check at 375 px and desktop, light and dark: the M4A chip shows for YouTube and not for a direct MP4, and a pre-upgrade stored MP3 job still shows "MP3".
- A real YouTube M4A came out with title, artist, date and a cover. The cover worked only after `7f94a67`.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `eb9980b` | feat | Tag downloads with title, artist, date and chapters, never the source link |
| `7e2c94b` | feat | Embed the source thumbnail as cover art with mutagen, best effort |
| `c9ecb8c` | feat | Offer M4A audio that stream-copies the source's AAC |
| `23af5c6` | feat | Label Best with its real size, frame rate and codecs |
| `81c2d0b` | feat | Add the M4A audio choice to the page |
| `7f94a67` | fix | Always try the source's own thumbnail for cover art |
| `0176c82` | docs | Describe M4A, file tags and cover art |
| `a3a4904` | fix | Bound the cover-art fetch with an overall time budget |
| `8de7a91` | fix | Cap the description written into tags at 16 KiB |
| `1cd50f9` | fix | Offer and select M4A only for audio that really is AAC |
| `a57f600` | docs | State when files get cover art |

### Key Decisions

- **The source's own `thumbnail` is always one of the 3 cover tries.** yt-dlp guesses YouTube's high-res names (`maxresdefault`, `hq720`, `sddefault`), and on older videos all three 404. When the source's own `thumbnail` isn't already among the best 3, the tries are the best 2 plus it. Newer videos still get maxres first.
- **The cover fetch runs in a daemon thread with a 15 s join, not SIGALRM.** urllib3 blocks until a whole block arrives, and `socket_timeout` limits only each receive, so a slow host could hold a finished job until it timed out. Signals are process-wide. An abandoned thread holds at most 5 MB, writes nothing and dies with the worker.
- **"Has AAC" is one case-insensitive pattern, used for both inspect and the format selector.** It accepts `aac`, bare `mp4a`, `mp4a.40.<n>` except 32–34, and `mp4a.66`–`68`. The spec's goal is "offered only when the source has AAC". The plan's `^=mp4a` also matched MP3, AC-3 and Opus in MP4, and the build has no AAC encoder to fall back on, so those jobs would fail after the whole download. This overrides the plan's exact selector string, and the spec and plan still show the old one.
- **The description in tags is capped at 16 KiB.** FFmpeg gets each tag as one argument, and Linux limits one argument to 128 KiB. Without the cap, a very long description would turn a link that used to work into `internal`.
- **Every M4A FFmpeg step reads "Preparing M4A".** The AAC HLS path runs `FixupM3u8` before `ExtractAudio`, and "Converting to MP4" on an M4A job would mislead.
- **An invalid `output` stays a 400 `invalid_request`.** The plan said 422, but that was never the app's behaviour, and changing it would break clients.
- **Docker checks ran in an isolated Compose project (`saveme-ftest`, port 8001).** The plan's bare `docker compose up -d --build` would have replaced the live `saveme-saveme-1` container with an unmerged branch.

### Issues Encountered

- **No cover on a real YouTube download.** The browser check in Task 5 caught this, and the guessed-names root cause was found with a probe. Fixed in `7f94a67`.
- **The whole-branch review found an unbounded cover fetch.** A 1 byte/s host could keep a finished job busy until `MAX_JOB_SECONDS`. Fixed in `a3a4904`, along with three smaller fixes.
- **Tagging rewrites the file,** so `test_resume_continues_a_real_partial_download` could no longer compare bytes. It now checks for an MP4 header, and its Range assertion still proves the resume.
- **A lone-surrogate title crashed FFmpeg's arguments** (`UnicodeEncodeError`). `info` is now scrubbed before processing, through `app.runner`'s private `_replace_lone_surrogates`. That should move to a shared module.
- **The permission check denied a subagent's `sed` edit of one test assertion** (`test_worker_inspect.py`, "Best (360p)" → "Best (360p · H.264)"). The owner approved the edit and it was redone with Edit.
- **Known limitations:**
  - Only the description is capped, so a huge title or tag list could still exceed the argument limit.
  - The AAC pattern still admits rare non-AAC `mp4a.40.<n>` values such as ALS (36).
  - An abandoned cover thread could still print a log line.
- **Wrap-up:**
  - Merged into `main` as `9b04e99`. The full suite passed on the merged result (471 passed), and `main` was pushed to GitHub.
  - The live `saveme-saveme-1` container was rebuilt from `main` with `docker compose up -d --build`. It came back healthy, `/health` answers 200 on port 80, and the served `app.js` has the M4A chip.
  - The spec and plan now record the stricter AAC rule that replaced the `^=mp4a` selector.

---

## 2026-11-10 17:26 IST — Session: Security hardening (#3–#8)

### Summary

The owner asked me to go through the open issues, fix the ones that need fixing, merge into `main` and close them. #1 and #7 were already fixed on `feature/security-quick-fixes`. #3, #4, #5, #6 and #8 are fixed on `feature/security-hardening`, which was branched from it. #2 (authentication) stays open: the issue itself says it is needed only before public or shared hosting, and the app is meant for one person on loopback.
- **#8:** a POST's `Origin` must equal the request's own origin (scheme, Host-header host, effective port), not just an allowed host.
- **#3:** POST bodies are read by the Guard and capped at 64 KiB (chunked too) before JSON parsing; raw link input is capped at 8,192 characters before stripping; DNS lookups run inside the inspection slot or a held queue place.
- **#5:** at most `MAX_ACTIVE_JOBS + MAX_QUEUED_JOBS` resumable cancelled jobs (a later cancel ends the oldest window), and at most 100 finished records.
- **#6:** one download per file at a time (409 `delivery_in_progress`); a file being sent stays on disk and in the budget and is skipped by TTL, eviction and the ready cap; a client idle for 60 s or still downloading after an hour is cut off, and the file stays for another try.
- **#4:** whole-body reads in the worker are capped at 16 MiB decoded (`source_too_large`); the playlist unwrap is lazy; each worker sets `RLIMIT_DATA` to 1 GiB, inherited by deno and ffmpeg.

A parallel session (README for the public release) committed on this same branch while I worked, because the two sessions share the working tree. Its commits touch only `README.md`, `JOURNAL.md` and `.gitattributes`, and its README rewrite kept my two README notes (#5, #6).

**Verified:**
- Offline suite on macOS: 419 passed, 2 skipped (the Linux-only memory tests). `test_readme_hedges_the_reddit_cause` failed for a while after the parallel README rewrite; that session was told and fixed it in `6360cfc`.
- `scripts/docker-ffmpeg-check.sh` on an image built from the branch: 249 integration tests passed on Linux, including the two `RLIMIT_DATA` tests.
- The same image, run as a throwaway container with Compose's limits: `pytest -m network` passed YouTube, youtu.be, direct MP4, unsupported and private-IP. Reddit failed with `login_required`, exactly as on the old container and as the README documents.
- Memory in that container on a real YouTube download: deno peaked at 563 MiB data segment (314 MiB RSS), the worker at 76 MiB (109 MiB RSS); all five processes showed a 1 GiB `Max data size`.
- Each new test failed first for the missing behaviour. The two delivery-timeout tests failed first only on a missing option, so each timeout was disabled by hand to confirm its test catches the break.
- 413 replies were also checked on a real uvicorn server, with and without `Content-Length`.

### Changes Made

| Commit    | Type | Description |
| --------- | ---- | ----------- |
| `84410d6` | fix | Require a POST's Origin to be the request's own origin (#8) |
| `3b4eef1` | fix | Cap request bodies and resolve links inside the concurrency caps (#3) |
| `f868a3e` | fix | Bound cancelled and finished job records by count (#5) |
| `3b19dd8` | fix | Deliver each file to one client at a time, with deadlines (#6) |
| `767b916` | fix | Bound worker memory during extraction (#4) |
| `7a3b1c4` | docs | Describe the page, memory, body and origin limits under Security |

### Key Decisions

- **The expected Origin comes from the request, not a new setting.** A configured origin list would have to track `SAVEME_PORT` and any proxy; the Host header is already allowlisted, and the page is always same-origin.
- **DNS inside the slot, syntax check outside it.** A malformed link still never takes a slot. A blocked or unresolvable link holds one only for the lookup (at most 5 s), and while busy it gets "busy" instead of its own error. For jobs, a link being resolved holds a queue place, so lookups never exceed the queue.
- **No time-based rate limits.** The concurrency and count bounds cap memory, CPU and lookups. Per-user quotas belong with authentication in #2.
- **Delivery is claimed inside the response's `__call__`, not the route handler.** Claim, open, send and release share one try/finally, so a claim can't leak and pin a file forever.
- **A file being sent stays on disk rather than being unlinked under the reader.** It then stays honestly counted in the budget; the deadlines bound how long a slow client can hold it.
- **The 1 GiB worker limit is a constant, sized from a measurement.** deno inherits it and needs about 570 MiB, so a lower cap would break YouTube. `RLIMIT_DATA`, not `RLIMIT_AS`: V8 reserves large address ranges it never touches.
- **The page cap wraps `YoutubeDL.urlopen`.** Extractors and downloaders all open responses there, and only reads with no size are capped, so media downloads (read in blocks) are untouched.
- **macOS refuses any `RLIMIT_DATA` change,** so the worker logs that and runs without it; the Docker image (Linux) enforces it.

### Issues Encountered

- **A parallel session shares this working tree** and committed README, journal and `.gitattributes` changes onto `feature/security-hardening`. They don't overlap my files. To avoid moving the branch under it, `main` is merged from a temporary worktree.
- **The first README edit for #4 missed** its anchor line, because the README had been rewritten meanwhile. The note went into the rewritten README's Security section afterwards (`7a3b1c4`).
- **The trickle-reader test failed on Linux only:** a 4 KiB receive buffer gives 8 KiB reads there, so the slow drain outlasted the timeout. The test now trickles only past the deadline, then drains at full speed.
- **Known limitation:** ending a resume window early (the #5 cap) can remove partial files while the cancelled worker is still being killed (up to its 5 s grace). The worker may write into a removed folder, and the next sweep removes anything it leaves.
- **Not done:** the running `saveme-saveme-1` container still uses the old image, and nothing is pushed.

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
| `53e74bb` | docs | State that SaveMe has no login by design |

The owner then asked for the `.gitattributes` and a repository description. The description is now set on GitHub: "A private, self-hosted media downloader. Paste a link and save it as MP4 or MP3. Runs locally in Docker, built on yt-dlp and FFmpeg." A clone with `core.autocrlf=true` had 89 CR bytes in `scripts/build-css.sh` at `3b02d7b`. At `603fe8e` that file and both `app/bin` wrappers had none.

### Key Decisions

- **"Public" means the repository, not hosting.** The README keeps a prominent warning never to expose the app to a network.
- **No login, by the owner's decision.** SaveMe is for one person running it locally, so authentication isn't planned. The README no longer links #2 as a future step, and Contributing lists logins, accounts and hosting for others as out of scope. Issue #2 is still open on GitHub.
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
