"""Auto Shorts captions: words for a clip, cues, and the SRT and ASS files (docs/AUTO_SHORTS_DESIGN.md
§6). Pure: transcript and style in, text out.

- Words come from the transcript's word timestamps when it has them. Otherwise each segment's time is
  shared out by each word's spoken length (graphemes), which is an *estimate*. Either way only words
  inside the clip are kept, shifted so the clip starts at 0.
- Cues group words by the style's limits (words, characters per line × lines, 3.5 s), and break at
  pauses and sentence ends. Lines are balanced.
- ASS is written for libass at the output's own pixel size. All text is cleaned and escaped, so a
  transcript or a typed caption can never open an override block or form a `\\N`/`\\h` sequence.
"""
from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.caption_styles import FALLBACK_FAMILY, FONT_PRESETS, MALAYALAM_FAMILY
from app.transcript import Transcript, clean_text

MAX_CUE_SECONDS = 3.5
MIN_CUE_SECONDS = 0.6
PAUSE_SECONDS = 0.6
HOLD_SECONDS = 0.25  # a cue stays this long after its last word when nothing follows at once
_SENTENCE_END = (".", "?", "!", "…", "।", "॥")
# Average glyph width as a share of the font size, for fitting lines into the frame.
_WIDTH = {"latin": 0.56, "latin_upper": 0.68, "malayalam": 0.78}
_HEAVY = 1.12  # Roboto Black and bold runs are wider


@dataclass(frozen=True)
class CaptionWord:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class CaptionCue:
    start: float
    end: float
    words: tuple[CaptionWord, ...]
    lines: tuple[tuple[int, int], ...]  # word index ranges [a, b) of each line
    speaker: str | None = None

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def line_texts(self) -> list[str]:
        return [" ".join(w.text for w in self.words[a:b]) for a, b in self.lines]


# --- text measures ---------------------------------------------------------------------------

def graphemes(text: str) -> int:
    """Visible characters, counting a base letter and its combining marks (Malayalam vowel signs,
    viramas, accents) as one."""
    return sum(1 for ch in text if unicodedata.category(ch) not in ("Mn", "Mc", "Me", "Cf"))


def is_malayalam(text: str) -> bool:
    """True when most letters are Malayalam (U+0D00–U+0D7F)."""
    letters = [ch for ch in text if ch.isalpha() or unicodedata.category(ch) in ("Mn", "Mc")]
    if not letters:
        return False
    return sum(1 for ch in letters if "ഀ" <= ch <= "ൿ") * 2 > len(letters)


def escape_ass(text: str) -> str:
    """Text safe inside an ASS Dialogue line: cleaned to one line, then `\\`, `{` and `}` replaced so
    nothing can start an override block or an escape such as `\\N`."""
    return clean_text(text).replace("\\", "/").replace("{", "(").replace("}", ")")


# --- words for a clip --------------------------------------------------------------------------

