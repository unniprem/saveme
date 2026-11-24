# Make shorts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Clip Studio's AI Highlights and Auto Shorts tabs with a single **Make shorts** tab. It has three numbered
steps (find moments → pick the ones you want → your shorts), and every other setting sits under a closed **More settings**.

**Architecture:** This is a page-only change. `index.html` moves the existing controls into the new tab. `ai.js` keeps finding moments and draws the moment
cards. `shorts.js` keeps making shorts and draws step 3. The two scripts hand off through a small `moments` state on
`window.SaveMeStudioBridge` in `studio.js`, which replaces today's route through the clip list. No server file changes.

**Tech Stack:** Plain browser JavaScript (no dependencies), HTML, and Tailwind v4 (built by `scripts/build-css.sh`). The tests are pytest (`tests/unit/test_page.py`, which does
static checks of the page files), plus a Playwright walkthrough in Chromium.

**Spec:** [`docs/superpowers/specs/2026-11-24-make-shorts-design.md`](../specs/2026-11-24-make-shorts-design.md). Read it first. It
says what each step must do and why. This plan says which file, which name, and which check.

## Global Constraints

- No server change. Nothing under `app/` is edited. The full suite stays at **1240 passed, 3 skipped, 8 deselected** (the baseline on `main` at `8388b08`), plus or minus only the page tests changed here.
- CSP is `script-src 'self'; style-src 'self'`. No inline script, inline style, `style=` attribute or `on*=` handler in the markup. Server text is set with `textContent` only (`test_static_js_never_injects_html` already enforces this). Setting `element.style.width` from JS is allowed, as today.
- Every `<button>` has `type="button"` (except `#inspect`). Labels use `for=`. Live messages use `aria-live="polite"`. A section that takes focus when its button disables itself has `tabindex="-1"`.
- The vocabulary is fixed. **Moments** are what the AI found, **shorts** are the finished videos, and **clips** belong to the Manual tab only. The tab is named **Make shorts**.
- Pinned copy, exact:
  - Step headings: `1. Find moments`, `2. Pick the ones you want`, `3. Your shorts`.
  - Questions: `Where will you post?`, `How many shorts?`, `What should they be about?` (plus the existing `(optional)` span).
  - `Only the transcript text is sent to the AI, never the video.`
  - `Transcription runs on this computer. To find moments, SaveMe sends the transcript text (never the audio or video) to OpenRouter, which passes it to the AI model.`
  - `Scores are the AI's editorial judgement of each moment, not a prediction of views.`
  - `No moments found. Try other words in "What should they be about?", or other lengths under More settings.`
  - `Clips you cut by hand`
  - `You can make up to 20 shorts at once.`
  - `Make them without captions`
  - Buttons: `Find moments`, `▶ Play`, `More`, `Edit captions`, `Save plain cut`, `Save`, `Download all`, `More settings`.
