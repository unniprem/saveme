# AI Smart Clipping (Phase 2) — Design

Status: implemented on `feature/ai-clipping` (see `docs/AI_CLIPPING_IMPLEMENTATION.md` for the
code map and `docs/OPENROUTER_SETUP.md` for setup). Date: 2026-11-24.

Phase 2 adds **AI Highlights** to Clip Studio: transcribe the open video locally with
faster-whisper (or import its subtitles), send transcript text — never audio or video — to an
OpenRouter model, and turn its answers into ranked, editable clip suggestions that feed the
existing Phase 1 clip list, single-clip export and ZIP export.

## 1. Audit of the starting point (Task 1)

Verified on `main` at `206d26b` (Phase 1 merged), branch `feature/ai-clipping` cut from it.
Baseline: `.venv/bin/pytest -q` → 673 passed, 2 skipped, 7 deselected (journal, 24 Nov).

| Area | What exists | How Phase 2 uses it |
| --- | --- | --- |
| `app/jobs.py` `JobManager` | In-memory jobs: `download`, `source`, `clip`, `batch`. Shared slots (`MAX_ACTIVE_JOBS`), queue, disk budget, TTL sweep, one-shot delivery. A *source* is `TEMP_DIR/<id>/media.<ext>`, kept while a studio is open, closed on request, after `JOB_TTL_MINUTES` unused, or when `MAX_CLIP_SOURCES` is reached. | Read-only use of ready sources. Two small hooks added: a *hold* (an analysis keeps its source from idling out or being replaced) and a *source-ended* listener (closing a source cancels its analysis). AI work never takes a download slot or queue place. |
| `app/runner.py` `run_worker` | Subprocess runner: JSON request on stdin, JSON events on stdout, own process group, kill on cancel/timeout, workdir size watchdog, stderr kept out of client messages. | Runs the new transcription worker unchanged. |
| `app/worker.py` | `python -m app.worker`: memory cap (RLIMIT_DATA 1 GiB), connect guard, modes inspect/download/probe/clip/batch. | Untouched. Transcription gets its **own** worker (`python -m app.transcriber`) so it can have a model-sized memory cap and a guard that refuses *every* connection. |
| `app/mediafile.py`, `app/bin/ffmpeg` | All FFmpeg calls go through wrappers pinning `-protocol_whitelist file,pipe`. Minimal FFmpeg build in Docker (no network, stream copy, audio decoders, MP3 encoder, `aresample`). | Audio extraction uses the same wrapper. The Docker build gains exactly two components: the `pcm_s16le` encoder and the `wav` muxer. |
| `app/main.py` `_Guard` | Host allow-list, same-origin check on POST, 64 KiB POST body cap (upload route exempt), security headers, CSP `connect-src 'self'`. | New POST routes inherit all of it. Subtitle import is the second route that streams its own body (with its own 4 MiB cap). OpenRouter is called server-side, so the CSP is unchanged. |
| `static/studio.js` | Clip Studio: preview, timeline, marks, clip list (≤ 20, `sessionStorage`), export clip, export ZIP. | Exposes a small bridge (`window.SaveMeStudioBridge`) so the new `static/ai.js` can read the open source and add ranges to the list, load a range, preview it or export it — no duplicated clip logic. |
| Docker | `python:3.12-slim-trixie`, uid 10001, read-only app code, compose binds `127.0.0.1` only, `cap_drop: ALL`, 2 GiB memory. | Adds faster-whisper, httpx, and the baked Whisper model (`base`) under a root-owned `HF_HOME`; runtime is offline for models. |

Risks found and how the design handles them:

1. **Memory.** faster-whisper builds features for the whole input at once. Measured on this
   M2 (20 min of speech, `base`, int8, 2 threads): 600 s windows peaked at 1.10 GB RSS, 300 s
   windows at 0.74 GB, 120 s at 0.62 GB. → Transcribe in **300 s windows** cut at the quietest
   point near each boundary, with a per-model RLIMIT_DATA cap.
2. **Speed.** VAD off took 228 s for 20 min; VAD on 76–81 s (≈ 15× real time). → `vad_filter=True`
   (also suppresses hallucinated text over music/silence).
3. **Model downloads at runtime** would need network from a worker and leak usage to Hugging Face.
   → Models are fetched explicitly (Docker build, or `python -m app.whisper_models`), and the
   transcriber loads them with `local_files_only=True` behind a deny-all connect guard.
