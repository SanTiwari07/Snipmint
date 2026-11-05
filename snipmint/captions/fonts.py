"""Font registry: bundled Google Fonts + user uploads (NarraReel NarraReel WHAT_TO_DO.md section 5.9.3).

libass finds fonts by *family name from the name table*, not by file name, so we read that
name from every font and the ASS writer uses it verbatim. Each render job gets its own
``fonts/`` folder with only the files it needs, so system fonts can never substitute silently.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import config
from ..errors import SnipmintError


class FontError(SnipmintError):
    """A font file is invalid or unknown."""


@dataclass(frozen=True)
class FontInfo:
    id: str
    family: str
    style: str
    weight: int
    path: Path
    ass_family: str  # exact family name libass should be asked for
    ass_bold: bool  # ASS "Bold" flag to use together with ass_family
    license: str = "unknown"
    tags: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ("latin",)
    user: bool = False
    source: str = ""
    license_file: Path | None = None
    _extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def display_name(self) -> str:
        return self.family if self.style in ("Regular", "") else f"{self.family} {self.style}"

    def public(self) -> dict:
        return {
            "id": self.id, "family": self.family, "style": self.style, "weight": self.weight,
            "license": self.license, "tags": list(self.tags), "scripts": list(self.scripts),
            "user": self.user, "name": self.display_name,
        }  # fmt: skip


def fonts_dir() -> Path:
    return config.assets_dir() / "fonts"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "font"


# ------------------------------------------------------------------ reading


def read_font_names(path: Path) -> tuple[str, str, int]:
    """(ass_family, ass_style, weight) from the font's name/OS2 tables."""
    from fontTools.ttLib import TTFont, TTLibError

    try:
        font = TTFont(str(path), lazy=True, fontNumber=0)
        names = font["name"]
        family = names.getDebugName(1) or names.getDebugName(16)
        style = names.getDebugName(2) or "Regular"
        weight = int(font["OS/2"].usWeightClass) if "OS/2" in font else 400
    except (TTLibError, KeyError, OSError, AttributeError) as exc:
        raise FontError(f"'{path.name}' is not a usable TrueType/OpenType font ({exc}).") from exc
    if not family:
        raise FontError(f"'{path.name}' has no family name.")
    return family, style, weight


@lru_cache(maxsize=1)
def _bundled() -> tuple[FontInfo, ...]:
    manifest = fonts_dir() / "manifest.json"
    if not manifest.is_file():
        return ()
    fonts = []
    for e in json.loads(manifest.read_text(encoding="utf-8")):
        path = fonts_dir() / e["file"]
        if not path.is_file():
            continue
        fonts.append(
            FontInfo(
                id=e["id"], family=e["family"], style=e["style"], weight=e["weight"], path=path,
                ass_family=e["ass_family"], ass_bold=e["ass_bold"], license=e["license"],
                tags=tuple(e.get("tags", [])), scripts=tuple(e.get("scripts", ["latin"])),
                source=e.get("source", ""),
                license_file=fonts_dir() / e["license_file"] if e.get("license_file") else None,
            )
        )  # fmt: skip
    return tuple(fonts)


def _user_fonts() -> list[FontInfo]:
    out = []
    for path in sorted(config.user_fonts_dir().glob("*")):
        if path.suffix.lower() not in (".ttf", ".otf"):
            continue
        try:
            family, style, weight = read_font_names(path)
        except FontError:
            continue
        out.append(
            FontInfo(
                id=f"user-{slug(path.stem)}", family=family, style=style, weight=weight, path=path,
                ass_family=family, ass_bold=style.lower().startswith("bold"),
                license="user-supplied", tags=("yours",), scripts=("latin",), user=True,
            )
        )  # fmt: skip
    return out


def list_fonts(tag: str | None = None) -> list[FontInfo]:
    fonts = [*_bundled(), *_user_fonts()]
    if tag:
        fonts = [f for f in fonts if tag in f.tags]
    return fonts


def get_font(font_id: str) -> FontInfo:
    for f in list_fonts():
        if f.id == font_id:
            return f
    known = ", ".join(f.id for f in _bundled()[:6])
    raise FontError(f"Unknown font '{font_id}'. Examples: {known} ... (run `snipmint fonts`).")


