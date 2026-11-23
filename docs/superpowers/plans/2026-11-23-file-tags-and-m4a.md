# File Tags, Cover Art and M4A Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Saved files carry title, artist, date, description, chapters and cover art (never the source link). A new "M4A audio (original)" choice stream-copies AAC. "Best" says what it really is.

**Architecture:**
- Tags come from yt-dlp's `FFmpegMetadata` postprocessor, with the source-link tags blanked via `meta_purl`/`meta_comment`.
- Cover art is fetched by a new `app/cover.py` through the worker's guarded `ydl.urlopen`, size-capped and checked by magic bytes, and embedded with mutagen after the final file exists. FFmpeg never sees an image.
- M4A is a third `output` value. It selects AAC-only audio, and `FFmpegExtractAudio` stream-copies it into the `ipod` muxer.

**Tech Stack:** Python 3.12, FastAPI, yt-dlp 2026.10.26, mutagen 1.48.1 (already pinned via `yt-dlp[default]`), minimal FFmpeg build, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-23-file-tags-and-m4a-design.md`

## Global Constraints

- **Dependencies:** no new ones. mutagen is already in `requirements.txt` (`mutagen==1.48.1 # via yt-dlp`).
- **FFmpeg:** the build gains only the `ffmetadata` demuxer. No image demuxers, decoders or encoders (`mjpeg`, `png`, `webp`, `image2`, `*_pipe`).
- **Cover art:**
  - JPEG or PNG only, judged by magic bytes: JPEG `FF D8 FF`, PNG `89 50 4E 47`.
  - At most `COVER_MAX_BYTES = 5 * 1024 * 1024` per image, and at most `MAX_COVER_TRIES = 3` URLs.
  - Fetched only through the worker's `ydl.urlopen`.
  - Any problem means no cover. The job never fails because of a cover.
- **Tags:** never the source link. Every download's info gets `meta_purl = ""` and `meta_comment = ""` before processing.
- **API:**
  - `output: "audio"` keeps meaning MP3.
  - The new value is `output: "m4a"`.
  - Inspect's `audio` object gains `"m4a": bool`.
- **Copy, exact:**
  - chip `"M4A audio (original)"`
  - phases `"Adding tags"` and `"Preparing M4A"`
  - job kind label `"M4A"`
  - "Best" label format `Best (<side>p[<fps>][ · <codecs>])`, e.g. `Best (2160p60 · AV1/VP9)`
- **Page:**
  - CSP rules from `tests/unit/test_page.py`: no `innerHTML` and similar, no inline handlers or styles, text set via `textContent`.
  - Any new Tailwind class must appear literally in `static/index.html` or `static/app.js`, then run `scripts/build-css.sh`.
- **Git:**
  - Branch `feature/file-tags-m4a`, one commit per task, conventional messages.
  - Every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Test runs:** `.venv/bin/pytest -q` must pass at the end of every task. Integration tests need the real `ffmpeg`/`ffprobe` on `PATH`, not the `app/bin` wrappers.

## Review Focus

Five cases most likely to hit a real user that the spec doesn't spell out. Each has a test in the task that owns the code.

1. **YouTube thumbnail URLs.** They carry query strings (`…/hqdefault.jpg?sqp=…`) and come with WebP twins (`…/vi_webp/…/maxresdefault.webp`). The suffix check must use the URL *path*, so the JPEG is kept and the WebP is dropped. → Task 2, `test_cover_urls_judges_the_path_not_the_query`.
2. **Bad thumbnail hosts.** A host that 404s, times out or is blocked must not stall the job for long or fail it. At most 3 URLs are tried, then the file goes out without a cover. → Task 2, `test_fetch_cover_stops_after_max_tries` and `test_blocked_thumbnail_still_delivers`.
3. **Hostile titles.** A title with quotes, `;`, `$()` and `../` must land in the title tag verbatim, and the file must stay in the workdir. → Task 1, extend `test_hostile_title_stays_in_workdir`.
4. **Source files that already carry a link.** A source file can come with its own `comment` tag holding a URL. It must not survive into the saved file. → Task 1, `test_source_comment_link_is_removed`.
5. **Tabs open across the upgrade.** A tab that stored an MP3 job before the upgrade must still show "MP3" after a reload. → Task 5, manual check step.