4. **PyAV.** faster-whisper depends on `av`, which bundles a full FFmpeg. SaveMe never hands it
   media: audio is decoded by the hardened FFmpeg wrapper to 16 kHz WAV and passed to Whisper as
   a NumPy array.
5. **The 64 KiB POST cap** is too small for subtitle files → a dedicated streaming route.
6. **Downloads must stay independent.** → Separate semaphore (`AI_MAX_CONCURRENT_JOBS`, default
   1), separate settings (an invalid AI setting disables only AI, never downloads), nice(10)
   on the transcriber.

## 2. Architecture

```
Browser (Clip Studio)                     FastAPI process                              Subprocess
 ai.js ──POST /api/sources/{id}/analyses──▶ AiManager ──hold──▶ JobManager (source)
        ◀─GET /api/analyses/{id} (poll)───  │  stage: preparing
                                            │  transcript cached? ─yes──────────────┐
                                            │  no: run_worker ─────────────────────▶ app.transcriber
                                            │        (extracting → transcribing)     ffmpeg wrapper → WAV
                                            │                                        faster-whisper (300 s windows)
                                            │  ◀──── transcript.json ────────────────┘
                                            │  stage: analyzing  (chunk 1..n)
                                            │      OpenRouterClient ──HTTPS──▶ openrouter.ai (transcript text only)
                                            │  stage: ranking  (validate, snap, dedupe, rank)
                                            ▼  ready: candidates + usage/cost
 Add to list / Preview / Export ──────────▶ existing /clips and /batch routes (Phase 1)
```

New modules:

| Module | Runs in | Responsibility |
| --- | --- | --- |
| `app/transcript.py` | both | Transcript schema (pydantic), SRT/VTT parsing, Whisper language codes, cache read/write. |
| `app/transcriber.py` | worker | `python -m app.transcriber`: audio extraction, windowed faster-whisper, progress events. |
| `app/whisper_models.py` | CLI | `python -m app.whisper_models [--list] [name…]`: fetch models into the HF cache. |
| `app/openrouter.py` | API | Async client: catalogue, capability check, chat completions with structured output, retries, usage/cost, key check. |
| `app/highlights.py` | API | Chunking with overlap, prompt building, response schema/validation, segment snapping, duration rules, dedupe, ranking, cost estimate. |
| `app/ai_jobs.py` | API | `AiManager`: analysis lifecycle, stages, concurrency, cancellation, cleanup, spend limit. |
| `app/ai_routes.py` | API | HTTP routes. |
| `static/ai.js` | browser | AI Highlights panel. |

## 3. Data schemas

### 3.1 Transcript (`TEMP_DIR/<source_id>/ai/transcript.json`)

```json
{
  "version": 1,
  "origin": "whisper",            // or "subtitles"
  "model": "base",                // whisper model; null for subtitles
  "language": "en",               // dominant language (ISO 639-1 / Whisper code) or "und"
  "language_probability": 0.98,   // null for subtitles
  "requested_language": "auto",
  "duration": 1234.5,             // seconds of audio (or of the source for subtitles)
  "segments": [
    {"start": 0.0, "end": 4.2, "text": "Welcome back to the show.", "language": "en"}
  ]
}
```

Rules: segments sorted by start; `0 ≤ start < end ≤ duration`; text is a single line,
≤ 2000 characters, control characters removed; at most 20,000 segments.

### 3.2 Highlight candidate (API output)

```json
{
  "id": "h1",
  "title": "How AI Will Change Software Development",
  "start": 325.5, "end": 388.2,
  "score": 92,
  "summary": "The speaker explains how AI may change development workflows.",
  "reason": "Clear opening, useful insight and a complete explanation.",
  "tags": ["AI", "Software", "Technology"]
}
```

Scores are the model's editorial heuristic (0–100), labelled as such in the UI; they are not
predictions of views or virality.

### 3.3 Model answer (what OpenRouter must return, per transcript chunk)

```json
{"clips": [{"title": "…", "start_segment": 12, "end_segment": 18, "score": 92,
            "summary": "…", "reason": "…", "tags": ["…"]}]}
```

The model picks **segment numbers**, not seconds. The server maps them to the exact segment
times, so a clip always starts and ends on real speech boundaries and can never point outside
the transcript. Sent as a strict `json_schema` when the model supports structured outputs,
else `json_object`, else plain instructions; validated with pydantic in every case.

