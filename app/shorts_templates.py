"""Auto Shorts presets as data: output platforms, video templates, quality presets
(docs/AUTO_SHORTS_DESIGN.md §4.1, §7; every value is listed in docs/RENDERING_PRESETS.md).

Templates are declarative: each names an aspect, a layout (mode, fallback, margins), a caption
style and position, and branding slots. A render request names a template and a platform and may
override any field; app.render_spec resolves and validates the result. Nothing here is code.
"""
from __future__ import annotations

from types import MappingProxyType

# Output platforms: aspect, default size, the share of each edge the platform's own buttons and
# captions cover (approximate; captions and logos are kept clear of it), and a length note.
PLATFORMS = MappingProxyType({
    "youtube_shorts": {"name": "YouTube Shorts", "aspect": "9:16", "width": 1080, "height": 1920,
                       "safe": {"top": 0.08, "bottom": 0.20, "left": 0.05, "right": 0.12},
                       "max_seconds": 180, "note": "Up to 3 minutes."},
    "instagram_reels": {"name": "Instagram Reels", "aspect": "9:16", "width": 1080, "height": 1920,
                        "safe": {"top": 0.10, "bottom": 0.22, "left": 0.05, "right": 0.12},
                        "max_seconds": 180, "note": "Up to 3 minutes."},
    "tiktok": {"name": "TikTok", "aspect": "9:16", "width": 1080, "height": 1920,
               "safe": {"top": 0.10, "bottom": 0.22, "left": 0.05, "right": 0.14},
               "max_seconds": 600, "note": "Up to 10 minutes when uploaded."},
    "instagram_feed": {"name": "Instagram / Facebook feed", "aspect": "4:5", "width": 1080, "height": 1350,
                       "safe": {"top": 0.05, "bottom": 0.08, "left": 0.05, "right": 0.05},
                       "max_seconds": None, "note": None},
    "square": {"name": "Square", "aspect": "1:1", "width": 1080, "height": 1080,
               "safe": {"top": 0.05, "bottom": 0.08, "left": 0.05, "right": 0.05},
               "max_seconds": None, "note": None},
    "landscape": {"name": "Landscape (YouTube, X)", "aspect": "16:9", "width": 1920, "height": 1080,
                  "safe": {"top": 0.05, "bottom": 0.08, "left": 0.05, "right": 0.05},
                  "max_seconds": None, "note": None},
    "source": {"name": "Source aspect", "aspect": "source", "width": None, "height": None,
               "safe": {"top": 0.05, "bottom": 0.08, "left": 0.05, "right": 0.05},
               "max_seconds": None, "note": None},
})

QUALITY_PRESETS = MappingProxyType({
    "draft": {"name": "Draft (fast, half size)", "description": "For checking framing and captions."},
    "standard": {"name": "Standard", "description": "Good quality at a small size. The default."},
    "high": {"name": "High", "description": "Best quality; about twice as slow."},
})

FPS_CHOICES = ("source", 24, 25, 30, 50, 60)

# Video templates. layout: mode, fallback, margin, deadzone, fg_position (blur/original),
# split_ratio. captions: style, position, offset. branding: logo corner and title bar.
TEMPLATES = MappingProxyType({
    "talking_head": {
        "name": "Talking head",
        "description": "One person to camera. Follows their face; big word-by-word captions.",
        "aspect": "9:16",
        "layout": {"mode": "face", "fallback": "center", "margin": 0.08, "deadzone": 0.3},
        "captions": {"style": "bold_creator", "position": "bottom", "offset": -0.06},
        "branding": {"logo_position": "top-right", "title_bar": False},
    },
    "interview": {
        "name": "Two-person interview",
        "description": "Two people in one shot. Frames whoever is talking (best effort).",
        "aspect": "9:16",
        "layout": {"mode": "speaker", "fallback": "center", "margin": 0.08, "deadzone": 0.25},
        "captions": {"style": "podcast_classic", "position": "bottom", "offset": 0.0},
        "branding": {"logo_position": "top-right", "title_bar": False},
    },
    "podcast_split": {
        "name": "Podcast split-screen",
        "description": "Two speakers stacked top and bottom, captions on the seam.",
        "aspect": "9:16",
        "layout": {"mode": "split", "fallback": "center", "margin": 0.06, "deadzone": 0.3, "split_ratio": 0.5},
        "captions": {"style": "podcast_classic", "position": "middle", "offset": 0.0},
        "branding": {"logo_position": "top-left", "title_bar": False},
    },
    "educational": {
        "name": "Educational explanation",
        "description": "Slides or screen stay whole over a blurred background; clean captions below.",
        "aspect": "9:16",
        "layout": {"mode": "blur", "fallback": "blur", "fg_position": 0.42},
        "captions": {"style": "clean_minimal", "position": "bottom", "offset": -0.04},
        "branding": {"logo_position": "top-right", "title_bar": False},
    },
    "news_commentary": {
        "name": "News commentary",
        "description": "The whole frame under a headline bar, with high-contrast captions.",
        "aspect": "9:16",
        "layout": {"mode": "blur", "fallback": "blur", "fg_position": 0.45},
        "captions": {"style": "high_contrast", "position": "bottom", "offset": -0.02},
        "branding": {"logo_position": "bottom-left", "title_bar": True},
    },
    "full_frame_blur": {
        "name": "Full frame, blurred background",
        "description": "Nothing cropped: the frame centred over a blurred copy of itself.",
        "aspect": "9:16",
        "layout": {"mode": "blur", "fallback": "blur", "fg_position": 0.5},
        "captions": {"style": "clean_minimal", "position": "bottom", "offset": 0.0},
        "branding": {"logo_position": "top-right", "title_bar": False},
    },
})

LAYOUT_DEFAULTS = MappingProxyType({"mode": "center", "fallback": "center", "margin": 0.08, "deadzone": 0.3,
                                    "fg_position": 0.5, "split_ratio": 0.5})