---

### Task 1: Tags and chapters on every download

**Files:**
- Modify: `app/ytdl.py` (add `TAGS_PP`, `NO_SOURCE_LINK`, `output_opts`, `without_source_link`)
- Modify: `app/worker.py` (`_Phases.__call__`, `_download`: use `output_opts` and `without_source_link`)
- Modify: `Dockerfile` (add `ffmetadata` to `--enable-demuxer=`)
- Modify: `scripts/docker-ffmpeg-check.sh` (chapters case)
- Modify: `tests/support/media.py` (add `tagged.mp4`)
- Test: `tests/unit/test_ytdl.py`, `tests/unit/test_packaging.py`, `tests/integration/test_worker_download.py`

**Interfaces:**
- Produces, in `app/ytdl.py`:
  - `TAGS_PP: dict = {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True, "add_infojson": False}`
  - `NO_SOURCE_LINK: dict = {"meta_purl": "", "meta_comment": ""}`
  - `output_opts(output: str, quality: Quality) -> dict`. It returns `video_opts(quality)` or `audio_opts()`, with `TAGS_PP` appended **last** to `postprocessors`, so tags are written after any audio extraction. Task 3 adds the `"m4a"` branch.
  - `without_source_link(info: dict) -> dict`, which returns `{**info, **NO_SOURCE_LINK}`.
- Produces, in `app/worker.py`: `_Phases` maps the postprocessor key `"Metadata"` to the phase `"Adding tags"`.

- [ ] **Step 1: Write the failing unit tests** in `tests/unit/test_ytdl.py`

```python
def test_output_opts_add_tags_last():
    for opts in (output_opts("video", "best"), output_opts("audio", "best")):
        assert opts["postprocessors"][-1] == TAGS_PP
    assert output_opts("audio", "best")["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert output_opts("video", "720")["format_sort"] == video_opts("720")["format_sort"]
    assert TAGS_PP["add_chapters"] is True and TAGS_PP["add_infojson"] is False

def test_without_source_link_blanks_purl_and_comment():
    out = without_source_link({"title": "t", "webpage_url": "https://x/y"})
    assert out["meta_purl"] == "" and out["meta_comment"] == "" and out["webpage_url"] == "https://x/y"
```

In `tests/unit/test_packaging.py::test_ffmpeg_build_reads_local_files_only`, add:

```python
    assert "ffmetadata" in demuxers
    decoders = re.search(r"--enable-decoder=(\S+)", flags).group(1).split(",")
    images = {"mjpeg", "png", "webp", "image2", "jpeg_pipe", "png_pipe", "webp_pipe", "bmp", "gif"}
    assert not images & (set(demuxers) | set(decoders))
```

- [ ] **Step 2: Write the failing integration tests** in `tests/integration/test_worker_download.py`

- Add a `tags(path) -> dict[str, str]` helper next to `probe`: the real ffprobe with `-show_entries format_tags -of json`, keys lower-cased, `{}` when there are none.
- In `tests/support/media.py`, generate `tagged.mp4`: the same as `sample.mp4` plus `-metadata comment=http://leak.example/x`. Add it to `names`.

```python
def test_mp3_and_mp4_carry_title_and_no_source_link(media_server, job, tmp_path):
    page = "/page?title=Tagged%20Song&video=/media/sample.mp4"
    events, rc = download(media_server, page, job, output="audio")
    result_of(events, rc)
    t = tags(job / "media.mp3")
    assert t["title"] == "Tagged Song"
    assert not any(media_server.url("") in v for v in t.values())
    assert "Adding tags" in phases_of(events)
    assert listing(job) == ["media.mp3"]          # no .meta / .temp files left
    other = tmp_path / "v"; other.mkdir()
    result_of(*download(media_server, page, other))
    t = tags(other / "media.mp4")
    assert t["title"] == "Tagged Song" and not any(media_server.url("") in v for v in t.values())

def test_source_comment_link_is_removed(media_server, job):
    result_of(*download(media_server, "/media/tagged.mp4", job))
    assert not any("leak.example" in v for v in tags(job / "media.mp4").values())
```

