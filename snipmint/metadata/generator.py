"""Generate, regenerate and store per-clip metadata.

- Gemini (existing key/model plumbing and ``make_call`` retries) when a key is available,
  validated against ``ClipMetadata``; any failure falls back to the free transcript-based
  builder and says why.
- Fields the person edited are never overwritten by "regenerate all". Regenerating one field
  only changes that field.
- Titles are unique within one source video.
- Stored in ``sources/{id}/metadata.json`` (atomic writes via ``Source.write_json``).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from pydantic import ValidationError

from ..analyze.service import load_transcript
from ..errors import SnipmintError
from ..ingest.source import Source
from . import fallback, prompts
from .schemas import (
    CATEGORIES,
    FIELDS,
    LANGUAGES,
    TITLE_MAX,
    ClipMetadata,
    MetadataDraft,
    MetadataRecord,
    TitleVariant,
)

log = logging.getLogger("snipmint.metadata")

FILE = "metadata.json"


class MetadataError(SnipmintError):
    http_status = 422


class DuplicateTitle(MetadataError):
    http_status = 409


def norm_title(title: str) -> str:
    return re.sub(r"[\W_]+", " ", title.lower(), flags=re.UNICODE).strip()


def check_language(language: str) -> str:
    if language not in LANGUAGES:
        raise MetadataError(f"Unsupported language '{language}'. Pick one of: {', '.join(LANGUAGES)}.")
    return language


# ------------------------------------------------------------------------- storage


def load_all(source: Source) -> dict[str, MetadataRecord]:
    if not source.file(FILE).is_file():
        return {}
    raw = source.read_json(FILE) or {}
    out: dict[str, MetadataRecord] = {}
    for cid, rec in (raw.get("clips") or {}).items():
        try:
            out[cid] = MetadataRecord.model_validate(rec)
        except ValidationError:
            log.warning("Ignoring an unreadable metadata entry for clip %s", cid)
    return out


def save_all(source: Source, records: dict[str, MetadataRecord]) -> None:
    source.write_json(FILE, {"version": 1, "clips": {k: v.model_dump() for k, v in records.items()}})


# ------------------------------------------------------------------------- context


def context_for(source: Source, clip_id: str, start: float, end: float, *, hint_title: str = "",
                hint_hook: str = "", keywords: list[str] | None = None) -> fallback.ClipContext:  # fmt: skip
    if not source.file("transcript.json").is_file():
        raise MetadataError("This video has no transcript yet. Wait until it is prepared.")
    if end <= start:
        raise MetadataError("The clip ends before it starts.")
    transcript = load_transcript(source)
    words = transcript.words_between(start, end)
    text = re.sub(r"\s+", " ", "".join(w.text if w.text.startswith(" ") else f" {w.text}" for w in words)).strip()
    if not text:
        raise MetadataError("There is no speech in this clip to write metadata from.")
    meta = source.meta
    return fallback.ClipContext(
        clip_id=clip_id, start=start, end=end, text=text,
        video_title=meta.title if meta else "", channel=meta.channel if meta else "",
        spoken_language=transcript.language, hint_title=hint_title, hint_hook=hint_hook,
        keywords=list(keywords or []),
    )  # fmt: skip


# --------------------------------------------------------------------------- gemini


def _clean_list(items: list[str], limit: int, max_len: int = 60) -> list[str]:
    out: list[str] = []
    for x in items:
        x = re.sub(r"\s+", " ", str(x)).strip().strip(",")[:max_len]
        if x and x.lower() not in {o.lower() for o in out}:
            out.append(x)
    return out[:limit]


def _clean_hashtags(items: list[str]) -> list[str]:
    out: list[str] = []
    for h in items:
        word = re.sub(r"[^\w]", "", str(h).lstrip("#"), flags=re.UNICODE)[:49]
        tag = f"#{word}" if word else ""
        if tag and tag.lower() not in {o.lower() for o in out}:
            out.append(tag)
    return out[:5]


def from_draft(draft: dict[str, Any], language: str, ctx: fallback.ClipContext) -> ClipMetadata:
    d = MetadataDraft.model_validate(draft)
    variants = [TitleVariant(title=fallback.shorten(v.title, TITLE_MAX), reason=fallback.shorten(v.reason, 240))
                for v in d.title_variants if v.title.strip()][:3]  # fmt: skip
    if len(variants) < 3:
        raise MetadataError("The model returned fewer than three titles.")
    chosen = d.chosen_index if 0 <= d.chosen_index < 3 else 0
    hashtags = _clean_hashtags(d.hashtags)
    for extra in fallback.build(ctx, language).hashtags:  # top up from the clip's own words
        if len(hashtags) >= 3:
            break
        if extra.lower() not in {h.lower() for h in hashtags}:
            hashtags.append(extra)
    cid = d.category_id if d.category_id in CATEGORIES else fallback.category_for(ctx)
    return ClipMetadata(
        title=variants[chosen].title, title_variants=variants,
        title_reason=fallback.shorten(d.title_reason or variants[chosen].reason, 240),
        description=d.description.strip()[:4800], hashtags=hashtags,
        tags=_clean_list(d.tags, 15), hook=fallback.shorten(d.hook, 150),
        pinned_comment=d.pinned_comment.strip()[:500], language=language,
        category_id=cid, category=CATEGORIES[cid], seo_keywords=_clean_list(d.seo_keywords, 10),
    )  # fmt: skip


def gemini_metadata(ctx: fallback.ClipContext, language: str, *, client: Any = None, api_key: str | None = None,
                    model: str | None = None, avoid_titles: list[str] | None = None) -> ClipMetadata:  # fmt: skip
    from ..analyze.providers import gemini

    if client is None:
        if not api_key:
            raise MetadataError("No Gemini key.")
        client = gemini.make_client(api_key)
    model = (model or "").strip() or gemini.model_name()
    prompt = prompts.metadata_prompt(ctx, LANGUAGES[language][1], avoid_titles or [])
    data = gemini.make_call(client, model)(prompt, MetadataDraft)
    return from_draft(data, language, ctx)


# -------------------------------------------------------------------------- generate


def _unique_title(meta: ClipMetadata, taken: set[str]) -> ClipMetadata:
    if norm_title(meta.title) not in taken:
        return meta
    for v in meta.title_variants:
        if norm_title(v.title) not in taken:
            return meta.model_copy(update={"title": v.title, "title_reason": f"{v.reason} (the first pick was "
                                           "already used by another clip of this video)"})  # fmt: skip
    n = 2
    while True:
        cand = f"{fallback.shorten(meta.title, TITLE_MAX - 10)} (part {n})"
        if norm_title(cand) not in taken:
            return meta.model_copy(update={"title": cand})
        n += 1


def generate(
    source: Source,
    ctx: fallback.ClipContext,
    language: str,
    *,
    fields: list[str] | None = None,
    gemini_key: str | None = None,
    gemini_model: str | None = None,
    client: Any = None,
    variant: int | None = None,
    records: dict[str, MetadataRecord] | None = None,
) -> MetadataRecord:
    """(Re)generate one clip's metadata and store it.

    ``fields=None`` regenerates everything except fields the person edited; a list of fields
    regenerates exactly those (an explicit request, so an edited field is replaced too).
    """
    check_language(language)
    for f in fields or []:
        if f not in FIELDS:
            raise MetadataError(f"Unknown field '{f}'.")
    records = load_all(source) if records is None else records
    existing = records.get(ctx.clip_id)
    taken = {norm_title(r.metadata.title) for cid, r in records.items() if cid != ctx.clip_id}

    use_ai = bool(client is not None or gemini_key)
    note = None
    fresh: ClipMetadata
    provider = "fallback"
    if use_ai:
        try:
            avoid = sorted({r.metadata.title for r in records.values()})
            fresh = gemini_metadata(ctx, language, client=client, api_key=gemini_key, model=gemini_model,
                                    avoid_titles=avoid)  # fmt: skip
            provider = "gemini"
        except Exception as exc:  # any model problem -> the free builder, and say so
            from ..analyze.providers.gemini import redact

            note = f"Used the free metadata builder: {redact(str(exc))[:200]}"
            log.info("Metadata fallback for %s: %s", ctx.clip_id, note)
    if provider == "fallback":
        if variant is None:
            # regenerating in free mode rotates through the candidates instead of repeating itself
            variant = existing.variant + 1 if (existing and fields) else 0
        fresh = fallback.build(ctx, language, variant)
        if language not in ("en", "hinglish") and ctx.spoken_language not in (language, "unknown"):
            note = (note + " " if note else "") + (
                "The free builder can't translate: the clip's own sentences stay in the spoken language."
            )
    origin_new = "ai" if provider == "gemini" else "fallback"

    if existing is None:
        merged = fresh
        origin = {f: origin_new for f in FIELDS}
    else:
        base = existing.metadata.model_dump()
        new = fresh.model_dump()
        origin = dict(existing.origin)
        targets = list(fields) if fields else [f for f in FIELDS if existing.origin.get(f) != "edited"]
        for f in targets:
            if f == "title":
                base["title"], base["title_reason"] = new["title"], new["title_reason"]
            elif f == "category":
                base["category_id"], base["category"] = new["category_id"], new["category"]
            else:
                base[f] = new[f]
            origin[f] = origin_new
        base["language"] = language
        merged = ClipMetadata.model_validate(base)

    if not existing or origin.get("title") != "edited" or (fields and "title" in fields):
        merged = _unique_title(merged, taken)
    record = MetadataRecord(clip_id=ctx.clip_id, source_id=source.id, start=ctx.start, end=ctx.end,
                            metadata=merged, origin=origin, provider=provider, note=note,
                            variant=variant or 0, updated=time.time())  # fmt: skip
    records[ctx.clip_id] = record
    save_all(source, records)
    return record


def edit(source: Source, clip_id: str, changes: dict[str, Any]) -> MetadataRecord:
    """Apply the person's edits; those fields are marked ``edited`` and kept on regenerate."""
    records = load_all(source)
    rec = records.get(clip_id)
    if rec is None:
        raise MetadataError("Generate this clip's metadata first.")
    data = rec.metadata.model_dump()
    origin = dict(rec.origin)
    for key, value in changes.items():
        if key == "category_id":
            data["category_id"] = value
            data["category"] = CATEGORIES.get(str(value), data["category"])
            origin["category"] = "edited"
        elif key in ("title_reason", "language"):
            data[key] = value
        elif key in FIELDS:
            data[key] = value
            origin[key] = "edited"
        else:
            raise MetadataError(f"Unknown field '{key}'.")
    if "hashtags" in changes:
        data["hashtags"] = _clean_hashtags(changes["hashtags"]) if isinstance(changes["hashtags"], list) else changes["hashtags"]
    try:
        meta = ClipMetadata.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", []))
        raise MetadataError(f"{where}: {first.get('msg')}") from None
    if "title" in changes:
        taken = {norm_title(r.metadata.title) for cid, r in records.items() if cid != clip_id}
        if norm_title(meta.title) in taken:
            raise DuplicateTitle("Another clip from this video already has this title. Titles must be unique.")
    rec = rec.model_copy(update={"metadata": meta, "origin": origin, "updated": time.time(),
                                 "approved": False, "approved_at": None, "approved_by": None})  # fmt: skip
    records[clip_id] = rec
    save_all(source, records)
    return rec


def set_approval(source: Source, clip_id: str, approved: bool, by: str = "user") -> MetadataRecord:
    """Approve (or withdraw approval of) a clip's metadata for publishing."""
    records = load_all(source)
    rec = records.get(clip_id)
    if rec is None:
        raise MetadataError("Generate this clip's metadata first.")
    if approved:
        dupes = {norm_title(r.metadata.title) for cid, r in records.items() if cid != clip_id}
        if norm_title(rec.metadata.title) in dupes:
            raise DuplicateTitle("Another clip from this video has the same title. Change one before approving.")
    rec = rec.model_copy(update={"approved": approved, "approved_at": time.time() if approved else None,
                                 "approved_by": by if approved else None})  # fmt: skip
    records[clip_id] = rec
    save_all(source, records)
    return rec
