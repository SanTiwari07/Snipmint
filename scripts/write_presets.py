"""Regenerates the built-in preset JSON files and the style schema.

Run from the repo root after editing PRESETS / FORMATS below:  python scripts/write_presets.py
(Contributors can also just add a JSON file by hand; this script is a convenience.)
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLES = ROOT / "snipmint/presets/styles"
FORMATS = ROOT / "snipmint/presets/formats"


def _colors(text="#FFFFFF", highlight="#FFE600", outline="#000000", shadow="#000000AA", box=None):
    return {"text": text, "highlight": highlight, "outline": outline, "shadow": shadow, "box": box}


def _style(sid, name, font, colors, outline, shadow, layout, highlight, anim, **extra):
    return {
        "id": sid, "name": name, "version": 1,
        "font": font, "colors": colors,
        "outline_width": outline, "shadow_offset": shadow,
        "layout": layout, "highlight": highlight,
        "animation": anim, "emphasis": extra.pop("emphasis", {"enabled": False}),
        **extra,
    }  # fmt: skip


def _layout(words, lines, pos):
    return {"words_per_chunk": words, "max_lines": lines, "position": pos, "offset_y": 0}


def _font(fid, size, upper, spacing=0):
    return {"id": fid, "size": size, "uppercase": upper, "letter_spacing": spacing}


def _hl(mode="word", scale=1.15, box=False, box_color="#FF3B6B", pad=14):
    return {"mode": mode, "scale": scale, "box": box, "box_color": box_color, "box_padding": pad}


PRESETS = [
    _style("bold-pop", "Bold Pop", _font("montserrat-black", 100, True), _colors(),
           6, 3, _layout(3, 2, "lower_middle"), _hl("word", 1.15), {"in": "pop", "duration_ms": 120}),
    _style("karaoke", "Karaoke Highlight", _font("montserrat-extrabold", 84, False),
           _colors(highlight="#3DFF9A"), 5, 2, _layout(6, 2, "lower_middle"),
           _hl("karaoke", 1.0), {"in": "fade", "duration_ms": 120}),
    _style("boxed", "Boxed", _font("poppins-bold", 80, False),
           _colors(highlight="#FFFFFF", shadow="#00000000"), 0, 0, _layout(3, 2, "lower_middle"),
           _hl("word", 1.0, True, "#FF3B6B", 9), {"in": "pop", "duration_ms": 120}),
    _style("one-word", "One Word", _font("anton-regular", 190, True, 2), _colors(), 9, 4,
           _layout(1, 1, "middle"), _hl("none", 1.0), {"in": "pop", "duration_ms": 100}),
    _style("minimal", "Minimal", _font("inter-bold", 66, False),
           _colors(highlight="#FFFFFF", shadow="#000000CC"), 2, 3, _layout(6, 2, "bottom"),
           _hl("none", 1.0), {"in": "fade", "duration_ms": 150}),
    _style("neon", "Neon", _font("rubik-bold", 90, True, 1),
           _colors(text="#E8FFFF", highlight="#FFF700", outline="#00E5FF", shadow="#00000000"),
           5, 0, _layout(3, 2, "lower_middle"), _hl("word", 1.1),
           {"in": "fade", "duration_ms": 120}, blur=3),
    _style("keyword-emphasis", "Keyword Emphasis", _font("poppins-bold", 78, False),
           _colors(highlight="#FFFFFF"), 5, 2, _layout(4, 2, "lower_middle"), _hl("none", 1.0),
           {"in": "pop", "duration_ms": 120},
           emphasis={"enabled": True, "auto": True, "color": "#FFE600", "scale": 1.3}),
    _style("subtitle-classic", "Subtitle Classic", _font("roboto-condensed-bold", 58, False),
           _colors(highlight="#FFFFFF", shadow="#00000000", box="#000000B3"), 10, 0,
           _layout(8, 2, "bottom"), _hl("none", 1.0), {"in": "none", "duration_ms": 0},
           border_style="box"),
]


def _format(fid, name, w, h, positions, max_w, safe, default_style, note):
    return {
        "id": fid, "name": name, "width": w, "height": h, "fps": 30,
        "positions": dict(zip(("top", "middle", "lower_middle", "bottom"), positions, strict=True)),
        "max_text_width": max_w,
        "safe_zone": dict(zip(("top", "bottom", "right"), safe, strict=True)),
        "default_style": default_style, "note": note,
    }  # fmt: skip


FORMAT_LIST = [
    _format("reels", "Reels / TikTok / Shorts", 1080, 1920, (0.17, 0.5, 0.62, 0.74), 0.84,
            (0.12, 0.20, 0.10), "bold-pop",
            "Captions stay clear of the platform buttons and description area."),
    _format("youtube", "YouTube", 1920, 1080, (0.12, 0.5, 0.78, 0.89), 0.70,
            (0.05, 0.08, 0.05), "subtitle-classic", "Standard 16:9 landscape video."),
    _format("square", "Instagram post (1:1)", 1080, 1080, (0.15, 0.5, 0.68, 0.84), 0.86,
            (0.06, 0.08, 0.05), "bold-pop", "Square 1:1."),
    _format("portrait", "Instagram portrait (4:5)", 1080, 1350, (0.15, 0.5, 0.66, 0.82), 0.86,
            (0.06, 0.10, 0.05), "bold-pop", "Portrait 4:5."),
    _format("audio", "Audio only", None, None, (0.17, 0.5, 0.62, 0.74), 0.84,
            (0, 0, 0), "bold-pop", "Voiceover, subtitles and timings only - no video."),
]


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    from snipmint.captions.styles import CaptionStyle, json_schema

    STYLES.mkdir(parents=True, exist_ok=True)
    FORMATS.mkdir(parents=True, exist_ok=True)
    for p in PRESETS:
        CaptionStyle.model_validate(p)  # fail loudly on typos
        _dump(STYLES / f"{p['id']}.json", p)
    for f in FORMAT_LIST:
        _dump(FORMATS / f"{f['id']}.json", f)
    _dump(STYLES / "schema.json", json_schema())
    print(f"wrote {len(PRESETS)} styles, {len(FORMAT_LIST)} formats and schema.json")


if __name__ == "__main__":
    main()