Extend `test_hostile_title_stays_in_workdir` with `assert tags(job / "media.mp4")["title"] == HOSTILE_TITLE`.

Add an in-process test, next to `test_failed_mp4_remux` and using its way of reaching the real ffmpeg: build an MKV with two chapters and a title tag using the real ffmpeg and an `;FFMETADATA1` file, run `_to_mp4` on it, then assert `ffprobe -show_chapters` lists 2 chapters and the title tag survives.

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_ytdl.py tests/unit/test_packaging.py tests/integration/test_worker_download.py`
Expected: FAIL. `output_opts`/`TAGS_PP` can't be imported, `ffmetadata` isn't in the demuxers, and there's no title tag.

- [ ] **Step 4: Implement**

- **`app/ytdl.py`:** add the four names from Interfaces.
- **`app/worker.py`, `_download`:**
  - Build params with `output_opts(output, quality)` instead of the inline `video_opts(...) if ... else audio_opts()`.
  - Right after `resolve()`, set `info = without_source_link(info)`.
- **`app/worker.py`, `_Phases.__call__`:** `"Merger"` → "Merging video and audio", `"Metadata"` → "Adding tags", otherwise the existing MP3/MP4 text.
- **`Dockerfile`:** append `ffmetadata` to `--enable-demuxer=`.
- **`scripts/docker-ffmpeg-check.sh`:** after the matrix, add a "chapters via ffmetadata" case.
  - Write an `;FFMETADATA1` file with two `[CHAPTER]` blocks.
  - Run `/app/app/bin/ffmpeg -i s.mp4 -i ch.meta -map 0 -dn -ignore_unknown -c copy -map_metadata 1 -metadata title=Tagged ch.mp4`.
  - PASS when `ffprobe -show_chapters -of csv=p=0 ch.mp4 | wc -l` is 2. Otherwise FAIL and set `failed=1`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 6: Run the FFmpeg check**

Run: `scripts/docker-ffmpeg-check.sh`
Expected: every line `PASS`, including the new chapters case. No `FFMPEG CHECK FAIL`.

- [ ] **Step 7: Commit**

```bash
git add app/ytdl.py app/worker.py Dockerfile scripts/docker-ffmpeg-check.sh tests/support/media.py tests/unit/test_ytdl.py tests/unit/test_packaging.py tests/integration/test_worker_download.py
git commit -m "feat: tag downloads with title, artist, date and chapters, never the source link"
```

---

### Task 2: Cover art without FFmpeg

**Files:**
- Create: `app/cover.py`
- Modify: `app/worker.py` (`_download`: fetch inside the `with` block, embed after the final file is settled)
- Modify: `tests/support/media.py` (add `cover.jpg`, `cover.png` and `junk.jpg`)
- Modify: `tests/support/server.py` (`CONTENT_TYPES`: `.jpg` → `image/jpeg`, `.png` → `image/png`)
- Test: `tests/unit/test_cover.py`, `tests/integration/test_cover_embed.py`, `tests/integration/test_worker_download.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the worker's existing `_download` structure.
- Produces, in `app/cover.py`:
  - `COVER_MAX_BYTES = 5 * 1024 * 1024`, `MAX_COVER_TRIES = 3`
  - `cover_urls(info: dict) -> list[str]`
    - Candidates come from `info["thumbnails"]`, which yt-dlp has already sorted worst→best, so best is taken first. If there are none, `info["thumbnail"]` is used.
    - Each URL goes through `yt_dlp.utils.sanitize_url(url, scheme="https")`.
    - Only `http`/`https` URLs whose **path** (`urlsplit(url).path.lower()`) ends in `.jpg`, `.jpeg` or `.png` are kept.
    - Duplicates are removed, and at most `MAX_COVER_TRIES` URLs are returned.
  - `image_kind(data: bytes) -> Literal["jpeg", "png"] | None`, judged by magic bytes.
  - `fetch_cover(urlopen: Callable[[str], Any], info: dict) -> tuple[bytes, str] | None`
    - Tries each `cover_urls(info)` URL in order.
    - Reads the response in blocks until EOF, abandoning a URL once it exceeds `COVER_MAX_BYTES`.
    - Closes every response, and returns the first `(data, kind)` that `image_kind` accepts.
    - Catches every `Exception` per URL and logs one line to stderr. It never raises.
  - `embed_cover(path: Path, data: bytes, kind: str) -> bool`
    - `.mp3`: mutagen ID3 with all `APIC` frames replaced by one front cover (`type=3`, mime `image/jpeg` or `image/png`). If the file has no ID3 header, create one.
    - `.mp4`/`.m4a`: mutagen `MP4` with `covr = [MP4Cover(data, imageformat=FORMAT_JPEG or FORMAT_PNG)]`.
    - Any other extension returns `False` and leaves the file untouched. Any `Exception` is logged and returns `False`.

