# Make shorts: one simple path from a video to finished shorts (design)

**Date:** 2026-11-24
**Builds on:** [AI_CLIPPING_DESIGN.md](../../AI_CLIPPING_DESIGN.md) (§11 UI) and
[AUTO_SHORTS_DESIGN.md](../../AUTO_SHORTS_DESIGN.md) (§10 UI). This replaces those two UI sections only.
Everything on the server stays as those documents describe it.
**Size:** a project. It's a page change across `static/index.html`, `static/ai.js`, `static/shorts.js` and `static/studio.js`,
with no server change.

## 1. Intent

The owner finds AI clipping confusing. Today, getting finished shorts out of a video means:

1. **AI Highlights tab:** up to 7 settings, then Find highlights. You tick the cards, then **Add selected to clip list**.
2. **Auto Shorts tab:** tick the same clips again, then work through about 35 controls in 6 sections
   (Format, Framing, Captions, Branding, Presets, Preview). Then **Render shorts**.
3. You choose between two ZIP buttons that do different things. "Export clips as ZIP" under the clip list gives
   plain cuts, and "Download all (ZIP)" gives the finished shorts.

The settings also overlap. A template already picks the framing and the caption style, and those same choices
appear again further down as separate controls.

The owner wants one obvious path. They answer a few questions, see the moments the AI found, **pick the ones they
want**, and get those as finished shorts. They chose "pick first" over "make everything straight away" so they
don't wait for videos they won't keep.

## 2. Goals

- **Two tabs: Manual | Make shorts.** The AI Highlights and Auto Shorts tabs are merged into **Make shorts**.
- **Three questions on the simple path**, and nothing else is needed:
  1. **Where will you post?** Three choice chips: YouTube Shorts (the default), Instagram Reels, TikTok.
  2. **How many shorts?** A select from 1 to the server's `max_clips` (default 10). It starts at 5.
  3. **What should they be about?** An optional text box (≤ 500 characters, as today). The existing placeholder
     example is kept.
- **Three numbered steps on the page**, top to bottom:
  1. **Find moments.** The three questions, a one-line cost estimate, a one-line privacy note
     ("Only the transcript text is sent to the AI, never the video."), and a **Find moments** button. While it runs,
     there's one progress bar, one status line (for example "Transcribing… 40 %") and **Cancel**.
  2. **Pick the ones you want.** One card per moment, in rank order. Under the cards are **Select all** and
     **Make N shorts**, where N is how many are ticked (the button is disabled at 0).
  3. **Your shorts.** While they're made, there's one row per short with its own measured % plus the overall
     bar and **Cancel**. When a short is ready, it plays on the page and has **Save**. **Download all** gives the ZIP.
