"""Colour conversion for ASS subtitles (the classic bug source: ASS uses BGR and inverted alpha)."""

from __future__ import annotations

import re

from ..errors import SnipmintError


class ColourError(SnipmintError, ValueError):
    """Not a valid hex colour (a ValueError so pydantic reports it as a validation error)."""


_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def parse_hex(value: str) -> tuple[int, int, int, int]:
    """``#RGB``, ``#RRGGBB`` or ``#RRGGBBAA`` -> (r, g, b, opacity 0-255; 255 = opaque)."""
    m = _HEX.match(value.strip()) if isinstance(value, str) else None
    if not m:
        raise ColourError(f"'{value}' is not a colour. Use a hex value like #FFE600.")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    a = int(h[6:8], 16) if len(h) == 8 else 255
    return r, g, b, a


def ass_style_color(value: str) -> str:
    """``&HAABBGGRR`` for Style lines. AA is transparency (00 = opaque)."""
    r, g, b, a = parse_hex(value)
    return f"&H{255 - a:02X}{b:02X}{g:02X}{r:02X}"


def ass_tag_color(value: str) -> str:
    """``&HBBGGRR&`` for colour override tags such as c and 3c (alpha is separate)."""
    r, g, b, _ = parse_hex(value)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def ass_tag_alpha(value: str) -> str:
    """``&HAA&`` for alpha override tags (00 = opaque)."""
    return f"&H{255 - parse_hex(value)[3]:02X}&"