- Defaults on the simple path: platform `youtube_shorts`, 5 shorts, template `talking_head` (it's `options.templates[0]`, as today), quality `standard`, fps `source`, captions on and burned in, no branding.
- The AI's moments start **ticked**. Hand-cut clips start **unticked**. At most `options.limits.max_clips` (20) shorts per Make.
- Element IDs that survive keep their names, so existing logic and `test_page.py` keep working. The removed IDs are listed in Task 1.
- Dev harness for every browser check:
  1. `scripts/build-css.sh`. This builds `static/dist/styles.css`, which is untracked. Rebuild after any class change.
  2. `.venv/bin/python /Users/unnikrishnan/.claude-max/jobs/0dd87ae1/tmp/dev/ai/serve_ai.py "$PWD" /Users/unnikrishnan/.claude-max/jobs/0dd87ae1/tmp/dev/ai 8077 configured`
     (or `unconfigured` for "AI not set up"). It runs SaveMe on `http://127.0.0.1:8077` with the real Whisper transcriber and a simulated
     OpenRouter, and prints `MEDIA <url>`, a 100 s talk to paste into the page. Its simulated model picks lines that share words with the
     instruction (try "artificial intelligence").
  3. Drive it with Playwright in Chromium, through the Playwright MCP browser tools or a `check-*.js` script like the earlier ones in
     `/Users/unnikrishnan/.claude-max/jobs/0dd87ae1/tmp/dev/`.
  4. Task 1's first browser check confirms `GET /api/shorts/options` answers `available: true` under this harness. If it doesn't, give
     `create_app` the same render settings `tests/support/render.py` uses, in a copy of the harness, and note it in the journal.

## Review Focus

These are the inputs the spec implies but no static test exercises. Each one has a browser check in the task that owns the code.

1. **A reload while shorts are being made.** Ticks, the Make count and step 3's progress come back, polling resumes, and Find moments
   stays disabled until the render ends. Owner: Task 2.
2. **A moment's times edited after it's ticked.** The short uses the edited range. Caption edits made for the old range don't follow it
   to the new one. Owner: Task 3.
3. **A hand-cut clip with exactly a moment's range, both ticked.** One short is made, carrying the moment's title. Owner: Task 2.
4. **Opening another video while moments are shown.** Picks clear, step 2 hides, and no short or caption edit from the old video
   carries over. Owner: Task 2.
5. **A preset that sets a platform outside the three chips** (for example Square). The chips all show unselected, with `Other: Square`,
   and the short is made square. Owner: Task 4.

---

### Task 1: Two tabs, and the Make shorts panel laid out in steps

Move the markup into its new places, switch the tab code to two tabs, and delete the duplicate controls and their code. The handoff
from moments to shorts is added in Task 2. Until then, step 2 lists the moments and the hand-cut clips, but Make only uses the hand-cut ones.

**Files:**
- Modify: `static/index.html` (the studio tabs, `#manual-panel`, `#ai-panel` and `#shorts-panel` at lines 204–548, and `#clip-list-wrap` at line 549)
- Modify: `static/ai.js` (tabs at lines 89–122; removed: `addSelected`, `loadIntoEditor` and the stage list in `paintProgress`)
- Modify: `static/shorts.js` (removed: `useMarks`, `toggleAll`, and the `el` entries for removed IDs)
- Test: `tests/unit/test_page.py`

**Interfaces:**
- Produces: the element IDs below, which Tasks 2–5 use. `ai.js` keeps `TABS = ["manual", "make"]` and stores the tab in `saveme.ai`
  as `"make"`. A stored `"ai"` or `"shorts"` restores as `"make"`.

Markup outline for `#make-panel` (top to bottom; the IDs are exact, the classes follow the page's existing patterns):

```
#studio-tabs (role=tablist, aria-label kept)
  button#tab-manual (role=tab, aria-controls=manual-panel, aria-selected=true, tabindex=0)  "Manual"
  button#tab-make   (role=tab, aria-controls=make-panel,   aria-selected=false, tabindex=-1) "Make shorts"
#manual-panel  …unchanged…, and now ends with #clip-list-wrap (moved inside it)
#make-panel (role=tabpanel, aria-labelledby=tab-make, hidden)
  section[aria-labelledby=make-step-find]
    h3#make-step-find "1. Find moments"
    form#ai-form (novalidate)
      fieldset#make-platform-chips > legend "Where will you post?"
        3 × label > input[type=radio][name=make-platform] values youtube_shorts (checked) | instagram_reels | tiktok,
            ids make-platform-youtube_shorts | make-platform-instagram_reels | make-platform-tiktok,
            texts "YouTube Shorts" | "Instagram Reels" | "TikTok"
        span#make-platform-other (hidden)
      label[for=ai-count] "How many shorts?" + select#ai-count
      label[for=ai-instruction] "What should they be about?" (optional) + textarea#ai-instruction (existing attributes)
      button#ai-start "Find moments"
      p#ai-estimate, p#ai-form-msg (aria-live), p#make-privacy (the pinned one-liner), p#ai-status (aria-live, hidden)
    section#ai-progress (hidden, tabindex=-1): status line, percent, #ai-progress-bar, #ai-progress-detail, #ai-cancel, with no #ai-stages
  details#make-more (closed) > summary "More settings"
    section "Finding moments" (h4): p#ai-model, #ai-test, #ai-test-msg, #ai-privacy (the pinned full note), #ai-min, #ai-max,
        #ai-min-score, #ai-language, #ai-retranscribe, button#ai-transcribe "Transcribe only", details#ai-transcript (as today, with subtitle import)
    section "Look" (h4): #shorts-status, the "rendered on this computer" note, #shorts-platform, #shorts-template (label "Video type"),
        #shorts-template-desc, #shorts-quality, #shorts-fps, then today's Framing fields
    section "Captions" (h4): today's captions section, unchanged inside
    details#shorts-branding, details#shorts-presets (unchanged)
    section "Preview" (h4): today's preview section, unchanged inside
  section#make-pick[aria-labelledby=make-step-pick]
    h3#make-step-pick "2. Pick the ones you want"
    section#ai-results (hidden, tabindex=-1): h4#ai-results-title "Moments the AI found", #ai-results-count, button#ai-select-all,
        #ai-usage, #ai-notice, #ai-error, p#make-no-moments (hidden, the pinned copy), #ai-cards-msg, ol#ai-cards
    section#shorts-hand (hidden, aria-labelledby=shorts-clips-title): h4#shorts-clips-title "Clips you cut by hand", ol#shorts-clips
    section#shorts-editor (as today)
    p#shorts-render-msg (aria-live) + button#shorts-render > span#shorts-render-label "Make shorts"
  section#shorts-progress (hidden, tabindex=-1, aria-labelledby=make-step-shorts)
    h3#make-step-shorts "3. Your shorts", the status/percent/bar and estimates note as today,
    div#shorts-nocaps (hidden) > p#shorts-nocaps-msg (aria-live) + button#shorts-nocaps-go "Make them without captions",
    ol#shorts-results, a#shorts-zip[download] "Download all", button#shorts-cancel
#export …unchanged, after both panels…
```

Removed IDs: `tab-ai`, `tab-shorts`, `ai-panel`, `shorts-panel`, `ai-stages`, `ai-select-none`, `ai-add-selected`, `ai-bulk`,
`shorts-use-marks`, `shorts-select-all`, `shorts-clips-empty`. The static "Scores are…" paragraph leaves the markup. Its copy moves into
`ai.js` in Task 3.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/test_page.py`:
  - replace `AI_IDS` and `AI_COPY` with `MAKE_IDS` and `MAKE_COPY`;
  - replace `test_ai_highlights_markup` with `test_make_shorts_markup`, and `test_ai_copy_is_pinned` with `test_make_shorts_copy_is_pinned`;
  - edit `SHORTS_IDS` and `test_auto_shorts_markup` to drop `tab-shorts`, `shorts-panel`, `shorts-use-marks` and `shorts-select-all`,
    and to add `shorts-hand`, `shorts-nocaps`, `shorts-nocaps-msg` and `shorts-nocaps-go`.

  Add this helper:

```python
def _between(start_id: str, end_id: str) -> str:
    h = _html()
    a = h.index(f'id="{start_id}"')
    return h[a:h.index(f'id="{end_id}"', a)]
```

  `MAKE_IDS` is every ID named in the outline above, except the `shorts-*` ones, which stay in `SHORTS_IDS`. `test_make_shorts_markup` asserts:
  - every ID in `MAKE_IDS` exists, and no removed ID exists;
  - the `role="tab"` buttons, in order, are exactly `["tab-manual", "tab-make"]`;
  - `tab-make` has `aria-controls == "make-panel"` and `aria-selected == "false"`;
  - `make-panel` has `role == "tabpanel"`, `aria-labelledby == "tab-make"` and is `hidden`;
  - `'id="clip-list-wrap"' in _between("manual-panel", "make-panel")`;
  - the IDs come in this order in the page: `make-step-find`, `ai-progress`, `make-more`, `make-pick`, `make-step-shorts`, `export`;
  - `make-more` is a `details` with no `open` attribute;
  - the `make-platform` radios' values are `["youtube_shorts", "instagram_reels", "tiktok"]`, and only the first is `checked`;
  - each of `ai-count`, `ai-instruction`, `ai-start`, `ai-estimate`, `make-privacy` and `ai-status` is in `_between("make-step-find", "make-more")`;
  - each of these is in `_between("make-more", "make-pick")`: `ai-model`, `ai-test`, `ai-min`, `ai-max`, `ai-min-score`, `ai-language`,
    `ai-retranscribe`, `ai-transcribe`, `ai-transcript`, `ai-subs-file`, `shorts-platform`, `shorts-template`, `shorts-quality`,
    `shorts-fps`, `shorts-layout`, `shorts-margin`, `shorts-manual`, `shorts-captions`, `shorts-burn`, `shorts-srt`, `shorts-style`,
    `shorts-branding`, `shorts-presets`, `shorts-preview-frame`;
  - `>1. Find moments<`, `>2. Pick the ones you want<` and `>3. Your shorts<` are in the page;
  - `ai-progress`, `ai-results` and `shorts-progress` have `tabindex == "-1"`;
  - `ai-form-msg`, `ai-status`, `ai-test-msg`, `ai-subs-msg`, `shorts-render-msg` and `shorts-nocaps-msg` are `aria-live="polite"`.

  `test_make_shorts_copy_is_pinned` checks every `MAKE_COPY` string against `page + ai.js + shorts.js`, and keeps today's route and
  bridge asserts for `ai.js`. Add these to `MAKE_COPY`: `"Clips you cut by hand"`, the two privacy lines, the three step headings, the three
  questions, and the "No moments found…" line (it's in `#make-no-moments`). The "Scores are…", "up to 20" and "without captions" strings
  are added by the tasks that write them.

- [ ] **Step 2: Run the tests and watch them fail.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q`.
  Expected: the new tests FAIL. For example `tab-make` is missing, or `tab-ai` is still present.

- [ ] **Step 3: Restructure `static/index.html`** to the outline above. Move the existing elements; don't recreate them. Keep the existing
  labels, attributes and classes of every surviving control. The stage `<ol>`, the bulk bar, "Use current marks" and the Auto Shorts
  "Select all" go.

- [ ] **Step 4: Update `static/ai.js` for the new markup.**
  - The `el` list drops the removed IDs and gains `tab-make`, `make-panel`, `ai-model` and `make-no-moments`.
  - `TABS = ["manual", "make"]`. `tabParts("make")` returns `[el.tabMake, el.makePanel]`.
  - In `onSource`, a stored `tab` of `"ai"` or `"shorts"` maps to `"make"`.
  - `#ai-select-all` becomes a toggle: when every moment is ticked it reads `Select none` and unticks them all, otherwise it reads `Select all`.
  - `addSelected`, `loadIntoEditor` and `MSG.noneSelected` are deleted, together with the stage-list loop and `STAGES`/`lastStage` in `paintProgress`.
    The headline and percent still show, and a stopped run still shows its error in `#ai-progress-detail`.

- [ ] **Step 5: Update `static/shorts.js` for the new markup.**
  - The `el` list drops `shorts-panel`, `shorts-use-marks`, `shorts-select-all` and `shorts-clips-empty`, and gains `shorts-hand`,
    `shorts-nocaps`, `shorts-nocaps-msg` and `shorts-nocaps-go`.
  - `useMarks` and `toggleAll` are deleted, with their listeners.
  - `renderClips` shows `#shorts-hand` only when `clips.length > 0`.
  - The render label reads `Make ${n} short(s)`, or `Make shorts` at 0.

- [ ] **Step 6: Run the tests and watch them pass.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q`. Expected: PASS.

- [ ] **Step 7: Browser check.** Use the dev harness, configured, at 1200 px.
  - `GET /api/shorts/options` reports `available: true`.
  - Paste the MEDIA URL and open Clip Studio. You see two tabs, and ArrowRight/ArrowLeft wrap between them.
  - Make shorts shows step 1, then a closed More settings. Opening it shows the five groups.
  - The Manual clip list shows only on Manual.
  - A reload on Make shorts comes back to Make shorts.
  - The console has no errors.

- [ ] **Step 8: Commit.**

```bash
git add static/index.html static/ai.js static/shorts.js tests/unit/test_page.py
git commit -m "feat: lay out Clip Studio as Manual and Make shorts, with steps and More settings"
```

### Task 2: Hand the picked moments to Make, with their titles

**Files:**
- Modify: `static/studio.js` (the bridge at lines 1003–1110)
- Modify: `static/ai.js` (`store`, `onSource`, `paintResults`, the tick handlers, `paintButtons`)
- Modify: `static/shorts.js` (`onClips`, `restore`, `chosen`, `clipBody`, `paint`, `poll`)
- Test: `tests/unit/test_page.py`

**Interfaces:**
- Produces, on `window.SaveMeStudioBridge`:
  - `setMoments(state: {shown: boolean, picks: Array<{start: number, end: number, title: string}>}): void`. This replaces the state.
    Times are rounded to whole ms with the existing `ms()`. Ranges shorter than `MIN_CLIP`, or past the video, are dropped. Titles are
    `String(title || "").slice(0, 120)`. It notifies the `onMoments` callbacks.
  - `onMoments(callback: (state) => void): void`. It calls the callback at once with a copy, then on every change.
  - `setMaking(on: boolean): void` and `onMaking(callback: (on: boolean) => void): void`, the same pattern.
  - When the open video changes or closes, both reset to `{shown: false, picks: []}` and `false`, and their callbacks run.
- Produces, in `ai.js`: `publish(): void`. It calls `bridge.setMoments({shown: Boolean(shown), picks})`, where `picks` is the ticked
  moments in rank order as `{...rangeOf(h), title: str(h.title)}`. It runs after `paintResults`, after every tick or time edit, after
  Select all/none, and after `onSource` restores.
- Consumes, in `shorts.js`: `onMoments`, `setMaking`.

- [ ] **Step 1: Write the failing test.** Add `test_moments_handoff_is_wired`, which asserts:
  - `"setMoments(" in studio`, `"onMoments(callback)" in studio`, `"setMaking(" in studio`, `"onMaking(callback)" in studio`;
  - `"bridge.setMoments(" in ai`, `"bridge.onMaking(" in ai`;
  - `"bridge.onMoments(" in shorts`, `"bridge.setMaking(" in shorts`;
  - `"You can make up to 20 shorts at once." in shorts`;
  - `make-pick` now carries `hidden`. Add that to `test_make_shorts_markup` too.

- [ ] **Step 2: Run the test and watch it fail.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q -k "handoff or make_shorts_markup"`. Expected: FAIL.

- [ ] **Step 3: Add the bridge state to `static/studio.js`** with the signatures above. Add `hidden` to `#make-pick` in `index.html`.

- [ ] **Step 4: Make `ai.js` publish moments, ticked by default.**
  - Add `pickedFor` (an analysis id) to the stored `saveme.ai` state.
  - In `paintResults`, when `str(v.analysis_id) !== pickedFor`, tick every moment, clear `edits` and set `pickedFor`. This replaces the
    `el.aiCards.dataset.analysis` reset. A reload of the same analysis keeps the stored ticks.
  - `onMaking` sets a `making` flag. `paintButtons` disables `#ai-start` while it's true.

- [ ] **Step 5: Make `shorts.js` use them.**
  - Keep `moments` from `onMoments`.
  - `chosen()` returns the picks first, then the ticked hand-cut clips, without repeating a `keyOf` already present. This way a clip with a
    moment's exact range is made once, with the moment's title.
  - `clipBody(c)` adds `title: c.title` when it isn't empty.
  - Hand-cut clips start unticked. `onClips` no longer ticks new rows, and `restore()` with no saved state starts empty.
  - `paint()` controls three things:
    - `#make-pick` is visible when `moments.shown || clips.length > 0`;
    - Make is disabled at 0 picks, and also above `options.limits.max_clips`, in which case `#shorts-render-msg` shows the pinned
      "up to 20" line;
    - when `options.available` is false, Make is disabled and `#shorts-render-msg` shows the server's `options.error.message`,
      because `#shorts-status` is out of sight in the closed More settings.
  - Call `bridge.setMaking(pending)` whenever the render's pending state changes.
  - The preview picker is filled from `chosen()`, labelled with the title (or `Clip N`) and the times.

- [ ] **Step 6: Run the tests and watch them pass.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q`. Expected: PASS.

- [ ] **Step 7: Browser check (Review Focus 1, 3 and 4).** Run the harness, configured.
  1. Instruction "artificial intelligence", Find moments. The cards arrive all ticked, and Make reads "Make N shorts". Untick one, and
     N drops by one.
  2. In Manual, set marks to exactly one moment's range and press Add to list. Back on Make shorts, the clip shows unticked under
     "Clips you cut by hand". Tick it, and N doesn't change.
  3. Make. Each short in the ZIP is named after its moment's title (`Content-Disposition`).
  4. Make again, and reload during rendering. The ticks, the count and step 3's progress come back, and Find moments is disabled until
     the render ends.
  5. Close the studio and open the MEDIA URL again. Step 2 is hidden and Make is disabled.

- [ ] **Step 8: Commit.**

```bash
git add static/studio.js static/ai.js static/shorts.js static/index.html tests/unit/test_page.py
git commit -m "feat: make the ticked moments into shorts, titles included, without the clip list"
```

### Task 3: Simple cards, with everything else behind More

**Files:**
- Modify: `static/ai.js` (`card`, `exportOne`)
- Modify: `static/shorts.js` (`renderClips`, `openEditor`, `editorReset`; the new global)
- Test: `tests/unit/test_page.py`

**Interfaces:**
- Produces, in `shorts.js`: `window.SaveMeShorts = Object.freeze({ editCaptions(range: {start: number, end: number}, label: string): void })`.
  It opens `#shorts-editor` for that range, titled `Captions for ${label} (${start} – ${end})`. When Auto Shorts is unavailable, it
  shows the reason in `#shorts-render-msg` and returns. `openEditor` and `editorReset` take the range and label rather than a clip-list
  row. The label is kept as `editingLabel`.
- Consumes, in `ai.js`: `window.SaveMeShorts.editCaptions`, looked up at click time, because `shorts.js` loads after `ai.js`.

A card's face, for both a moment and a hand-cut clip:
- the tick box (its `aria-label` names the title);
- `h4` with the title (for a hand-cut clip, `Clip N`);
- a line of `#N · 1:02 – 1:44 · 0:42` (for a hand-cut clip, no rank);
- for a moment, the summary clamped to two lines (`line-clamp-2`);
- a "Captions edited" badge when there are edits;
- `▶ Play`, which loads the range and plays it, as `preview(h)` does today.

Then `details` > `summary` "More" (its `aria-label` is `More about moment N` or `More about clip N`).
- For a moment it holds:
  - the existing Start/End fields, length and message;
  - `Why it was picked: …`;
  - `Score N.` followed by the pinned "Scores are…" sentence;
  - the tags;
  - `Edit captions`;
  - `Save plain cut`.
- For a hand-cut clip it holds only `Edit captions`.

- [ ] **Step 1: Write the failing test.** Add `test_cards_are_simple`, which asserts:
  - `"window.SaveMeShorts = Object.freeze(" in shorts` and `"editCaptions(" in shorts`;
  - `"window.SaveMeShorts" in ai`;
  - each of `"▶ Play"`, `"Save plain cut"`, `"Edit captions"`, `"Why it was picked: "` and `"line-clamp-2"` is in `ai`;
  - `"Load into editor" not in ai`.

  Add `"Scores are the AI's editorial judgement of each moment, not a prediction of views."` to `MAKE_COPY`.

- [ ] **Step 2: Run the test and watch it fail.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q -k "cards_are_simple or copy"`. Expected: FAIL.

- [ ] **Step 3: Rewrite `card(h, n)` in `ai.js`** to the face-and-More layout above. Keep the existing time-field validation, but call
  `publish()` after an edit. `exportOne` says `Saving moment ${n} as a plain cut. Its progress shows below the steps.`

- [ ] **Step 4: Rewrite `renderClips()` in `shorts.js`** to the same face, plus a More that holds only Edit captions. Play calls
  `bridge.loadRange(c.start, c.end, true)`. Then add
  `window.SaveMeShorts` and generalize `openEditor`/`editorReset`.

- [ ] **Step 5: Run the tests and watch them pass.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q`. Expected: PASS, including `test_static_js_never_injects_html`.

- [ ] **Step 6: Browser check (Review Focus 2).**
  1. A card shows only its face. Opening More shows the rest.
  2. Edit captions on moment 2 opens the editor titled for moment 2. Change a word, then Done, and the card shows "Captions edited".
  3. Change moment 2's end time in its More. The badge goes, Make still counts it, and the made short has the new length (ffprobe) and
     automatic captions.
  4. Save plain cut starts the plain export below the steps.
  5. At 375 px in dark mode, there's no horizontal scroll and no button under 40 px.

- [ ] **Step 7: Commit.**

```bash
git add static/ai.js static/shorts.js tests/unit/test_page.py
git commit -m "feat: show each moment and clip as a simple card, with the rest behind More"
```

### Task 4: Step 1: the platform chips, short status lines and one progress bar

**Files:**
- Modify: `static/shorts.js` (chips, `applySpec`, `loadOptions`)
- Modify: `static/ai.js` (`MSG`, `HEADLINE`, `paintStatus`, `estimate`, `paintButtons`, `paintResults`, `importSubtitles`, the announcements)
- Test: `tests/unit/test_page.py`

**Interfaces:**
- Produces, in `shorts.js`: `syncChips(): void`.
  - It checks the `make-platform` radio whose value equals `el.platform.value`.
  - If none matches, it unchecks all three and shows `#make-platform-other` as `Other: ${platform.name}`. Otherwise it hides it.
  - It runs after `loadOptions`, `applySpec` and every `#shorts-platform` change.
  - A radio `change` sets `el.platform.value`, then calls `changed()`, `refreshCaptions()` and `syncChips()`.

- [ ] **Step 1: Write the failing test.** Add `test_step_one_copy`, which asserts:
  - `"syncChips(" in shorts` and `'name="make-platform"' in page`;
  - `"Finding moments needs an OpenRouter key" in ai`;
  - `"Ranking moments" in ai` and `"highlights found" not in ai`.

- [ ] **Step 2: Run the test and watch it fail.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q -k step_one`. Expected: FAIL.

- [ ] **Step 3: Rewrite the status lines in `ai.js`.**
  - `paintStatus` puts the model line in `#ai-model`.
  - `#ai-status` shows only when Find can't run:
    - when AI is unavailable: `Finding moments is off: ${message} Fix the setting and restart SaveMe.`
    - when it isn't configured, `MSG.notConfigured` reads: `Finding moments needs an OpenRouter key: add OPENROUTER_API_KEY to the server's .env and restart SaveMe (see docs/OPENROUTER_SETUP.md). You can still make shorts from clips you cut by hand in Manual.`
  - `estimate()` writes `Estimated cost: about $X (at most $Y).` When the cost is over the limit (with `basis === "transcript"`), it appends
    `That's over the $L limit, so it won't run.`, sets an `overLimit` flag, and `paintButtons` disables `#ai-start` while that flag is set.
  - The text says moments, not highlights:
    - `HEADLINE.ranking` is `Ranking moments`, and `HEADLINE.ready` is `Moments ready`;
    - the announcements are `Looking for moments.` and `${n} moment(s) found.`;
    - `MSG.noTranscript` says `Find moments`, and so does the subtitle-import success line.
  - `paintResults` shows `#make-no-moments` when there are no moments, and hides `#ai-select-all` then.

- [ ] **Step 4: Add `syncChips` and the radio wiring to `shorts.js`.**

- [ ] **Step 5: Run the tests and watch them pass.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q`. Expected: PASS.

- [ ] **Step 6: Browser check (Review Focus 5).**
  1. Choose TikTok. Under More settings → Look, the select shows TikTok.
  2. Choose Square there. The chips all show unselected, with "Other: Square".
  3. Save that as a preset, choose YouTube Shorts, then load the preset. The chips show "Other: Square" again, and a made short is 1080×1080.
  4. Restart the harness with `unconfigured`. `#ai-status` shows the key message, Find moments is disabled, and step 2 stays hidden
     until a clip is added in Manual.
  5. A search that the simulated model can't match (instruction "zzzz qqqq") shows "No moments found…".

- [ ] **Step 7: Commit.**

```bash
git add static/ai.js static/shorts.js tests/unit/test_page.py
git commit -m "feat: ask where to post with chips, and keep step 1's status to one line each"
```

### Task 5: Step 3: finished shorts play on the page, and captions never go missing silently

**Files:**
- Modify: `static/shorts.js` (`paintProgress`, `startRender`, `transcribe`, `loadOptions`, `paint`)
- Test: `tests/unit/test_page.py`

**Interfaces:**
- Produces, in `shorts.js`:
  - `runTranscription(g: number, retranscribe: boolean): Promise<void>`. This is today's `transcribe()` loop, moved out. It throws
    `Error(message)` when the analysis can't start (the server's message, for example `video_too_long` or AI being off) or when it ends
    `failed` or `cancelled`. It reports progress through a callback argument `(text: string) => void`.
  - `captionsGate(list): Promise<{ok: true} | {ok: false, reason: string}>`. It returns ok when `#shorts-captions` is off. It returns
    not-ok with `This FFmpeg can't burn in captions.` when burning in is on and `options.captions_available` is false. It returns ok when
    `POST /shorts/captions` for `list[0]` returns captions. Otherwise it runs `runTranscription`, which writes its status into step 3,
    and returns ok, or not-ok with the error's message.
  - `startRender(noCaptions = false)`. Unless `noCaptions` is set, it awaits `captionsGate`. When the gate says not-ok, it shows
    `#shorts-progress` with `#shorts-nocaps` visible, sets `#shorts-nocaps-msg` to `Captions can't be made: ${reason}`, and stops.
    `#shorts-nocaps-go` calls `startRender(true)`, which sends `spec()` with `captions.enabled = false` for this request only. The
    checkbox is left alone.
  - `aiStatus`: `GET /api/ai/status`, fetched once in `loadOptions`, or `null` on error.

- [ ] **Step 1: Write the failing test.** Add `test_step_three`, which asserts:
  - `"captionsGate(" in shorts` and `"runTranscription(" in shorts`;
  - `"/api/ai/status" in shorts`;
  - `"Make them without captions" in page`;
  - `'createElement("video")' in shorts`;
  - `"Download all" in page`.

  Add `"Make them without captions"` to `MAKE_COPY`.

- [ ] **Step 2: Run the test and watch it fail.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q -k step_three`. Expected: FAIL.

- [ ] **Step 3: Rewrite each row in `paintProgress`.**
  - It shows the short's title (`c.title`, or `Short N`), the times and the status, as today.
  - When the short is ready, it adds `<video controls playsinline preload="metadata">` with `src` set to the MP4 file URL. The range
    requests are already served (`render_routes.py:194`), and `media-src` falls back to `default-src 'self'`. It also adds a `Save` link
    (with `download`, and `aria-label` `Save short N`), plus the SRT link as today.
  - Skip stays on pending rows.

- [ ] **Step 4: Add `runTranscription`, `captionsGate` and the new `startRender` flow.**
  - The More settings → Captions "Transcribe" button uses `runTranscription` too.
  - `#shorts-progress` shows when there's a render view, a transcription running, or the no-captions block.
  - Before anything starts, `paint` puts a notice in `#shorts-render-msg` (worded by the builder) when captions are known not to work:
    - when burning in is on and `options.captions_available` is false;
    - or when there's no transcript, `aiStatus` exists, and `!aiStatus.available` or `source.duration > aiStatus.max_source_minutes * 60`.

- [ ] **Step 5: Run the tests and watch them pass.**
  Run `.venv/bin/pytest tests/unit/test_page.py -q`. Expected: PASS.

- [ ] **Step 6: Browser check.**
  1. A fresh harness: in Manual, add a clip by hand. Don't find moments, so there's no transcript.
  2. Tick the clip and Make. Step 3 shows transcribing on this computer, then rendering. The short plays on the page with captions, and
     Save downloads it.
  3. Restart the harness with `AI_MAX_SOURCE_MINUTES=1` (the media is 100 s). One way is an env override in a harness copy that passes it to
     `load_ai_settings`.
  4. Add a clip and Make. The notice is above Make first. Step 3 then shows `Captions can't be made: …`, and Make them without
     captions produces a short with no caption ink and the captions checkbox still ticked.
  5. Cancel during the transcription stops it, and Make is enabled again.

- [ ] **Step 7: Commit.**

```bash
git add static/shorts.js tests/unit/test_page.py
git commit -m "feat: play finished shorts on the page, and ask before making them without captions"
```

### Task 6: Docs, the full suite, the end-to-end walkthrough and the journal

**Files:**
- Modify: `README.md`. The "AI Highlights" and "Auto Shorts" sections and their table-of-contents entries become one
  "Make shorts" section: the three steps, then "More settings" groups. The feature bullets at lines 74–75 are updated, and the
  troubleshooting row that mentions the AI tab names Make shorts.
- Modify: `docs/AI_CLIPPING_DESIGN.md` §11 and `docs/AUTO_SHORTS_DESIGN.md` §10. Each gets one first line:
  `Superseded for the page by [the Make shorts design](superpowers/specs/2026-11-24-make-shorts-design.md); the server parts below still hold.`
- Modify: `JOURNAL.md`. Add a new top entry in the existing format.

- [ ] **Step 1: Update the docs** as listed above. `grep -n -E "AI Highlights tab|Auto Shorts tab|Add selected to clip list|Use current marks" README.md docs/*.md` finds only historical text in the design docs, not README instructions.

- [ ] **Step 2: Run the full suite.**
  Run `.venv/bin/pytest -q`.
  Expected: `1240 passed`, with the count changed only by the page tests changed here, plus `3 skipped, 8 deselected`.

- [ ] **Step 3: Run the end-to-end walkthrough** at 1200 px light and 375 px dark, with no console errors:
  1. The simple path: TikTok, 3 shorts, "artificial intelligence", Find moments, untick one, Make 2 shorts, play one, Save, then Download
     all (a ZIP with 2 entries that passes `testzip`).
  2. Cancel during finding, and Cancel during making.
  3. Find moments again: the new moments are all ticked, and the old shorts stay in step 3.

  Save the screenshots in `$CLAUDE_JOB_DIR/tmp/dev/shots/make-*.png`.

- [ ] **Step 4: Commit the docs.**

```bash
git add README.md docs/AI_CLIPPING_DESIGN.md docs/AUTO_SHORTS_DESIGN.md
git commit -m "docs: describe Make shorts in place of the AI Highlights and Auto Shorts tabs"
```

- [ ] **Step 5: Write the JOURNAL.md entry** with the session's commits from `git log --oneline main..`, the decisions (from the spec's
  "Decided after the builder check" items) and anything the walkthroughs found. Then commit:

```bash
git add JOURNAL.md
git commit -m "docs: update journal"
```
