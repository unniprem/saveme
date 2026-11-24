"""docs/RENDERING_PRESETS.md names every preset the code defines, with its values."""
from pathlib import Path

from app.caption_styles import FONT_PRESETS, STYLES
from app.encoders import QUALITIES
from app.shorts_templates import PLATFORMS, TEMPLATES

DOC = (Path(__file__).resolve().parents[2] / "docs" / "RENDERING_PRESETS.md").read_text(encoding="utf-8")


def test_every_preset_is_documented():
    for ids in (PLATFORMS, TEMPLATES, STYLES, QUALITIES, FONT_PRESETS):
        for key in ids:
            assert f"`{key}`" in DOC, key


def test_documented_values_match_the_code():
    for key, style in STYLES.items():
        row = next(line for line in DOC.splitlines() if line.startswith(f"| `{key}` |"))
        assert f"| {style['size']} |" in row and style["color"] in row, key
    for key, q in QUALITIES.items():
        row = next(line for line in DOC.splitlines() if line.startswith(f"| `{key}` |"))
        assert f"`{q.x264_preset}`" in row and f"| {q.crf} |" in row, key
    for key, p in PLATFORMS.items():
        row = next(line for line in DOC.splitlines() if line.startswith(f"| `{key}` |"))
        assert p["aspect"] in row, key