- [ ] **Step 1: Write the failing unit tests** in `tests/unit/test_cover.py`. They use no network; a fake `urlopen` maps URL → bytes or an exception and records each call.

```python
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100

def test_cover_urls_judges_the_path_not_the_query():
    info = {"thumbnails": [
        {"url": "https://i.ytimg.com/vi/a/default.jpg"},
        {"url": "https://i.ytimg.com/vi_webp/a/maxresdefault.webp"},
        {"url": "https://x.test/y.webp?fake=.jpg"},
        {"url": "https://i.ytimg.com/vi/a/hqdefault.jpg?sqp=abc"},
    ]}
    assert cover_urls(info) == ["https://i.ytimg.com/vi/a/hqdefault.jpg?sqp=abc",
                                "https://i.ytimg.com/vi/a/default.jpg"]

def test_cover_urls_schemes_and_fallback():
    assert cover_urls({"thumbnail": "//cdn.test/c.PNG"}) == ["https://cdn.test/c.PNG"]
    assert cover_urls({"thumbnails": [{"url": "file:///etc/a.jpg"}, {"url": "data:image/jpeg,x.jpg"}]}) == []
    assert len(cover_urls({"thumbnails": [{"url": f"https://c.test/{i}.jpg"} for i in range(9)]})) == MAX_COVER_TRIES

def test_image_kind():
    assert image_kind(JPEG) == "jpeg" and image_kind(PNG) == "png"
    assert image_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") is None and image_kind(b"") is None

def test_fetch_cover_takes_first_good_image():  # a.jpg raises OSError, b.png returns PNG
    assert fetch_cover(opener, info_ab) == (PNG, "png") and calls == [A, B]

def test_fetch_cover_skips_oversize_and_junk():  # JPEG header + COVER_MAX_BYTES of padding; then junk
    assert fetch_cover(opener, info) is None

def test_fetch_cover_stops_after_max_tries():   # 5 URLs, every one raises
    assert fetch_cover(opener, info) is None and len(calls) == MAX_COVER_TRIES

def test_fetch_cover_never_raises():            # urlopen raises RuntimeError
    assert fetch_cover(opener, info) is None
```

- [ ] **Step 2: Write the failing embed tests** in `tests/integration/test_cover_embed.py`, using the session `media` fixture. Copy each file to `tmp_path` first.
  - `embed_cover(copy_of_sample_mp3, jpeg_bytes, "jpeg")` is `True`. Then `ID3(path).getall("APIC")` has exactly one frame, with mime `image/jpeg`, `type == 3` and the same bytes. Running it twice still leaves one frame.
  - `embed_cover(copy_of_sample_mp4, png_bytes, "png")` is `True`. Then `MP4(path).tags["covr"][0]` equals the PNG bytes, and its `imageformat` is `MP4Cover.FORMAT_PNG`. The same holds for a copy renamed `.m4a`.
  - `embed_cover(tmp_path / "a.mkv", ...)` is `False`, and the file's bytes are unchanged.
  - `embed_cover(tmp_path / "missing.mp3", ...)` is `False` and doesn't raise.