def _estimate(start: float, end: float, text: str) -> list[CaptionWord]:
    """Share [start, end] among the words of `text` by their length (plus one for the gap)."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [graphemes(tok) + 1 for tok in tokens]
    total = sum(weights)
    out, t = [], start
    for tok, weight in zip(tokens, weights):
        span = (end - start) * weight / total
        out.append(CaptionWord(round(t, 3), round(t + span, 3), tok))
        t += span
    return out


def clip_words(transcript: Transcript, start: float, end: float) -> tuple[list[CaptionWord], str]:
    """The words spoken in [start, end) of the source, in clip time, and how they were timed:
    "words" (Whisper word timestamps), "estimated" (shared out within segments) or "mixed"."""
    words: list[CaptionWord] = []
    timed = estimated = 0
    for seg in transcript.segments:
        if seg.end <= start or seg.start >= end:
            continue
        if seg.words:
            timed += 1
            source = [CaptionWord(w.start, w.end, clean_text(w.text)) for w in seg.words]
        else:
            estimated += 1
            source = _estimate(seg.start, seg.end, seg.text)
        for w in source:
            mid = (w.start + w.end) / 2
            if not w.text or mid < start or mid >= end:
                continue
            s = max(0.0, w.start - start)
            e = min(end - start, max(w.end, w.start) - start)
            words.append(CaptionWord(round(s, 3), round(max(e, s), 3), w.text))
    timing = "words" if timed and not estimated else "estimated" if estimated and not timed else \
        "mixed" if timed else "none"
    return words, timing


# --- cues --------------------------------------------------------------------------------------

def balance_lines(words: Sequence[CaptionWord], max_lines: int, max_chars: int) -> tuple[tuple[int, int], ...]:
    """Split words into at most `max_lines` lines of similar width (graphemes), each ≤ max_chars when
    possible. Returns word index ranges."""
    n = len(words)
    prefix = [0]
    for w in words:
        prefix.append(prefix[-1] + graphemes(w.text))

    def width(a: int, b: int) -> int:  # O(1): the graphemes of words a..b-1 plus the spaces between them
        return prefix[b] - prefix[a] + max(0, b - a - 1)

    if n <= 1 or max_lines == 1 or width(0, n) <= max_chars:
        return ((0, n),)
    best, best_score = ((0, n),), math.inf
    if max_lines >= 2:
        for i in range(1, n):
            score = max(width(0, i), width(i, n))
            if score < best_score:
                best, best_score = ((0, i), (i, n)), score
    if max_lines >= 3 and best_score > max_chars:
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                score = max(width(0, i), width(i, j), width(j, n))
                if score < best_score:
                    best, best_score = ((0, i), (i, j), (j, n)), score
    return best


def segment(words: Sequence[CaptionWord], *, max_words: int, max_chars: int, max_lines: int,
            max_seconds: float = MAX_CUE_SECONDS, clip_seconds: float | None = None) -> list[CaptionCue]:
    """Group words into cues within the limits, breaking at pauses and sentence ends, then give each
    cue its lines and its display time (at least MIN_CUE_SECONDS when the next cue allows, a short
    hold after the last word, never overlapping the next cue, never past the clip)."""
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    room = max_chars * max_lines
    for word in words:
        if current:
            text_len = graphemes(" ".join(w.text for w in current + [word]))
            if (len(current) >= max_words or text_len > room or word.start - current[-1].end >= PAUSE_SECONDS
                    or word.end - current[0].start > max_seconds
                    or current[-1].text.endswith(_SENTENCE_END)):
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    cues: list[CaptionCue] = []
    for i, group in enumerate(groups):
        start = group[0].start
        next_start = groups[i + 1][0].start if i + 1 < len(groups) else math.inf
        end = max(group[-1].end + HOLD_SECONDS, start + MIN_CUE_SECONDS)
        end = min(end, next_start)
        if clip_seconds is not None:
            end = min(end, clip_seconds)
        end = max(end, min(group[-1].end, next_start))
        if end <= start:
            end = start + 0.01
        cues.append(CaptionCue(round(start, 3), round(end, 3), tuple(group),
                               balance_lines(group, max_lines, max_chars)))
    return cues


def cues_from_edits(edits: Iterable, *, max_lines: int, max_chars: int) -> list[CaptionCue]:
    """Cues the user edited ({start, end, text, speaker}, already validated), with word times shared
    out inside each cue (for highlighting)."""
    out = []
    for cue in edits:
        words = tuple(_estimate(cue.start, cue.end, cue.text))
        if words:
            out.append(CaptionCue(round(cue.start, 3), round(cue.end, 3), words,
                                  balance_lines(words, max_lines, max_chars), cue.speaker))
    return out


def fit_chars(style: dict, width: int, height: int, safe: dict, *, malayalam: bool = False) -> int:
    """The most characters a line may hold to fit the frame at this style's size: the style's limit,
    lowered when the estimated glyph width wouldn't fit between the safe margins."""
    scale = min(width, height) / 1080
    size = style["size"] * scale
    kind = "malayalam" if malayalam else "latin_upper" if style.get("uppercase") else "latin"
    glyph = size * _WIDTH[kind] * (_HEAVY if style.get("bold") or style.get("font") == "heavy" else 1.0)
    pad = 2 * (style.get("outline", 0) * scale + (14 * scale if style.get("box") else 0))
    room = width * (1 - safe.get("left", 0.05) - safe.get("right", 0.05)) - pad
    return max(6, min(style["max_chars"], int(room / max(glyph, 1))))


# --- SRT ---------------------------------------------------------------------------------------

def _srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: Sequence[CaptionCue], *, uppercase: bool = False) -> str:
    blocks = []
    for n, cue in enumerate(cues, 1):
        lines = cue.line_texts()
        if uppercase:
            lines = [line.upper() for line in lines]
        if cue.speaker:
            lines[0] = f"{cue.speaker}: {lines[0]}"
        blocks.append(f"{n}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n" + "\n".join(lines) + "\n")
    return "\n".join(blocks)


# --- ASS ---------------------------------------------------------------------------------------