def add_user_font(data: bytes, filename: str) -> FontInfo:
    """Validate and store an uploaded .ttf/.otf. Validation is by decoding, not by extension."""
    ext = Path(filename).suffix.lower()
    if ext not in (".ttf", ".otf"):
        raise FontError("Only .ttf and .otf fonts can be added.")
    if len(data) > 10 * 1024 * 1024:
        raise FontError("That font file is larger than 10 MB.")
    try:
        ImageFont.truetype(io.BytesIO(data), 40).getname()
    except Exception as exc:
        raise FontError(f"'{filename}' could not be loaded as a font ({exc}).") from exc
    digest = hashlib.sha1(data).hexdigest()[:8]
    stem = f"{slug(Path(filename).stem)}-{digest}"
    target = config.user_fonts_dir() / f"{stem}{ext}"
    target.write_bytes(data)
    try:
        read_font_names(target)
    except FontError:
        target.unlink(missing_ok=True)
        raise
    return get_font(f"user-{stem}")


# ----------------------------------------------------------------- coverage


@lru_cache(maxsize=64)
def _cmap(path: str) -> frozenset[int]:
    from fontTools.ttLib import TTFont

    try:
        return frozenset(TTFont(path, lazy=True).getBestCmap().keys())
    except Exception:
        return frozenset()


def missing_glyphs(font: FontInfo, text: str) -> set[str]:
    """Characters of ``text`` the font cannot draw (whitespace and controls ignored)."""
    cmap = _cmap(str(font.path))
    if not cmap:
        return set()
    return {ch for ch in text if not ch.isspace() and ord(ch) >= 32 and ord(ch) not in cmap}


# ---------------------------------------------------------------- measuring


@lru_cache(maxsize=64)
def _ass_scale(path: str) -> float:
    """Pixels-per-ASS-size-unit: libass sizes fonts so that ascent+descent equals the size."""
    probe = ImageFont.truetype(path, 1000)
    asc, desc = probe.getmetrics()
    return 1000.0 / max(1, asc + desc)


def measure_font(font: FontInfo, ass_size: float) -> ImageFont.FreeTypeFont:
    """A Pillow font whose pixel metrics match libass at ASS font size ``ass_size``."""
    px = max(1.0, ass_size * _ass_scale(str(font.path)))
    # BASIC, not RAQM: Raqm is only present in some Pillow builds and its GPOS kerning shifts widths by
    # 1-4 %, so line breaks (and the golden .ass files) would differ between machines.
    return ImageFont.truetype(str(font.path), size=round(px * 4) / 4, layout_engine=ImageFont.Layout.BASIC)


def text_width(pil_font: ImageFont.FreeTypeFont, text: str, letter_spacing: float = 0.0) -> float:
    return pil_font.getlength(text) + letter_spacing * len(text)


# ------------------------------------------------------------------ preview


def preview_png(font: FontInfo, text: str = "THIS IS YOUR CAPTION", size: int = 64) -> bytes:
    """Sample text rendered in the font (white on dark), cached on disk."""
    key = hashlib.sha1(f"{font.path}:{font.path.stat().st_mtime_ns}:{text}:{size}".encode()).hexdigest()
    cache = config.cache_dir() / "font_previews"
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{key}.png"
    if cached.is_file():
        return cached.read_bytes()
    pil = ImageFont.truetype(str(font.path), size)
    bbox = pil.getbbox(text)
    w, h = bbox[2] - bbox[0] + 48, bbox[3] - bbox[1] + 48
    img = Image.new("RGB", (max(w, 64), max(h, 64)), (24, 24, 28))
    ImageDraw.Draw(img).text((24 - bbox[0], 24 - bbox[1]), text, font=pil, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    with contextlib.suppress(OSError):
        cached.write_bytes(buf.getvalue())
    return buf.getvalue()


# --------------------------------------------------------------- job folders


def copy_fonts_for_job(fonts: list[FontInfo], dest: Path) -> Path:
    """Copy only the needed font files into ``dest`` (used as libass ``fontsdir``)."""
    dest.mkdir(parents=True, exist_ok=True)
    for f in fonts:
        shutil.copyfile(f.path, dest / f.path.name)
    return dest