## 4. API contracts

All routes are under `/api`, JSON unless noted, behind the existing Guard (host, origin, body
cap, security headers). Errors use the existing `{"error": {"code", "message"}}` shape.

| Method & path | Body / query | Answer |
| --- | --- | --- |
| `GET /ai/status` | — | `{configured, model, whisper_model, max_clips, max_cost_usd, max_source_minutes, data_collection, error}` (no secrets). |
| `POST /ai/test` | `{}` | Checks the key (`GET /key`) and the model's capabilities: `{ok, model: {id, name, context_length, prompt_usd_per_million, completion_usd_per_million, structured_outputs}, key: {usage_usd, limit_usd, limit_remaining_usd, is_free_tier}}`. |
| `POST /sources/{id}/analyses/estimate` | `AnalysisRequest` | `{basis: "transcript"\|"duration", chunks, estimated_usd, max_usd, limit_usd, within_limit, transcript_cached}`. |
| `POST /sources/{id}/analyses` | `AnalysisRequest` | `202 {analysis_id, status}`. 409 `analysis_running` if one is pending for this source. |
| `GET /analyses/{id}` | — | Analysis view (below). |
| `POST /analyses/{id}/cancel` | `{}` | Analysis view. |
| `GET /sources/{id}/transcript` | — | `{"transcript": <3.1>}`, or `{"transcript": null}` while there is none (the page asks whenever a video opens, so this isn't an error). |
| `POST /sources/{id}/transcript` | raw SRT/VTT body, `Content-Type: text/plain` (or `application/x-subrip`, `text/vtt`), `Content-Length` ≤ 4 MiB, `?language=xx` optional, `X-SaveMe-Filename` optional | Imports subtitles as the cached transcript: `{origin, language, segments, duration}` summary. |

`AnalysisRequest` (extra fields refused):

| Field | Type | Default | Rule |
| --- | --- | --- | --- |
| `instruction` | string | `""` | ≤ 500 characters, control characters removed. |
| `min_seconds` | number | 20 | 3–600 |
| `max_seconds` | number | 90 | 5–900, ≥ `min_seconds` + 1 |
| `count` | integer | 5 | 1–`AI_MAX_CLIPS` |
| `min_score` | integer | 0 | 0–100 |
| `language` | string | `"auto"` | `auto` or a Whisper language code |
| `retranscribe` | bool | false | ignore the cached transcript |
| `transcribe_only` | bool | false | stop after transcription (works without an OpenRouter key) |

Analysis view:

```json
{
  "analysis_id": "…22 chars…", "source_id": "…", "status": "analyzing",
  "skipped": ["extracting", "transcribing"],
  "detail": "Analyzing part 2 of 3", "progress_percent": 33.3,
  "transcript": {"origin": "whisper", "language": "en", "segments": 281, "cached": true},
  "candidates": [ … ], "notice": null, "error": null,
  "usage": {"calls": 1, "prompt_tokens": 5120, "completion_tokens": 610,
            "cost_usd": 0.00082, "cost_reported": true, "estimated_usd": 0.0021,
            "max_usd": 0.0049, "limit_usd": 0.25},
  "options": { … the accepted AnalysisRequest … }
}
```

## 5. Job lifecycle

```
queued → preparing → extracting → transcribing → analyzing → ranking → ready
   └────────┴────────────┴─────────────┴─────────────┴───────────┴──→ failed | cancelled
```

- **queued**: waiting for one of `AI_MAX_CONCURRENT_JOBS` AI slots (separate from download slots).
- **preparing**: source must be ready (and stays *held* until the analysis ends), model and
  limits checked, cached transcript looked up, disk room for the WAV reserved.
- **extracting**: FFmpeg → `ai/<analysis_id>/audio.wav` (16 kHz mono PCM). Percent from
  FFmpeg's `out_time_us` against the duration.
- **transcribing**: faster-whisper over 300 s windows. Percent from the end time of the last
  segment against the duration — real progress, never a timer.
- **analyzing**: one OpenRouter call per chunk, sequential. Percent = chunks done / chunks.
- **ranking**: snapping, duration rules, dedupe, ranking (instant, no percent).
- **ready**: candidates and usage kept until the source ends, or 60 min after finishing.

The status is the stage. With a cached transcript the view lists `extracting` and
`transcribing` as `skipped`. `usage` is null until analysis starts (and for transcribe-only runs).

Cancellation at any stage cancels the analysis task: the transcriber's process group is
killed by `run_worker`, an in-flight OpenRouter request is abandoned (the provider may still
bill a request it already started), and the work folder is removed. Closing the studio's
source cancels its analysis. Shutdown cancels every analysis.

One analysis may be pending per source; finished records are capped at 50.

## 6. Resource limits

| Limit | Value | Enforced by |
| --- | --- | --- |
| Concurrent AI jobs | `AI_MAX_CONCURRENT_JOBS` (1, range 1–4) | `asyncio.Semaphore` in `AiManager` |
| Longest video | `AI_MAX_SOURCE_MINUTES` (180, range 1–1440) | `preparing` (probed duration) and the transcriber (audio length) |
| Wall clock per analysis | `AI_MAX_JOB_SECONDS` (3600, range 60–86400) | transcriber timeout + `asyncio.timeout` on the whole job |
| Transcriber memory | RLIMIT_DATA by model: tiny/base 1.5 GiB, small 2.5 GiB, medium 5 GiB, large 8 GiB (Linux) | `app.transcriber` before importing the model |
| Transcriber CPU | `WHISPER_CPU_THREADS` (2, range 1–16), `nice(10)` | `app.transcriber` |
| Disk | WAV ≈ 32 kB/s, reserved through the shared disk budget before extraction; watchdog caps the work folder | `JobManager.make_room`, `run_worker` |
| Spend | `AI_MAX_COST_USD_PER_JOB` (0.25) — upper-bound estimate checked before analysis, running total checked before every call | `AiManager` |
| Clips | `AI_MAX_CLIPS` (10, range 1–20) | request validation and ranking |
| Instruction | 500 characters | request validation |
| Subtitle upload | 4 MiB, 20,000 cues | import route |
| Network | transcriber: deny-all connect guard; OpenRouter only from the API process to `OPENROUTER_BASE_URL` (https) | `app.transcriber`, config |

## 7. Transcription

1. `ffprobe` the source (through the wrapper); no audio stream → `no_audio` ("import
   subtitles instead").
2. `ffmpeg -i media -map 0:a:0 -vn -ac 1 -ar 16000 -c:a pcm_s16le -f wav audio.wav` through
   the wrapper (`-protocol_whitelist file,pipe`).
3. Load `WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
   cpu_threads=WHISPER_CPU_THREADS, local_files_only=True)`; a missing model fails with
   `whisper_model_missing` naming the command that installs it.
4. Read the WAV in 300 s windows. Each boundary moves back to the quietest 100 ms frame in the
   last 20 s of the window, so words are rarely split. Each window: `transcribe(window,
   language=None|code, vad_filter=True, beam_size=5, condition_on_previous_text=False)`;
   segment times are offset by the window start. With `language=auto`, each window detects
   its own language (code-switching videos keep per-segment languages); the transcript's
   `language` is the one covering the most speech.
5. Write `transcript.json`; delete `audio.wav`. The API process validates the file and moves
   it to `ai/transcript.json`, the per-source cache.

Cache rule: a cached transcript is reused unless `retranscribe` is set, or the request names
a specific language different from the one requested for the cached Whisper transcript.
Imported subtitles always win until replaced. The cache lives in the source's folder, so it
is deleted with the source (close, idle TTL, replacement, restart).

SRT/VTT import: UTF-8 (BOM allowed) or Windows-1252; cue numbers, VTT headers, `NOTE`,
`STYLE`, `REGION` blocks, cue settings and inline tags (`<i>`, `<c.x>`, `{\an8}`) removed;
multi-line cues joined; cues past the video's end dropped, overlapping cues kept in time
order. A file with no valid cue is refused (`invalid_subtitles`).

## 8. OpenRouter integration

**Model.** Default `OPENROUTER_MODEL=anthropic/claude-haiku-5.5`, chosen on 2026-11-24 from
the live catalogue (`GET /api/v1/models`, 474 models): $0.10 / $0.50 per million input/output
tokens, 1M context, `structured_outputs` and `response_format` on its providers, reasoning
optional (effort `none` available), strong multilingual quality. Any catalogue model works;
the setting is only a default.

**Capability check** (before every analysis, catalogue cached 1 h): model exists; text in and
out; context length ≥ chunk + answer budget; fixed numeric pricing (dynamic routers `-1`
refused, because the spend limit needs a price); detects `structured_outputs`,
`response_format`, `reasoning` support and whether reasoning is mandatory.

**Request.**

```json
{
  "model": "anthropic/claude-haiku-5.5",
  "messages": [{"role": "system", "content": "…rules…"}, {"role": "user", "content": "…chunk…"}],
  "max_tokens": 2000,
  "response_format": {"type": "json_schema", "json_schema": {"name": "highlights", "strict": true, "schema": {…}}},
  "reasoning": {"effort": "none"},
  "provider": {"require_parameters": true, "data_collection": "deny"}
}
```

Only parameters the model lists are sent (with `require_parameters` an unlisted one would
exclude every provider — Haiku 5.5, for example, lists no `temperature`). `reasoning` is sent
only when supported, at the lowest effort offered. `data_collection: "deny"` keeps transcripts
away from providers that store or train on prompts (`OPENROUTER_DATA_COLLECTION=allow` to opt
out).

**Errors and retries.** Retried with exponential backoff (1 s, 2 s, 4 s + jitter, at most 3
retries, `Retry-After` honoured up to 60 s): network errors, timeouts, 408, 429, 500, 502, 503,
504, and 200 bodies carrying `error` or `finish_reason: "error"`. Not retried: 400, 401
(`openrouter_auth`), 402 (`openrouter_credits`), 403, 404. An answer that isn't valid JSON or
doesn't match the schema is retried once, then that chunk is skipped with a notice; if every
chunk fails the analysis fails with `ai_invalid_response`.

**Usage and cost.** Every completion's `usage` (`prompt_tokens`, `completion_tokens`, `cost`)
is summed. `cost` (OpenRouter credits, USD) is shown as the actual cost when every call
reported it; otherwise the total is computed from token counts × catalogue prices and labelled
an estimate. Before the analysis: an estimate (`chunks × prompt tokens × price + expected
answer`) and an upper bound (`… + max_tokens × price`); the analysis is refused if the upper
bound exceeds `AI_MAX_COST_USD_PER_JOB`. During it, a call is only made if
`spent + its upper bound ≤ limit`, else the analysis stops with the highlights found so far and
a notice. This is a client-side guard on estimates, not a billing cap; set a credit limit on
the OpenRouter key for a hard cap.

## 9. Highlight engine

1. **Chunking.** Segments are grouped into chunks of ≤ 6,000 estimated tokens and ≤ 20 min.
   Consecutive chunks overlap by `max(60 s, max_seconds)` (segment-aligned), so any clip that
   fits the length rule lies wholly inside at least one chunk. Token estimate: UTF-8 bytes / 3
   (conservative for English, adequate for Malayalam and other non-Latin scripts).
2. **Prompt.** A fixed system prompt (role, rules, output format) and a user message with the
   video length, the chunk's range, the length rule, the clip budget, the user's instruction
   inside `<instruction>` and the numbered transcript inside `<transcript>`
   (`[12] 5:25.5–5:31.0 text`). `<`/`>` in transcript text and in the instruction are replaced
   with `‹`/`›`, so neither can close its block. The system prompt says both blocks are data:
   the instruction only says what to look for, and text in the transcript is never followed.
   Without an instruction the default asks for interesting, informative, surprising, funny or
   emotionally engaging moments. Per chunk the model may return up to `min(10, count + 2)`
   clips and is told to return none rather than invent any.
3. **Validation.** Each clip item is validated alone (bad items are dropped and counted):
   segment numbers must be integers inside the chunk with start ≤ end; score is clamped to
   0–100; title ≤ 100, summary/reason ≤ 400 characters, ≤ 5 tags of ≤ 32 characters; control
   characters stripped. Text is untrusted and only ever rendered with `textContent`.
4. **Snapping and length.** Times come from the segments. Too long → trailing segments drop
   until it fits (a single over-long segment is cut at `max_seconds`). Too short → following,
   then preceding, segments are added while it stays ≤ `max_seconds`. Still outside the rule →
   dropped. Then 0.3 s lead-in and 0.5 s tail are added where the rule and the video allow, and
   times are clamped to `[0, duration]` and rounded to milliseconds — the same checks the clip
   routes apply.
5. **Dedupe.** Highest score first; a candidate overlapping an already kept one by more than
   30 % of the shorter clip is dropped.
6. **Ranking.** `score ≥ min_score`, score descending (ties: earlier first), top `count`.
   Fewer qualifying moments → fewer results, with a notice; never padding.

## 10. Security and privacy

- Localhost-only binding, Host/Origin checks, SSRF guard, FFmpeg protocol whitelist and the
  minimal build are unchanged; FFmpeg gains only a PCM encoder and WAV muxer.
- The OpenRouter key is read from the environment only; it is excluded from `repr`, never
  logged, never returned by any route, and is not settable from the browser. Logs record
  status codes and token counts, never prompts or transcript text.
- Only transcript excerpts (and the user's instruction) leave the machine, over HTTPS, to
  `OPENROUTER_BASE_URL`; the page says so next to the Analyze button. Audio, video and file
  paths are never sent. The transcriber process cannot open any network connection.
- Model output, transcripts and subtitle files are untrusted: schema-validated, length-capped,
  stripped of control characters, rendered as text. Model output can only choose segment
  numbers inside the chunk it was shown; times, durations, counts and scores are enforced on
  the server.
- No client-supplied path is used: transcript and work files live at fixed names under the
  source's validated job folder.

## 11. UI

Clip Studio gains a two-tab switch under the preview: **Manual** (Phase 1 controls) and
**AI Highlights**. The clip list and export area stay below both tabs.

AI Highlights panel: OpenRouter status (model, "Test connection"), the privacy note,
instruction box with examples, min/max length, number of clips, minimum score, language,
"Transcribe again", estimated cost, **Find highlights** / **Transcribe only**; a stage list
with a real progress bar and Cancel; results with usage/cost, select all/none and **Add
selected to clip list**; one card per highlight (rank, score, title, editable start/end,
summary, reason, tags, Preview, Load into editor, Export); a collapsible transcript with
search and click-to-seek, and subtitle import. Same Tailwind tokens, light/dark, 375 px
layout, keyboard and screen-reader behaviour (labels, live region, focus kept when controls
disable themselves).

## 12. Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | blank | blank = AI analysis off (transcription still works) |
| `OPENROUTER_MODEL` | `anthropic/claude-haiku-5.5` | any catalogue model id |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | must be `https://` |
| `OPENROUTER_DATA_COLLECTION` | `deny` | `deny` or `allow` |
| `AI_MAX_CLIPS` | 10 | 1–20 |
| `AI_MAX_CONCURRENT_JOBS` | 1 | 1–4 |
| `AI_MAX_COST_USD_PER_JOB` | 0.25 | 0.001–100 |
| `AI_MAX_SOURCE_MINUTES` | 180 | 1–1440 |
| `AI_MAX_JOB_SECONDS` | 3600 | 60–86400 |
| `WHISPER_MODEL` | `base` | a faster-whisper model name |
| `WHISPER_CPU_THREADS` | 2 | 1–16 |

An invalid AI value turns AI Highlights off with a message naming the variable; downloads and
manual clipping keep working.

## 13. Testing

Standard suite (no network, no paid calls): transcript parsing and cache; audio extraction
with the real FFmpeg; the transcriber worker with a fake Whisper backend (windows, offsets,
languages, progress, kill on cancel); OpenRouter client against `httpx.MockTransport`
(request shape, auth, retries, rate limits, invalid answers, usage/cost, key check, capability
check); highlight engine (chunking/overlap, prompt, validation, snapping, dedupe, ranking,
duration rules, estimates); `AiManager` lifecycle, caching, cancellation, cleanup, holds,
spend limit, concurrency, downloads unaffected; API routes; page markup. Opt-in: a real
Whisper test when the `base` model is cached, and `tests/network/test_openrouter_live.py`
(marker `network`, needs `OPENROUTER_API_KEY`).

## 14. Non-goals and limitations

- No 9:16 reframing, burned-in subtitles or re-encoding (Phase 3; needs an H.264 encoder).
- No speaker diarisation; no word-level timestamps; clip edges follow Whisper segments.
- Spending limits are client-side estimates; a cancelled request may still be billed.
- Transcription runs on CPU (int8); Apple GPU (MPS) is not used inside Docker.
- Analyses and transcripts live in memory and in the source folder: a restart loses them,
  as it loses the source.