def ass_color(hex_rgb: str, alpha: float = 0.0) -> str:
    """#RRGGBB (and a transparency 0–1) as ASS &HAABBGGRR."""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    a = max(0, min(255, round(alpha * 255)))
    return f"&H{a:02X}{b}{g}{r}".upper()


def tag_color(hex_rgb: str) -> str:
    """#RRGGBB as an override tag's colour, &HBBGGRR&."""
    return f"&H{hex_rgb[5:7]}{hex_rgb[3:5]}{hex_rgb[1:3]}&".upper()


def _ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rest = divmod(cs, 360000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _n(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class AssPlan:
    width: int
    height: int
    style: dict  # a resolved caption style (app.render_spec.resolve's captions)
    safe: dict
    enabled: bool = True  # captions burned in; title bar and watermark don't depend on it
    title: str | None = None  # a title bar's text
    brand_color: str | None = None
    watermark: str = ""
    watermark_position: str = "bottom-right"
    duration: float = 0.0
    split_seam: float | None = None  # a split layout's seam (share of the height)


def _family(style: dict) -> str:
    preset = FONT_PRESETS.get(style.get("font", "sans"), FONT_PRESETS["sans"])
    return preset["family"]


def _position(plan: AssPlan) -> tuple[int, float, float]:
    """(alignment, x, y) for the caption block from the style's position and offset, inside the safe
    area. Offsets are shares of the height (negative moves up)."""
    w, h, safe, style = plan.width, plan.height, plan.safe, plan.style
    x = w * (safe.get("left", 0.05) + (1 - safe.get("left", 0.05) - safe.get("right", 0.05)) / 2)
    offset = style.get("offset", 0.0) * h
    position = style.get("position", "bottom")
    top, bottom = h * safe.get("top", 0.05), h * (1 - safe.get("bottom", 0.08))
    if position == "top":
        return 8, x, min(max(top, top + offset), bottom)
    if position == "middle":
        seam = plan.split_seam if plan.split_seam is not None else 0.5
        return 5, x, min(max(top, h * seam + offset), bottom)
    return 2, x, max(min(bottom, bottom + offset), top)


def _cue_text(cue: CaptionCue, style: dict, *, active: int | None, primary: str) -> str:
    """One cue's ASS text, its lines joined with \\N, the active word (if any) highlighted."""
    mode = style.get("highlight_mode", "none")
    highlight = tag_color(style.get("highlight", "#FFD400"))
    scale_outline = style.get("_outline_px", 0.0)
    parts = []
    for a, b in cue.lines:
        words = []
        for i in range(a, b):
            text = escape_ass(cue.words[i].text)
            if style.get("uppercase"):
                text = text.upper()
            if active == i and mode == "color":
                text = f"{{\\c{highlight}}}{text}{{\\c{primary}}}"
            elif active == i and mode == "box":
                pad = max(4.0, scale_outline + 6)
                text = (f"{{\\3c{highlight}\\bord{_n(pad)}\\shad0}}{text}"
                        f"{{\\3c{style['_outline_color']}\\bord{_n(scale_outline)}\\shad{_n(style['_shadow_px'])}}}")
            words.append(text)
        parts.append(" ".join(words))
    text = "\\N".join(parts)
    if cue.speaker:
        text = f"{{\\c{highlight}}}{escape_ass(cue.speaker)}:{{\\c{primary}}} " + text
    return text


def to_ass(cues: Sequence[CaptionCue], plan: AssPlan) -> str:
    """The ASS file: one Caption style, the cues (one event per active word when highlighting),
    and the optional title bar and watermark."""
    w, h, style = plan.width, plan.height, dict(plan.style)
    scale = min(w, h) / 1080
    size = style["size"] * scale
    outline = style.get("outline", 0.0) * scale
    shadow = style.get("shadow", 0.0) * scale
    box = bool(style.get("box"))
    primary = ass_color(style["color"])
    outline_color = ass_color(style["box_color"], 1 - style.get("box_opacity", 1.0)) if box \
        else ass_color(style.get("outline_color", "#000000"))
    back = ass_color("#000000", 0.45)
    style["_outline_px"], style["_shadow_px"] = outline, shadow
    style["_outline_color"] = tag_color(style["box_color"] if box else style.get("outline_color", "#000000"))
    border_style = 3 if box else 1
    border = 14 * scale if box else outline
    bold = -1 if style.get("bold") else 0
    family = _family(style)
    header = [
        "[Script Info]", "; Written by SaveMe Auto Shorts", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{family},{_n(size)},{primary},{primary},{outline_color},{back},{bold},0,0,0,100,100,"
        f"{_n(style.get('spacing', 0.0) * scale)},0,{border_style},{_n(border)},{_n(shadow)},2,0,0,0,1",
        f"Style: Overlay,{FONT_PRESETS['sans']['family']},{_n(30 * scale)},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    end_all = max([plan.duration, *(c.end for c in cues)]) if (cues or plan.duration) else 0.0
    if plan.title:
        events += _title_bar(plan, end_all, scale)
    if plan.watermark:
        events.append(_watermark(plan, end_all, scale))
    if plan.enabled:
        align, x, y = _position(plan)
        pos = f"{{\\an{align}\\pos({_n(x)},{_n(y)})}}"
        for cue in cues:
            font = f"{{\\fn{MALAYALAM_FAMILY}}}" if is_malayalam(cue.text) else ""
            highlighting = style.get("highlight_mode", "none") != "none" and len(cue.words) > 1
            if not highlighting:
                events.append(f"Dialogue: 2,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,"
                              f"{pos}{font}{_cue_text(cue, style, active=None, primary=tag_color(style['color']))}")
                continue
            made = 0
            for i, word in enumerate(cue.words):
                start = cue.start if i == 0 else word.start
                end = cue.words[i + 1].start if i + 1 < len(cue.words) else cue.end
                if end - start < 0.005:
                    continue
                events.append(f"Dialogue: 2,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,"
                              f"{pos}{font}{_cue_text(cue, style, active=i, primary=tag_color(style['color']))}")
                made += 1
            if not made:  # every word too short to highlight on its own: show the cue plainly
                events.append(f"Dialogue: 2,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,"
                              f"{pos}{font}{_cue_text(cue, style, active=None, primary=tag_color(style['color']))}")
    return "\n".join(header + events) + "\n"


def _title_bar(plan: AssPlan, end: float, scale: float) -> list[str]:
    """A brand-colour band across the top of the safe area with the clip's title, for the whole clip."""
    w, h = plan.width, plan.height
    top = h * plan.safe.get("top", 0.05)
    band = round(h * 0.085 if h >= w else h * 0.11)
    color = tag_color(plan.brand_color or "#E5484D")
    title = escape_ass(plan.title or "")
    fs = 40 * scale
    max_chars = max(10, int(w * 0.9 / (fs * 0.6)))
    if len(title) > max_chars * 2:
        title = title[:max_chars * 2 - 1].rstrip() + "…"
    words = [CaptionWord(0, 0, t) for t in title.split()]
    lines = balance_lines(words, 2, max_chars) if words else ()
    text = "\\N".join(" ".join(x.text for x in words[a:b]) for a, b in lines)
    t0, t1 = _ass_time(0), _ass_time(end)
    shape = f"m 0 0 l {w} 0 {w} {band} 0 {band}"
    font = f"\\fn{MALAYALAM_FAMILY}" if is_malayalam(title) else ""
    return [
        f"Dialogue: 0,{t0},{t1},Overlay,,0,0,0,,{{\\an7\\pos(0,{_n(top)})\\p1\\bord0\\shad0\\c{color}}}{shape}{{\\p0}}",
        f"Dialogue: 1,{t0},{t1},Overlay,,0,0,0,,{{\\an5\\pos({_n(w / 2)},{_n(top + band / 2)})\\fs{_n(fs)}"
        f"\\bord0\\shad0{font}}}{text}",
    ]


def _watermark(plan: AssPlan, end: float, scale: float) -> str:
    w, h = plan.width, plan.height
    corner = plan.watermark_position
    margin_x = w * max(plan.safe.get("left", 0.05), plan.safe.get("right", 0.05)) * 0.6 + 8
    margin_y = h * 0.03
    x = margin_x if corner.endswith("left") else w - margin_x
    if corner.startswith("top"):
        y, align = h * plan.safe.get("top", 0.05) * 0.5 + margin_y, 7 if corner.endswith("left") else 9
    else:
        y, align = h - margin_y, 1 if corner.endswith("left") else 3
    text = escape_ass(plan.watermark)
    return (f"Dialogue: 3,{_ass_time(0)},{_ass_time(end)},Overlay,,0,0,0,,{{\\an{align}\\pos({_n(x)},{_n(y)})"
            f"\\fs{_n(28 * scale)}\\alpha&H50&\\bord{_n(1.5 * scale)}\\3c&H000000&}}{text}")


def fallback_fonts() -> list[str]:
    return [FALLBACK_FAMILY]
