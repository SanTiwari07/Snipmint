"""HTTP routes for per-clip publishing metadata: /api/metadata/*."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ..ingest.source import Source
from ..metadata import generator
from ..metadata.schemas import CATEGORIES, LANGUAGES, MetadataRecord
from .clips_api import gemini_key_from, gemini_model_from


class ClipRef(BaseModel):
    clip_id: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = ""
    hook_text: str = ""
    keywords: list[str] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    clips: list[ClipRef] = Field(min_length=1, max_length=30)
    language: str = "en"
    provider: Literal["auto", "gemini", "fallback"] = "auto"


class RegenerateRequest(BaseModel):
    field: str | None = Field(None, description="One field to regenerate; empty = every field you haven't edited")
    language: str | None = None
    provider: Literal["auto", "gemini", "fallback"] = "auto"


class EditRequest(BaseModel):
    changes: dict[str, Any]


class ApprovalRequest(BaseModel):
    approved: bool = True


class MetadataSet(BaseModel):
    source_id: str
    clips: dict[str, MetadataRecord]


class LanguageInfo(BaseModel):
    code: str
    label: str


class MetadataOptions(BaseModel):
    languages: list[LanguageInfo]
    categories: dict[str, str]


def _source(source_id: str) -> Source:
    src = Source.existing(source_id)
    if src is None:
        raise HTTPException(404, "That video isn't known here. Paste the link again.")
    return src


def _key(request: Request, provider: str) -> tuple[str | None, str | None]:
    if provider == "fallback":
        return None, None
    key = gemini_key_from(request)
    if provider == "gemini" and not key:
        raise generator.MetadataError("Advanced mode needs a Gemini key.")
    return key, gemini_model_from(request)


def register(app: FastAPI) -> None:
    @app.get("/api/metadata/options", response_model=MetadataOptions, tags=["metadata"])
    def options() -> MetadataOptions:
        return MetadataOptions(languages=[LanguageInfo(code=k, label=v[0]) for k, v in LANGUAGES.items()],
                               categories=CATEGORIES)  # fmt: skip

    @app.get("/api/metadata/{source_id}", response_model=MetadataSet, tags=["metadata"])
    def get_metadata(source_id: str) -> MetadataSet:
        src = _source(source_id)
        return MetadataSet(source_id=src.id, clips=generator.load_all(src))

    @app.post("/api/metadata/{source_id}/generate", response_model=MetadataSet, tags=["metadata"])
    async def generate(source_id: str, req: GenerateRequest, request: Request) -> MetadataSet:
        src = _source(source_id)
        generator.check_language(req.language)
        key, model = _key(request, req.provider)

        def run() -> dict[str, MetadataRecord]:
            records = generator.load_all(src)
            for c in req.clips:
                ctx = generator.context_for(src, c.clip_id, c.start, c.end, hint_title=c.title,
                                            hint_hook=c.hook_text, keywords=c.keywords)  # fmt: skip
                generator.generate(src, ctx, req.language, gemini_key=key, gemini_model=model, records=records)
            return records

        return MetadataSet(source_id=src.id, clips=await asyncio.to_thread(run))

    @app.post("/api/metadata/{source_id}/{clip_id}/regenerate", response_model=MetadataRecord, tags=["metadata"])
    async def regenerate(source_id: str, clip_id: str, req: RegenerateRequest, request: Request) -> MetadataRecord:
        src = _source(source_id)
        records = generator.load_all(src)
        rec = records.get(clip_id)
        if rec is None:
            raise generator.MetadataError("Generate this clip's metadata first.")
        key, model = _key(request, req.provider)
        language = req.language or rec.metadata.language
        ctx = generator.context_for(src, clip_id, rec.start, rec.end)
        fields = [req.field] if req.field else None
        return await asyncio.to_thread(generator.generate, src, ctx, language, fields=fields, gemini_key=key,
                                       gemini_model=model, records=records)  # fmt: skip

    @app.post("/api/metadata/{source_id}/{clip_id}/approve", response_model=MetadataRecord, tags=["metadata"])
    def approve(source_id: str, clip_id: str, req: ApprovalRequest) -> MetadataRecord:
        """Approve the clip's current metadata for publishing (needed before automation may upload it)."""
        return generator.set_approval(_source(source_id), clip_id, req.approved, "user")

    @app.put("/api/metadata/{source_id}/{clip_id}", response_model=MetadataRecord, tags=["metadata"])
    def edit(source_id: str, clip_id: str, req: EditRequest) -> MetadataRecord:
        return generator.edit(_source(source_id), clip_id, req.changes)
