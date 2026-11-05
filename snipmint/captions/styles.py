"""Caption style model and presets (NarraReel NarraReel WHAT_TO_DO.md section 5.9.2)."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import config
from ..errors import SnipmintError
from .colors import parse_hex


class StyleError(SnipmintError):
    """Unknown preset or invalid style values."""


class FontSpec(BaseModel):
    id: str = "montserrat-black"
    size: int = Field(88, ge=20, le=300)
    uppercase: bool = True
    letter_spacing: float = Field(0.0, ge=-5.0, le=20.0)


class Colors(BaseModel):
    text: str = "#FFFFFF"
    highlight: str = "#FFE600"
    outline: str = "#000000"
    shadow: str = "#000000AA"
    box: str | None = None  # background box behind each line (BorderStyle 3)

    @field_validator("text", "highlight", "outline", "shadow", "box")
    @classmethod
    def _valid_hex(cls, v: str | None) -> str | None:
        if v is not None:
            parse_hex(v)
        return v


class Layout(BaseModel):
    words_per_chunk: int = Field(3, ge=1, le=8)
    max_lines: int = Field(2, ge=1, le=2)
    position: Literal["top", "middle", "lower_middle", "bottom"] = "lower_middle"
    offset_y: int = Field(0, ge=-1000, le=1000)
    align_x: Literal["left", "center", "right"] = "center"  # which side of the frame the text hugs
    offset_x: int = Field(0, ge=-1000, le=1000)  # nudge right (+) or left (-), in video pixels


class Highlight(BaseModel):
    mode: Literal["word", "karaoke", "none"] = "word"
    scale: float = Field(1.15, ge=0.5, le=2.0)
    box: bool = False  # draw a rounded colour box behind the active word
    box_color: str = "#FF3B6B"
    box_padding: int = Field(14, ge=0, le=60)

    @field_validator("box_color")
    @classmethod
    def _valid_hex(cls, v: str) -> str:
        parse_hex(v)
        return v


class Animation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    in_: Literal["none", "pop", "fade", "bounce"] = Field("pop", alias="in")
    duration_ms: int = Field(120, ge=0, le=1000)


class Emphasis(BaseModel):
    enabled: bool = False
    auto: bool = False  # numbers, long words and ALL-CAPS are emphasised without asterisks
    color: str = "#00E5FF"
    scale: float = Field(1.25, ge=0.5, le=2.0)

    @field_validator("color")
    @classmethod
    def _valid_hex(cls, v: str) -> str:
        parse_hex(v)
        return v


class CaptionStyle(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = "custom"
    name: str = "Custom"
    version: int = 1
    font: FontSpec = Field(default_factory=FontSpec)
    colors: Colors = Field(default_factory=Colors)
    outline_width: float = Field(6.0, ge=0, le=30)
    shadow_offset: float = Field(3.0, ge=0, le=20)
    blur: float = Field(0.0, ge=0, le=10)  # soft edge on the outline (glow look)
    border_style: Literal["outline", "box"] = "outline"
    layout: Layout = Field(default_factory=Layout)
    highlight: Highlight = Field(default_factory=Highlight)
    animation: Animation = Field(default_factory=Animation)
    emphasis: Emphasis = Field(default_factory=Emphasis)


# ------------------------------------------------------------------ loading


def _builtin_dir() -> Path:
    return config.presets_dir() / "styles"


def _read(path: Path) -> CaptionStyle:
    try:
        return CaptionStyle.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise StyleError(f"Style preset {path.name} is invalid: {exc}") from exc


@lru_cache(maxsize=1)
def _builtin() -> dict[str, CaptionStyle]:
    paths = [p for p in sorted(_builtin_dir().glob("*.json")) if p.name != "schema.json"]
    return {s.id: s for s in (_read(p) for p in paths)}


def list_styles() -> list[CaptionStyle]:
    """Built-in presets followed by the user's saved presets."""
    styles = list(_builtin().values())
    for p in sorted(config.user_presets_dir().glob("*.json")):
        try:
            styles.append(_read(p))
        except StyleError:
            continue
    return styles


def get_style(style_id: str) -> CaptionStyle:
    for s in list_styles():
        if s.id == style_id:
            return s.model_copy(deep=True)
    ids = ", ".join(s.id for s in _builtin().values())
    raise StyleError(f"Unknown caption style '{style_id}'. Available: {ids}.")


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_style(
    preset: str = "bold-pop", overrides: dict[str, Any] | None = None, font: str | None = None
) -> CaptionStyle:
    """A preset with user tweaks applied (``overrides`` mirrors the JSON structure)."""
    data = get_style(preset).model_dump(by_alias=True)
    if overrides:
        data = _merge(data, overrides)
    if font:
        data["font"]["id"] = font
    try:
        return CaptionStyle.model_validate(data)
    except ValueError as exc:
        raise StyleError(f"Invalid caption settings: {exc}") from exc


def save_user_style(style: CaptionStyle) -> Path:
    """Store a style as a user preset (the id is a slug of the name, never a built-in id)."""
    from .fonts import slug

    sid = slug(style.name or style.id)
    if sid in _builtin():
        sid = f"my-{sid}"
    style = style.model_copy(update={"id": sid})
    path = config.user_presets_dir() / f"{sid}.json"
    path.write_text(json.dumps(style.model_dump(by_alias=True), indent=2), encoding="utf-8")
    return path


def json_schema() -> dict[str, Any]:
    return CaptionStyle.model_json_schema(by_alias=True)
