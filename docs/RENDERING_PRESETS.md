# Auto Shorts rendering presets

Every preset Auto Shorts offers, as the code defines it (`app/shorts_templates.py`, `app/caption_styles.py`,
`app/encoders.py`). `tests/unit/test_presets_doc.py` checks that this page names every preset. A render
request picks a **template** and a **platform** and may override any field. The server resolves
template ⊕ platform ⊕ overrides (`app/render_spec.py`) and refuses anything out of range. Design: [AUTO_SHORTS_DESIGN.md](AUTO_SHORTS_DESIGN.md).

## Platforms

The safe area is the share of each edge that the platform's own buttons and text usually cover. These are approximate. Captions and
the watermark stay inside it.

| Id | Name | Aspect | Size | Safe area (top / bottom / left / right) | Note |
| --- | --- | --- | --- | --- | --- |
| `youtube_shorts` | YouTube Shorts | 9:16 | 1080×1920 | 8% / 20% / 5% / 12% | Up to 3 minutes. |
| `instagram_reels` | Instagram Reels | 9:16 | 1080×1920 | 10% / 22% / 5% / 12% | Up to 3 minutes. |
| `tiktok` | TikTok | 9:16 | 1080×1920 | 10% / 22% / 5% / 14% | Up to 10 minutes when uploaded. |
| `instagram_feed` | Instagram / Facebook feed | 4:5 | 1080×1350 | 5% / 8% / 5% / 5% |  |
| `square` | Square | 1:1 | 1080×1080 | 5% / 8% / 5% / 5% |  |
| `landscape` | Landscape (YouTube, X) | 16:9 | 1920×1080 | 5% / 8% / 5% / 5% |  |
| `source` | Source aspect | source | the video's own (long side ≤ 1920) | 5% / 8% / 5% / 5% |  |

A custom size (`width`, `height`) may be any even pair from 240 to 3840, at most 2160 × 3840 pixels in all.

## Quality

Every output is H.264 High, `yuv420p`, with BT.709 tags and a 2-second GOP. Audio is AAC LC at 48 kHz, stereo. The
file is MP4 with `+faststart`. The bit-rate cap is `bpp × width × height × fps`. For x264 it is `-maxrate` (with a
buffer of twice that); VideoToolbox, which has no CRF, uses `hw bpp` as its target.

| Id | Name | x264 preset | CRF | Cap (bits/pixel/frame) | VideoToolbox target | Audio | Size |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `draft` | Draft (fast, half size) | `ultrafast` | 30 | 0.1 | 0.06 | 96 kb/s | half |
| `standard` | Standard | `veryfast` | 21 | 0.2 | 0.16 | 160 kb/s | full |
| `high` | High | `medium` | 18 | 0.3 | 0.25 | 192 kb/s | full |