- [ ] **Step 3: Write the failing worker tests** in `tests/integration/test_worker_download.py`. Pages are `/page?title=T&video=/media/sample.mp4&thumb=<quoted absolute URL>`.
  - `test_mp3_gets_jpeg_cover`, with `thumb=media_server.url("/media/cover.jpg")` and output `audio`: the APIC bytes equal `media["cover.jpg"].read_bytes()`, and `listing(job) == ["media.mp3"]`.
  - `test_mp4_gets_png_cover`, with `thumb=…/media/cover.png`: the MP4 `covr` holds the PNG bytes.
  - `test_webp_thumbnail_is_never_fetched`, with `thumb=…/media/cover.webp`: the result is OK with no `covr`, and `media_server.hits["/media/cover.webp"] == 0`.
  - `test_junk_and_missing_thumbnails_still_deliver`, with `junk.jpg` and then `/missing.jpg`: the result is OK, there's no cover, and only the media file is in the job folder.
  - `test_blocked_thumbnail_still_delivers`, with `thumb=http://169.254.169.254/x.jpg`: the result is OK, there's no cover, and no `error` event.

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_cover.py tests/integration/test_cover_embed.py tests/integration/test_worker_download.py -k "cover or thumbnail"`
Expected: FAIL. `app.cover` doesn't exist, and the files have no cover.

- [ ] **Step 5: Implement**
  - Write `app/cover.py` as in Interfaces. Import mutagen lazily inside `embed_cover`, as the worker imports yt-dlp.
  - In `_download`, right after `processed = ydl.process_ie_result(...)` and inside the `with` block, set `cover = fetch_cover(ydl.urlopen, processed)`.
  - After the video `_to_mp4` branch and before building the return dict: `if cover: embed_cover(final, *cover)`.
  - Add `cover.jpg` and `cover.png` (64×64 `color=c=red`, one frame, made with the real ffmpeg) and `junk.jpg` (`b"not an image"`) to `make_media` and its `names`.

- [ ] **Step 6: Run all tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/cover.py app/worker.py tests/support/media.py tests/support/server.py tests/unit/test_cover.py tests/integration/test_cover_embed.py tests/integration/test_worker_download.py
git commit -m "feat: embed the source thumbnail as cover art with mutagen, best effort"
```

---

### Task 3: M4A (original) on the server

