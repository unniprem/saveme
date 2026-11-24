"""Auto Shorts caption styles and font presets, as data (docs/AUTO_SHORTS_DESIGN.md §6.4, §6.5).

Sizes, outlines and shadows are in pixels for a frame whose short side is 1080; app.captions scales
them to the real output. Every field can be overridden per request (app.render_spec validates the
ranges). Fonts are named by family; libass finds them through fontconfig and falls back per glyph.
The Docker image installs Roboto (Apache-2.0), DejaVu (Bitstream Vera licence) and Manjari (SIL OFL
1.1) from Debian; no font file is committed to this repository.
"""
from __future__ import annotations

from types import MappingProxyType

FONT_PRESETS = MappingProxyType({
    "sans": {"name": "Sans (Roboto)", "family": "Roboto", "licence": "Apache-2.0"},
    "heavy": {"name": "Heavy (Roboto Black)", "family": "Roboto Black", "licence": "Apache-2.0"},
    "condensed": {"name": "Condensed (Roboto Condensed)", "family": "Roboto Condensed", "licence": "Apache-2.0"},
    "serif": {"name": "Serif (DejaVu Serif)", "family": "DejaVu Serif", "licence": "Bitstream Vera"},
})
# Used for text that is mostly Malayalam, whatever the preset; libass shapes it with HarfBuzz.
MALAYALAM_FAMILY = "Manjari"
FALLBACK_FAMILY = "DejaVu Sans"

POSITIONS = ("top", "middle", "bottom")
HIGHLIGHT_MODES = ("none", "color", "box")

STYLES = MappingProxyType({
    "clean_minimal": {
        "name": "Clean Minimal", "font": "sans", "bold": False, "size": 54, "color": "#FFFFFF",
        "outline": 2.5, "outline_color": "#000000", "shadow": 1.5, "box": False, "box_color": "#000000",
        "box_opacity": 0.0, "uppercase": False, "highlight_mode": "none", "highlight": "#FFD400",
        "max_words": 8, "max_chars": 32, "max_lines": 2, "spacing": 0.0,
    },
    "bold_creator": {
        "name": "Bold Creator", "font": "heavy", "bold": True, "size": 86, "color": "#FFFFFF",
        "outline": 7.0, "outline_color": "#000000", "shadow": 3.0, "box": False, "box_color": "#000000",
        "box_opacity": 0.0, "uppercase": True, "highlight_mode": "color", "highlight": "#FFD400",
        "max_words": 3, "max_chars": 16, "max_lines": 2, "spacing": 0.5,
    },
    "karaoke": {
        "name": "Karaoke Highlight", "font": "sans", "bold": True, "size": 68, "color": "#FFFFFF",
        "outline": 4.5, "outline_color": "#101010", "shadow": 2.0, "box": False, "box_color": "#000000",
        "box_opacity": 0.0, "uppercase": False, "highlight_mode": "box", "highlight": "#7C3AED",
        "max_words": 6, "max_chars": 22, "max_lines": 2, "spacing": 0.0,
    },
    "podcast_classic": {
        "name": "Podcast Classic", "font": "sans", "bold": True, "size": 56, "color": "#FFFFFF",
        "outline": 0.0, "outline_color": "#000000", "shadow": 0.0, "box": True, "box_color": "#000000",
        "box_opacity": 0.65, "uppercase": False, "highlight_mode": "none", "highlight": "#FFD400",
        "max_words": 10, "max_chars": 30, "max_lines": 2, "spacing": 0.0,
    },
    "cinematic": {
        "name": "Cinematic", "font": "serif", "bold": False, "size": 50, "color": "#F5F0E6",
        "outline": 0.0, "outline_color": "#000000", "shadow": 2.5, "box": False, "box_color": "#000000",
        "box_opacity": 0.0, "uppercase": False, "highlight_mode": "none", "highlight": "#F5C451",
        "max_words": 10, "max_chars": 38, "max_lines": 2, "spacing": 1.0,
    },
    "high_contrast": {
        "name": "High Contrast", "font": "sans", "bold": True, "size": 62, "color": "#FFE600",
        "outline": 0.0, "outline_color": "#000000", "shadow": 0.0, "box": True, "box_color": "#000000",
        "box_opacity": 1.0, "uppercase": False, "highlight_mode": "none", "highlight": "#FFFFFF",
        "max_words": 7, "max_chars": 24, "max_lines": 2, "spacing": 0.0,
    },
})

# What a request may set for each field: (low, high) for numbers.
RANGES = MappingProxyType({
    "size": (16, 200), "outline": (0.0, 20.0), "shadow": (0.0, 20.0), "box_opacity": (0.0, 1.0),
    "max_words": (1, 15), "max_chars": (8, 60), "max_lines": (1, 3), "spacing": (0.0, 10.0),
})