Measured on an Apple M2 (8 GB) for 30 s of 1080×1920 at 30 fps, 2 threads: `ultrafast` took 2.8 s (162 MB),
`veryfast` 5.9 s (308 MB), `medium` 11.1 s (508 MB), and VideoToolbox 4.8 s (183 MB). Frame rates: `source`, `24`, `25`, `30`, `50`, `60` (`source` is the video's own, at most 60).

## Templates

| Id | Name | Aspect | Framing | Fallback | Captions | Position | Branding slots |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `talking_head` | Talking head | 9:16 | `face` margin 0.08 | `center` | `bold_creator` | bottom (-0.06) | logo top-right |
| `interview` | Two-person interview | 9:16 | `speaker` margin 0.08 | `center` | `podcast_classic` | bottom (+0.00) | logo top-right |
| `podcast_split` | Podcast split-screen | 9:16 | `split` margin 0.06 | `center` | `podcast_classic` | middle (+0.00) | logo top-left |
| `educational` | Educational explanation | 9:16 | `blur` frame at 0.42 | `blur` | `clean_minimal` | bottom (-0.04) | logo top-right |
| `news_commentary` | News commentary | 9:16 | `blur` frame at 0.45 | `blur` | `high_contrast` | bottom (-0.02) | logo bottom-left, title bar |
| `full_frame_blur` | Full frame, blurred background | 9:16 | `blur` frame at 0.5 | `blur` | `clean_minimal` | bottom (+0.00) | logo top-right |

- **Talking head** (`talking_head`): One person to camera. Follows their face; big word-by-word captions.
- **Two-person interview** (`interview`): Two people in one shot. Frames whoever is talking (best effort).
- **Podcast split-screen** (`podcast_split`): Two speakers stacked top and bottom, captions on the seam.
- **Educational explanation** (`educational`): Slides or screen stay whole over a blurred background; clean captions below.
- **News commentary** (`news_commentary`): The whole frame under a headline bar, with high-contrast captions.
- **Full frame, blurred background** (`full_frame_blur`): Nothing cropped: the frame centred over a blurred copy of itself.

## Framing modes

| Mode | What it does | Parameters |
| --- | --- | --- |
| `center` | A still window of the output's aspect at the frame's centre, or at the manual position. | `manual: {x, y, zoom}` (0–1, 0–1, 1–3) |
| `face` | Follows the faces: all of them while they fit, else the main one. Eases at most 0.6 window-widths a second, and jumps only at scene cuts. | `margin` 0–0.25, `deadzone` 0–0.8, `fallback` |
| `speaker` | Like `face`, but while someone speaks it prefers the face whose mouth moves most (best effort, never voice identification). | as `face` |
| `split` | Two panels stacked, each following one of the two main faces (left person on top). With fewer than two, the left and right halves. | `split_ratio` 0.3–0.7 |
| `blur` | The whole frame over a blurred, darkened copy of itself. | `fg_position` 0–1 |
| `original` | The whole frame over black or the brand colour. | `fg_position` 0–1 |

## Caption styles

Sizes, outlines and shadows are pixels at a short side of 1080 and scale with the output. `max_chars` is
lowered automatically when the font size wouldn't fit between the safe margins.

| Id | Name | Font | Size | Colour | Outline | Shadow | Box | Capitals | Highlight | Words / chars / lines |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `clean_minimal` | Clean Minimal | sans | 54 | #FFFFFF | 2.5 | 1.5 | no | no | none | 8 / 32 / 2 |
| `bold_creator` | Bold Creator | heavy bold | 86 | #FFFFFF | 7.0 | 3.0 | no | yes | color #FFD400 | 3 / 16 / 2 |
| `karaoke` | Karaoke Highlight | sans bold | 68 | #FFFFFF | 4.5 | 2.0 | no | no | box #7C3AED | 6 / 22 / 2 |
| `podcast_classic` | Podcast Classic | sans bold | 56 | #FFFFFF | 0.0 | 0.0 | #000000 at 65% | no | none | 10 / 30 / 2 |
| `cinematic` | Cinematic | serif | 50 | #F5F0E6 | 0.0 | 2.5 | no | no | none | 10 / 38 / 2 |
| `high_contrast` | High Contrast | sans bold | 62 | #FFE600 | 0.0 | 0.0 | #000000 at 100% | no | none | 7 / 24 / 2 |

Overridable per request: `font`, `bold`, `size` (16–200), `color`, `highlight`, `highlight_mode` (none, color, box), `outline` (0–20), `shadow` (0–20), `box`, `box_color`, `box_opacity`, `uppercase`, `max_words` (1–15), `max_chars` (8–60), `max_lines` (1–3), `position` (top, middle, bottom), `offset` (−0.4–0.4 of the height), plus `enabled`, `burn` and `export` (`srt`, `ass`).

Cues last at most 3.5 s. A cue breaks at a pause of 0.6 s or more and after a sentence end, shows for at least 0.6 s when the
next cue allows, and holds 0.25 s after its last word.

## Fonts

| Id | Family | Licence |
| --- | --- | --- |
| `sans` | Roboto | Apache-2.0 |
| `heavy` | Roboto Black | Apache-2.0 |
| `condensed` | Roboto Condensed | Apache-2.0 |
| `serif` | DejaVu Serif | Bitstream Vera |
| (Malayalam text) | Manjari | SIL OFL 1.1 |

The Docker image installs these from Debian (`fonts-roboto-unhinted`, `fonts-dejavu-core`, `fonts-smc-manjari`). A
native run uses whatever fontconfig or CoreText finds, falling back per glyph (on macOS, Malayalam Sangam MN). No font
file is committed, and no commercial font is used. `RENDER_FONTS_DIR` adds a folder of your own fonts.

## Branding

Everything is off by default, and SaveMe never adds its own watermark.

| Field | Values |
| --- | --- |
| `logo` | an uploaded PNG (≤ 2 MB, ≤ 2048 px a side, not interlaced), decoded by SaveMe, not FFmpeg |
| `logo_position` | top-left, top-right, bottom-left, bottom-right (the template's by default) |
| `logo_scale` | 0.05–0.4 of the width (0.15) |
| `logo_opacity` | 0.1–1 (0.9) |
| `watermark` | text, ≤ 60 characters, drawn by libass in `watermark_position` (bottom-right) |
| `brand_color` | `#RRGGBB`: padding, title bar, and the default word-highlight colour |
| `title_bar` | the clip's title in a brand-colour bar at the top (news template) |
| `intro`, `outro` | uploaded videos (≤ 100 MB, ≤ 15 s), fitted to the canvas, with silent audio added if they have none |

## Presets in the browser

Saved presets are stored in `localStorage` under `saveme.shorts.presets.v1` (at most 20). Each is `{name, spec, logo,
bumpers, saved}`, with the logo as a PNG data URL of at most 512 KB. Intro and outro videos aren't saved. Export writes
`{"saveme_presets": 1, "presets": [...]}`. Import checks every spec with `POST /api/shorts/validate`, which answers
`{"valid": false, "error"}` for one it refuses.
