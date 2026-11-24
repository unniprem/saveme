# Clip Studio, Phase 1 completion: clip list and ZIP export (design)

**Date:** 2026-11-24
**Builds on:** [`2026-11-24-clip-studio-phase-1-design.md`](2026-11-24-clip-studio-phase-1-design.md). This document records only what changes.
**Size:** a small change on top of a project.

## 1. Intent

The owner's Phase 1 brief lists ten features. The first Clip Studio pass delivered eight of them. The other two were left out as non-goals (§3 of the Phase 1 design):

- **#4 Multiple clips from one source.** Before this change you could export one range at a time: mark it, export it, then mark the next.
- **#6 ZIP batch export.** Before this change there was no way to export several clips at once.

The brief asks for both, so it overrides those two non-goals. The rest of the Phase 1 design stands: stream-copy cuts, sources as jobs, uploads and their limits, and cleanup.

## 2. Goals

- **A clip list.** While a source is open, the owner can add the current start and end to a list, as often as they like, up to a cap. Each row shows the range and its length, and has **Load** (puts the range back in the editor to preview, adjust or export alone) and **Remove**.
- **Export the list as one ZIP.** This runs as one server job with one progress bar and one Cancel. The ZIP is saved to the device automatically, like any clip, and then deleted from the server.
- **Single clips are unchanged.** **Export clip** still exports the current selection as its own file (MP4 whenever the source is MP4, which covers every link and nearly every upload).

## 3. Non-goals

- Joining ranges into one video, re-encoding, and per-clip names typed by the owner.
- Keeping the list on the server. It lives in the page and in `sessionStorage`, like the marks.

## 4. Anti-goals

- **A new dependency or FFmpeg change.** The ZIP is written by Python's standard `zipfile` in the worker. FFmpeg only cuts, exactly as for a single clip.
- **A ZIP from the downloader.** `zip` is not added to the delivery media types that downloads use, so a link to a `.zip` file can never be delivered. Only a `batch` job produces a ZIP.
- **Zip slip or names from the client.** Entry names are built by the server from the validated times and the source title (through `sanitize_filename`), and the worker refuses any name with a path separator, a control character or a leading dot.
- **Weakening a limit.** A batch takes one queue place and one slot. It reserves `2 × MAX_FILE_SIZE_MB` like a download, and the whole ZIP must stay under `MAX_FILE_SIZE_MB`.

## 5. Approaches

| | Approach | Trade-off |
|---|---|---|
| **A** | **One `batch` job: the worker cuts each range and adds it to `media.zip`** | One queue place, one progress bar, one Cancel. Delivery, TTL and cleanup are the existing ones. The ZIP and the part being added sit on disk together, so it reserves 2× (like downloads). |
| B | N clip jobs, then a ZIP streamed at delivery from their files | Needs only 1× disk, but takes N queue places (the default queue holds 6), needs a hand-written streaming ZIP, and claiming N one-shot files at once is a new delivery lifecycle. |
| C | Zip in the browser | Holds every clip in browser memory, needs a ZIP writer written in JS (no libraries), and still runs N server jobs. |

**Chosen: A** (designer's call). It reuses the job, delivery and cleanup machinery as it is.

## 6. Design

### 6.1 API

| Route | Body | Answer |
|---|---|---|
| `POST /api/sources/{id}/batch` | `{"clips": [{"start", "end"}, …]}`, 1 to `MAX_BATCH_CLIPS` (20) items | 202 `{"job_id", "status"}`; then the existing `/api/jobs/{id}`, `/cancel` and `/file` routes |
| `GET /api/studio` | | gains `max_batch_clips` |

- Each range follows the single-clip rules (§6.3 of the Phase 1 design), rounded to milliseconds. An error names the clip: `invalid_clip` "Clip 3: The end must be after the start."
- A list that is empty, too long or not a list is a 400 `invalid_request`. Field errors inside a clip read "Clip 2: start must be a number of seconds, 0 or more".
- Job view: `kind` is `"batch"`. While it runs, `detail` reads "Cutting clip 2 of 5", `progress_percent` covers the whole batch, and `downloaded_bytes` is the bytes written so far.

### 6.2 Job (`kind="batch"`)

```text
batch: queued → cutting → ready → expired (delivered | ttl | evicted)
       queued/cutting → failed | cancelled   (no resume: the folder is deleted)
```

- It behaves like a clip: the source counts as in use while it is pending (so it never expires idle), closing the source cancels it, it can't be resumed, and once ready it is a one-shot file under the TTL and the ready-file cap.
- Filename: `"<title> (5 clips).zip"`, media type `application/zip`.
- Entry names: `"01 <title> (clip 0m05s-0m12.5s).mp4"`, numbered in list order, so names are unique even for repeated ranges.

### 6.3 Worker mode `batch`

`{"mode": "batch", "source", "workdir", "clips": [{"start", "end", "name"}], "keep_tags", "max_bytes"}`

1. Validate as `clip` does, plus 1–20 clips, each `0 ≤ start < end`, and each name a plain file name (no `/`, `\`, NUL or control characters, no leading `.`, at most 255 bytes, ending in the source's extension).
2. For each clip in order:
   - Cut it to `part.<ext>` with `mediafile.cut()`, with `-fs` set to the room left under `max_bytes`.
   - Add it to `media.zip` (`ZIP_STORED`, because video is already compressed; ZIP64 is allowed), then delete the part.
   - Progress percent is the share of the total length cut so far. The detail is "Cutting clip i of n", and the bytes are the ZIP's size plus the part's.
3. A cut that fails is `clip_failed` "Couldn't cut clip i. Try a different start or end." Passing the size limit is `too_large` "These clips add up to more than the … limit. Export fewer or shorter clips."
4. The result is `{"file": "media.zip", "ext": "zip", "count": n}`. On any failure the workdir is left empty.

### 6.4 Page

- The editor's button row becomes **Play clip**, **Add to list** and **Export clip**.
- **Add to list** refuses a range already in the list, and refuses a new one once the list is full. Either way it says why in the clip message line.
- **Clip list** appears under the editor once it has a row. Rows are kept sorted by start, so the ZIP follows the video. Each row shows "1  0:05.0 – 0:12.5 · 7.5 s" with **Load** and **Remove** (40 px targets). The last control is **Export N clips as ZIP**.
- The export area is shared. A ZIP shows the server's "Cutting clip 2 of 5", the percent, "MB written" and Cancel, then "Saving to your device…" and then "ZIP saved". The fallback link reads "Save ZIP". Only one export (clip or ZIP) runs at a time.
- `sessionStorage` gains the list and the running export's kind, so a reload keeps both. **Close** clears them.

## 7. Also fixed in this pass (deferred minors from the Phase 1 review)

- Exactly-0.1 s ranges: the page compares lengths in whole milliseconds, like the server.
- The keep-alive interval is derived from `idle_minutes` (at most 60 s, at least 15 s). `JOB_TTL_MINUTES=1` then can't expire a visible studio.
- With no preview, **Set to playhead**, **Play clip** and `I`/`O` are disabled, since the playhead is stuck at 0.
- A clip that is too large is told "Choose a shorter part", not "Try a lower quality or audio only".

## 8. Left to the builder

Tailwind classes, the exact wording beyond the copy above, and test names.