**Files:**
- Modify: `app/schemas.py` (`Output`)
- Modify: `app/ytdl.py` (`m4a_opts`, the `output_opts` m4a branch, `track_available`, `inspect_payload`'s `audio.m4a`)
- Modify: `app/worker.py` (`_MISSING_TRACK`, availability check, the `_lacks_audio` branch, the `_Phases` m4a text)
- Modify: `app/filenames.py` (`"m4a": "audio/mp4"`)
- Modify: `tests/support/media.py` (`hls/aac/`, `hls/opus/`)
- Test: `tests/unit/test_ytdl.py`, `tests/unit/test_filenames.py`, `tests/integration/test_worker_download.py`, `tests/integration/test_api.py`

**Interfaces:**
- Consumes: `output_opts`, `TAGS_PP` (Task 1).
- Produces:
  - `Output = Literal["video", "audio", "m4a"]`
  - `m4a_opts() -> dict`: `{"format": "ba[acodec^=mp4a]/b[acodec^=mp4a]", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]}`
  - `output_opts("m4a", q)` is `m4a_opts()` with `TAGS_PP` appended.
  - `track_available(payload: dict, output: str) -> bool`: `video` → `payload["video"]["available"]`, `audio` → `payload["audio"]["available"]`, `m4a` → `payload["audio"]["m4a"]`.
  - `inspect_payload(...)["audio"] == {"available": bool, "m4a": bool}`. `m4a` is true if any kept format's `acodec` is a string starting with `mp4a` (case-insensitive). `None` or unknown codecs count as false.
  - `_MISSING_TRACK["m4a"] == "no_audio"`
  - Phase text for `ExtractAudio` when the output is `m4a`: `"Preparing M4A"`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_m4a_flag_needs_known_aac():
    f = lambda **k: {"url": "https://x/f", "protocol": "https", **k}
    assert inspect_payload({"formats": [f(vcodec="none", acodec="mp4a.40.2")]})["audio"]["m4a"] is True
    assert inspect_payload({"formats": [f(vcodec="none", acodec="opus")]})["audio"]["m4a"] is False
    assert inspect_payload({"formats": [f(vcodec=None, acodec=None)]})["audio"]["m4a"] is False

def test_m4a_opts_copy_aac():
    o = output_opts("m4a", "best")
    assert o["format"] == "ba[acodec^=mp4a]/b[acodec^=mp4a]"
    assert o["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}, TAGS_PP]

def test_track_available():
    p = {"video": {"available": False}, "audio": {"available": True, "m4a": False}}
    assert [track_available(p, o) for o in ("video", "audio", "m4a")] == [False, True, False]
```

In `tests/unit/test_filenames.py`: `sanitize_filename("Song", "m4a") == "Song.m4a"` and `media_type_for("m4a") == "audio/mp4"`.

In `tests/integration/test_api.py`, a `POST /api/jobs` with `output: "m4a"` is accepted, and `output: "flac"` is still a 422 whose message lists the three choices. Update any existing assertion that lists only `video, audio`.

- [ ] **Step 2: Add fixtures and write the failing worker tests**
  - `make_media` writes `hls/aac/`: the real ffmpeg, `sine` for 4 s, `-c:a aac -b:a 64k -f hls -hls_time 2 -hls_playlist_type vod -hls_segment_filename a_%03d.ts a.m3u8`. The master is written **by hand**, so the codec string is deterministic: `"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=70000,CODECS=\"mp4a.40.2\"\na.m3u8\n"` as `hls/aac/master.m3u8`.
  - `hls/opus/master.m3u8` is the same text with `CODECS="opus"`, pointing at `../aac/a.m3u8`.

```python
def test_m4a_is_a_stream_copy_of_aac(media_server, job):
    events, rc = download(media_server, "/hls/aac/master.m3u8", job, output="m4a")
    data = result_of(events, rc)
    assert data["file"] == "media.m4a" and data["ext"] == "m4a"
    s = probe_streams_with_bitrate(job / "media.m4a")   # ffprobe stream=codec_name,bit_rate
    assert [x["codec_name"] for x in s] == ["aac"] and int(s[0]["bit_rate"]) < 80_000  # 64k source; a re-encode would be ~128k
    assert "Preparing M4A" in phases_of(events) and "Converting to MP3" not in phases_of(events)
    assert listing(job) == ["media.m4a"]

def test_m4a_without_aac_fails_before_download(media_server, job):
    before = media_server.hits["/hls/aac/a_000.ts"]
    assert error_of(*download(media_server, "/hls/opus/master.m3u8", job, output="m4a"))["code"] == "no_audio"
    assert media_server.hits["/hls/aac/a_000.ts"] == before and listing(job) == []

def test_m4a_from_unknown_codec_direct_link_is_refused(media_server, job):
    assert error_of(*download(media_server, "/media/sample.mp4", job, output="m4a"))["code"] == "no_audio"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_ytdl.py tests/unit/test_filenames.py tests/integration/test_api.py tests/integration/test_worker_download.py -k "m4a or track_available or choices"`
Expected: FAIL.

- [ ] **Step 4: Implement**
  - Make the Interfaces changes.
  - In `_download`, replace `inspect_payload(info)[output]["available"]` with `track_available(inspect_payload(info), output)`.
  - Change `if output == "audio" and _lacks_audio(workdir)` to `if output in ("audio", "m4a") and ...`.

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/pytest -q`
Expected: all pass, including the unchanged `test_audio_mp3`, which proves `"audio"` still means MP3.

- [ ] **Step 6: Commit**

```bash
git add app/schemas.py app/ytdl.py app/worker.py app/filenames.py tests/support/media.py tests/unit/test_ytdl.py tests/unit/test_filenames.py tests/integration/test_api.py tests/integration/test_worker_download.py
git commit -m "feat: offer M4A audio that stream-copies the source's AAC"
```

---

### Task 4: An honest "Best" label

**Files:**
- Modify: `app/ytdl.py` (`codec_family`, `best_label`; `inspect_payload` uses `best_label`)
- Test: `tests/unit/test_ytdl.py`

**Interfaces:**
- Produces:
  - `codec_family(vcodec: str | None) -> str | None`. Matching is on the lower-cased prefix:
    - `av01` → `"AV1"`
    - `vp09`, `vp9` → `"VP9"`
    - `hev1`, `hvc1`, `h265`, `hevc` → `"H.265"`
    - `avc1`, `avc3`, `h264` → `"H.264"`
    - anything else, including `None`, → `None`
  - `best_label(video_fmts: list[dict]) -> str`. The rule:
    1. `top` is the largest `short_side`. If no format has a size, return `"Best"`.
    2. Of the formats at `top`, `top_fps` is the largest numeric `fps` above 0, or `None` if there is none. When `top_fps` is set, keep only the formats with that fps.
    3. The fps text is `str(round(top_fps))` when `top_fps > 30`, otherwise empty.
    4. The codec text is the families of the kept formats in the order AV1, VP9, H.265, H.264, joined by `"/"`. It's empty if any kept format's family is `None`.
    5. Return `f"Best ({top}p{fps_text}"`, plus `f" · {codecs}"` when the codec text isn't empty, plus `")"`.

- [ ] **Step 1: Write the failing tests**

```python
V = lambda side, codec, fps=None: {"url": "https://x/v", "protocol": "https", "width": side * 16 // 9,
                                   "height": side, "vcodec": codec, "acodec": "none", "fps": fps}

def test_best_label_cases():
    assert best_label([V(2160, "av01.0.12M.08", 60), V(2160, "vp09.00.51.08", 60),
                       V(2160, "vp09.00.51.08", 30), V(1080, "avc1.640028", 60)]) == "Best (2160p60 · AV1/VP9)"
    assert best_label([V(1080, "avc1.64001F", 30)]) == "Best (1080p · H.264)"
    assert best_label([V(1080, "avc1.64001F", 50)]) == "Best (1080p50 · H.264)"
    assert best_label([V(1080, "avc1.64001F", 59.94)]) == "Best (1080p60 · H.264)"
    assert best_label([V(1080, None)]) == "Best (1080p)"
    assert best_label([V(1080, "avc1"), V(1080, "vp8")]) == "Best (1080p)"
    assert best_label([{"url": "https://x/v", "vcodec": None}]) == "Best"
```

Update the existing label assertions in `test_tiers_listed_only_when_present` and `test_vertical_uses_short_side` to the new format, as their fixtures' codecs and fps dictate. `test_direct_link_unknown_codecs` stays `"Best"`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_ytdl.py -k best_label`
Expected: FAIL, because `best_label` can't be imported.

- [ ] **Step 3: Implement** `codec_family`, `best_label`, and have `inspect_payload` build the `"best"` quality's label with `best_label(video_fmts)`.

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/ytdl.py tests/unit/test_ytdl.py
git commit -m "feat: label Best with its real size, frame rate and codecs"
```

---

### Task 5: The page offers M4A

**Files:**
- Modify: `static/app.js` (`M4A` constant, `normalize`, `renderFormats`, `chosenFormat`, `startJob`, `loadJob`, `paintJob`)
- Modify: `static/index.html` (the "How it works" step copy: `Pick a video quality or MP3 audio` → `Pick a video quality, or MP3 or M4A audio`)
- Test: `tests/unit/test_page.py`

**Interfaces:**
- Consumes: inspect `audio.m4a` and job `output: "m4a"` (Task 3).
- Produces: none (leaf task).

- [ ] **Step 1: Write the failing page test.** In `test_app_js_carries_pinned_copy`, also require `"M4A audio (original)"` in `app.js`. In the index test, require `"MP3 or M4A audio"` in `index.html`.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_page.py`
Expected: FAIL on the missing copy.

- [ ] **Step 3: Implement in `static/app.js`**
  - `const M4A = "m4a";`
  - `normalize`: `audio: { available: Boolean(audio.available), m4a: Boolean(audio.available) && audio.m4a === true }`
  - `renderFormats`: after the MP3 chip, append `formatChip(M4A, "M4A audio (original)", true)` **only** when `info.audio.m4a`, so it's hidden, not disabled, when unavailable. The default pick doesn't change.
  - `chosenFormat`: `M4A` → `{ output: "m4a", quality: "best" }`.
  - `startJob`: `if (output === "m4a" && !inspected.info.audio.m4a) return;`. Announce "Audio download started." for any non-video output.
  - `loadJob`: `output: saved.output === "audio" || saved.output === "m4a" ? saved.output : "video"`.
  - `paintJob`: kind is `"MP3"` for `audio`, `"M4A"` for `m4a`, otherwise `"Video"`.
  - Update the comment on `AUDIO`. If no new Tailwind classes were added, the CSS doesn't need rebuilding; if any were, run `scripts/build-css.sh`.

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Check in a real browser.** Start the app with `.venv/bin/python -m app` and open `http://127.0.0.1:8000`.
  - Inspect the YouTube URL from `tests/network/urls.py`. The chips should show `Best (…p… · …)`, the tiers, "MP3 audio" and "M4A audio (original)".
  - Download M4A. The job card should say "M4A", and the saved `.m4a` should play with its title and cover.
  - Inspect a direct `.mp4` link (no known codec). There should be no M4A chip.
  - Reload the tab before the upgrade (with an MP3 job stored on the old page), or simulate it: `sessionStorage.setItem("saveme.job", JSON.stringify({id: "<22-char id>", output: "audio", ...}))`. After a reload the card should say "MP3". (Review Focus 5.)
  - Check light and dark mode at phone width (375px) and desktop: no layout break with the extra chip.

- [ ] **Step 6: Commit**

```bash
git add static/app.js static/index.html tests/unit/test_page.py
git commit -m "feat: add the M4A audio choice to the page"
```

---

### Task 6: Docs, real-site checks and the image

**Files:**
- Modify: `README.md`
- Modify: `tests/network/test_real_sites.py`
- Test: `tests/network/test_real_sites.py` (opt-in, `-m network`)

**Interfaces:**
- Consumes: everything above.
- Produces: none.

- [ ] **Step 1: Extend the network test.** Add `test_youtube_m4a_and_tags_and_cover(client, tmp_path)`:
  - Inspect `urls.YOUTUBE`, and assert `info["audio"]["m4a"] is True` and the best label starts with `"Best ("`.
  - Run an `m4a` job and download it. `_codecs(probe, "audio") == ["aac"]`.
  - `MP4(path).tags` has a non-empty `©nam` (title) and `©ART` (artist), and `covr`.
  - No tag value contains `"youtube.com/watch"` or `"youtu.be"`.
  - Run an `audio` (MP3) job: `ID3(path)` has `TIT2`, `TPE1` and one `APIC`.

- [ ] **Step 2: Update `README.md`**
  - **Features:** M4A, tags (title, artist, date, description, chapters), cover art.
  - **Usage:** what "M4A audio (original)" is and when it appears, and how to read the "Best" label.
  - **Security:** the thumbnail is fetched by the worker through the same guard, at most 5 MB, JPEG/PNG only, and written by mutagen, so FFmpeg never reads it. Files never carry the link they came from. The FFmpeg build gained only the `ffmetadata` demuxer (chapters).

- [ ] **Step 3: Rebuild and smoke-test the image**

Run: `scripts/docker-ffmpeg-check.sh && scripts/docker-smoke.sh`
Expected: all `PASS`, and the smoke test ends healthy.

- [ ] **Step 4: Run the real-site tests against the container**

Run: `docker compose up -d --build && .venv/bin/pytest -m network -v`
Expected: the YouTube and direct-link tests pass, including the new one. Reddit may still fail; that's pre-existing and documented.

- [ ] **Step 5: Run the full offline suite once more**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add README.md tests/network/test_real_sites.py
git commit -m "docs: describe M4A, file tags and cover art"
```
