"""Per-clip publishing metadata: what the model must return and what is stored."""

from __future__ import annotations

import re
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Origin = Literal["ai", "fallback", "edited"]
FieldName = Literal[
    "title", "title_variants", "description", "hashtags", "tags", "hook",
    "pinned_comment", "category", "seo_keywords",
]  # fmt: skip
FIELDS: tuple[str, ...] = FieldName.__args__  # type: ignore[attr-defined]

# label, how the model is told, YouTube language code, fallback template set
LANGUAGES: dict[str, tuple[str, str, str]] = {
    "en": ("English", "English", "en"),
    "hi": ("Hindi", "Hindi in Devanagari script", "hi"),
    "hinglish": ("Hinglish", "Hinglish: Hindi words written in Latin script, mixed with English, as Indian creators write", "hi"),
    "mr": ("Marathi", "Marathi in Devanagari script", "mr"),
    "bn": ("Bengali", "Bengali", "bn"),
    "ta": ("Tamil", "Tamil", "ta"),
    "te": ("Telugu", "Telugu", "te"),
    "gu": ("Gujarati", "Gujarati", "gu"),
    "kn": ("Kannada", "Kannada", "kn"),
    "pa": ("Punjabi", "Punjabi in Gurmukhi script", "pa"),
    "ur": ("Urdu", "Urdu", "ur"),
    "es": ("Spanish", "Spanish", "es"),
    "pt": ("Portuguese", "Portuguese", "pt"),
    "fr": ("French", "French", "fr"),
    "de": ("German", "German", "de"),
    "id": ("Indonesian", "Indonesian", "id"),
}

# YouTube video categories that are assignable in most regions
CATEGORIES: dict[str, str] = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music", "15": "Pets & Animals",
    "17": "Sports", "19": "Travel & Events", "20": "Gaming", "22": "People & Blogs",
    "23": "Comedy", "24": "Entertainment", "25": "News & Politics", "26": "Howto & Style",
    "27": "Education", "28": "Science & Technology", "29": "Nonprofits & Activism",
}  # fmt: skip

TITLE_MAX = 100
DESCRIPTION_MAX = 4800  # leaves room for the hashtag line under YouTube's 5,000
HOOK_MAX = 150
COMMENT_MAX = 500
_HASHTAG = re.compile(r"^#[^\s#]{1,49}$", re.UNICODE)


class TitleVariant(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    reason: str = Field("", max_length=240)


class ClipMetadata(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    title_variants: list[TitleVariant] = Field(min_length=3, max_length=3)
    title_reason: str = Field("", max_length=240, description="Why this title was picked from the variants")
    description: str = Field("", max_length=DESCRIPTION_MAX)
    hashtags: list[str] = Field(min_length=3, max_length=5)
    tags: list[str] = Field(default_factory=list, max_length=30)
    hook: str = Field("", max_length=HOOK_MAX)
    pinned_comment: str = Field("", max_length=COMMENT_MAX)
    language: str = "en"
    category_id: str = "22"
    category: str = "People & Blogs"
    seo_keywords: list[str] = Field(default_factory=list, max_length=15)

    @field_validator("title", "description", "hook", "pinned_comment")
    @classmethod
    def _no_angle_brackets(cls, v: str) -> str:
        return v.replace("<", "‹").replace(">", "›").strip()

    @field_validator("hashtags")
    @classmethod
    def _hashtags(cls, v: list[str]) -> list[str]:
        for h in v:
            if not _HASHTAG.match(h):
                raise ValueError(f"'{h}' is not a hashtag (#word, no spaces)")
        if len({h.lower() for h in v}) != len(v):
            raise ValueError("hashtags repeat")
        return v

    @field_validator("category_id")
    @classmethod
    def _category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"unknown YouTube category {v}")
        return v


class MetadataRecord(BaseModel):
    clip_id: str
    source_id: str
    start: float
    end: float
    metadata: ClipMetadata
    origin: dict[str, Origin] = Field(default_factory=dict)
    provider: Literal["gemini", "fallback"] = "fallback"
    note: str | None = None
    variant: int = 0  # free-mode rotation counter for "regenerate"
    # publishing approval: any edit or regeneration afterwards withdraws it, so only the text
    # someone approved is ever published by automation
    approved: bool = False
    approved_at: float | None = None
    approved_by: Literal["user", "auto"] | None = None
    updated: float = Field(default_factory=time.time)


class MetadataDraft(BaseModel):
    """What Gemini is asked to return (loose types; normalised and validated afterwards)."""

    title_variants: list[TitleVariant]
    chosen_index: int = 0
    title_reason: str = ""
    description: str = ""
    hashtags: list[str] = []
    tags: list[str] = []
    hook: str = ""
    pinned_comment: str = ""
    category_id: str = "22"
    seo_keywords: list[str] = []