- **A moment card's face** shows only: a tick box, the title, the time and length ("1:02–1:44 · 42 s"), a one-line
  summary, and **▶ Play**. Play plays that part in the player above, as Preview does today. Everything else
  goes behind a small **More** toggle on the card:
  change the start and end, why it was picked (reason, score with today's "editorial judgement" note, tags),
  **Edit captions** (today's cue editor, for this moment), and **Save plain cut** (today's Export).
- **More settings** is one closed disclosure under step 1. It holds every setting that exists today, grouped:
  - *Finding moments:* shortest and longest length, minimum score, spoken language, transcribe again, Test
    connection, the full privacy note, the transcript viewer with search, subtitle import, **Transcribe only**.
  - *Look:* the other platforms (Instagram / Facebook feed, Square, Landscape, Source aspect), video type
    (today's template), framing mode, safe margin, manual framing, quality, frame rate.
  - *Captions:* every caption control from today, plus "Also save SRT files".
  - *Branding:* logo, watermark, brand colour, title bar, intro, outro.
  - *Presets:* save, load, delete, export, import (unchanged, still local to the browser).
  - *Preview:* the frame and draft-clip preview, with a picker over the ticked moments.
- **Moment titles reach the shorts.** The render request sends each moment's AI title. The server already uses it
  for the file name and the title bar (`clip.title`). Today the titles are lost on the way through the clip list,
  and every file is named after the video.
- **Clips cut by hand can still become shorts.** If the Manual clip list has rows, step 2 shows them as a second
  group, "Clips you cut by hand", with the same card face (titled "Clip 1", "Clip 2"…). Behind their **More** is
  only **Edit captions**. Their times are changed in Manual, where the list lives, so a clip never has two
  different ranges. Ticks and caption edits stay keyed by start and end, as in `shorts.js` today.
  *(Decided after the builder check.)*
- **Captions never silently go missing.** If captions are on and the video has no transcript yet (only possible
  with hand-cut clips), **Make N shorts** first runs a transcribe-only analysis on this computer, with its status
  shown in step 3, then makes the shorts. If captions can't be made, nothing is made yet. That happens when the
  transcription fails, can't run (the AI settings have an error, or the video is longer than
  `AI_MAX_SOURCE_MINUTES`), or FFmpeg has no `ass` filter while burning in is on. Step 3 then shows the reason and
  one button, **Make them without captions**, which makes the same shorts with captions off for this run only.
  When the page already knows captions can't be made, it says so above Make, before anything starts.
  *(Decided after the builder check.)*
- **One vocabulary:** *moments* are what the AI found, *shorts* are the finished videos, and *clips* belong to the
  Manual tab only.

## 3. Non-goals

- **Any server change.** Every route, limit, schema and worker stays as it is. The page uses the existing
  `/api/ai/*`, `/analyses*`, `/transcript`, `/api/shorts/*` and `/renders*` routes.
- **New settings or capabilities.** Nothing is added beyond the three questions, which are existing settings
  brought forward.
- **Making shorts without picking.** No "make everything straight away" mode (the owner chose pick first).
- **Changing the Manual tab's editing.** Marks, Play clip, Add to list, Export clip and the clip list behave as
  today.

## 4. Anti-goals

- **Losing a capability.** Every control on today's two tabs must still be reachable, either on the simple path or
  under More settings. The only exceptions are the removed duplicates listed in §6.
- **The simple path depending on More settings.** With More settings never opened, the defaults must produce
  good shorts: YouTube Shorts 9:16, the *Talking head* video type (follows the face, falls back to centre),
  bold word-by-word captions burned in, Standard quality, the source frame rate, and no branding.
- **Two lists of the same thing.** The page must never again ask the owner to tick the same clips in two places.
- **Weakening a security property.** Text from the server is still set with `textContent` only. No inline
  script or style is added. Every request is still validated by the server as untrusted.

## 5. Approaches

| | Approach | Trade-off |
|---|---|---|
| **1 (chosen)** | **One new Make shorts tab, built from the existing finding and making code.** Only the page changes. | The biggest change to the page's layout, but no server work, and every existing feature stays reachable. |
| 2 | Keep three tabs, hide most settings, and add a "Make shorts from these" jump button | Least work, but the owner still moves between two tabs and two lists, which is the confusing part. |
| 3 | A new server job that finds moments, waits for picks, then renders | No visible difference from 1, plus a new job type, tests and a security review for nothing the owner would notice. |

The owner picked approach 1.

## 6. What's removed, and why

These controls are removed because each is a duplicate on the new path. Their code is removed too, not just hidden.

- **The AI Highlights and Auto Shorts tabs** are replaced by Make shorts.
- **Add selected to clip list.** Ticking a moment is now the selection.
- **Auto Shorts' own clip rows, "Use current marks" and its second "Select all".** Step 2 is the only list.
  To make a short from the current marks, use **Add to list** in Manual. That row then appears in step 2.
- **"Load into editor"** on a card. ▶ Play already loads the range and plays it.
- **The six-stage list** for finding moments. It becomes one bar and one status line, and the stage names
  still appear in that line.

The Manual clip list and **Export clips as ZIP** move inside the Manual tab, because they're about plain cuts.
The plain-export progress area stays outside both tabs, because **Save plain cut** on a card uses it too.

## 7. Behaviour in the edge cases

- **AI isn't set up** (no OpenRouter key, or AI is unavailable): **Find moments** is disabled. One line explains why
  and points to `docs/OPENROUTER_SETUP.md`, and says shorts can still be made from clips cut by hand.
- **Auto Shorts is unavailable** (`/api/shorts/options` answers `available: false`, because FFmpeg lacks
  `libx264`/VideoToolbox, `aac` or a required filter): **Make N shorts** is disabled with the server's message.
  Finding moments and **Save plain cut** still work.
- **FFmpeg has no `ass` filter** (`captions_available: false` while `available` is true): Make still works.
  Burned-in captions are the only thing missing, and that case is handled as described in §2 under "Captions
  never silently go missing". Shorts without captions, or with SRT files only, work as they do today.
  *(Decided after the builder check.)*
- **The cost estimate is over the limit:** the estimate line says so, and Find moments is disabled, as today.
- **No moments found:** step 2 says so and suggests changing "What should they be about?" or the lengths under
  More settings.
- **Find moments again:** the new moments replace the old ones, and their ticks are cleared. Shorts already made
  stay in step 3 until the next Make. Find moments is disabled while shorts are being made.
- **More than 20 ticked** (the render limit; possible with 10 moments plus hand-cut clips): Make is disabled, with
  "You can make up to 20 shorts at once."
- **Which moments start ticked:** the AI's moments start **ticked**, so the fast path is "look, untick any you
  don't want, Make". Hand-cut clips start **unticked**, because they may have been cut for a plain ZIP instead.
- **Reload, a closed video, or a different video:** as today. A running find or make is picked up again after a
  reload, ticks and card edits are remembered in `sessionStorage`, and everything resets when the video changes
  or closes.
- **Choosing a platform in More settings → Look** that isn't one of the three chips: the chips all show unselected,
  with "Other: <name>" next to them, and choosing a chip again overrides it.
- **Keyboard and screen readers:** the tabs keep today's keyboard behaviour. Each step heading is a real heading.
  Focus follows the progress area when a button disables itself, as today. Live-region messages are as today.
- **Phone (375 px), light and dark:** no horizontal scroll, and buttons at least 40 px tall, as in the earlier
  walkthroughs.

## 8. How it's built (decided)

- **The page:** `index.html` gets two tabs and one `make-panel`. Existing controls keep their element IDs wherever
  the control survives, so the logic in `ai.js` and `shorts.js` keeps working as they move into steps and More
  settings. `tests/unit/test_page.py` pins the new structure.
- **`ai.js` keeps finding moments** (questions 2 and 3, the estimate, the run, the moment cards and the
  transcript). **`shorts.js` keeps making shorts** (question 1, the Look, Captions, Branding, Presets and Preview
  settings, the render, and step 3).
- **The handoff between them is a small "picks" list on the studio bridge** (ranges with titles, kept in
  `sessionStorage`). `ai.js` writes it as the owner ticks moments and edits their times. `shorts.js` reads it,
  together with the ticked hand-cut clips, and sends `{start, end, title, cues?, manual?}` per clip in the render
  request. This replaces today's route through the clip list.
- **Tab handling** moves from three tabs to two, and the stored tab name migrates: a stored `ai` or `shorts`
  opens Make shorts.

## 9. Testing and checks

- **`tests/unit/test_page.py`:** the markup and copy tests for the new tab, steps, More settings groups, labels,
  live regions and routes are written first, and must fail before the page changes. The old AI Highlights and Auto
  Shorts markup tests are replaced, not deleted. The IDs that still exist stay pinned.
- **The full suite stays green**, with no server test changes expected.
- **A real-browser walkthrough** (Playwright with Chromium, SaveMe run natively, a simulated OpenRouter, as in the
  earlier phases), at 1200 px and at 375 px in dark mode, with no console errors. It covers:
  - the simple path: three questions → find → untick one → make → play → Save → Download all;
  - AI not set up, making shorts from hand-cut clips only, including the automatic transcription;
  - captions that can't be made (a video longer than a lowered `AI_MAX_SOURCE_MINUTES`), followed by
    **Make them without captions**;
  - Find again, a reload while shorts are being made, and Cancel during finding and during making;
  - a short's file name coming from its moment's title.
- **Docs:** the README's AI Highlights and Auto Shorts sections become one **Make shorts** section, with More
  settings described under it. AI_CLIPPING_DESIGN §11 and AUTO_SHORTS_DESIGN §10 each get a line pointing here.
  JOURNAL.md gets the session entry.

## 10. Left to the builder

- The exact new element IDs, the card and step layout, and the styling, within the existing Tailwind tokens.
- The name and shape of the picks API on the bridge.
- Which script owns the **Make N shorts** button. Exactly one does.
- The wording of status lines and messages not quoted above, in the page's existing plain style.
