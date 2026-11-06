"""The Gemini prompt for per-clip publishing metadata."""

from __future__ import annotations

import json

from .schemas import CATEGORIES


def metadata_prompt(ctx, language_instruction: str, avoid_titles: list[str]) -> str:
    cats = ", ".join(f"{k} = {v}" for k, v in CATEGORIES.items())
    avoid = json.dumps(avoid_titles[:30], ensure_ascii=False)
    return f"""You write YouTube Shorts metadata for one clip cut from a longer video.

STRICT RULES
- Use ONLY facts stated in the clip transcript below. Never invent names, numbers, places,
  claims, results or quotes. If the transcript doesn't say it, don't write it.
- The video title and channel are context only; do not claim anything from them that the clip doesn't say.
- Write every text field in: {language_instruction}.
- No clickbait that the clip does not deliver. No ALL CAPS titles. No "<" or ">" characters.

RETURN JSON with:
- title_variants: exactly 3 objects {{"title", "reason"}}; each title at most 70 characters, all different,
  and none equal to any of these titles already used for other clips of the same video: {avoid}
- chosen_index: 0, 1 or 2 — the best title; title_reason: one short sentence saying why it was picked
- description: 2-4 sentences (max 600 characters) summarising what is said in the clip
- hashtags: 3 to 5 hashtags like "#Money", no spaces, relevant to the clip; include "#Shorts"
- tags: 5-15 search tags (words or short phrases from the clip's topic)
- hook: the opening line that makes viewers keep watching (max 120 characters, from the clip's content)
- pinned_comment: a short comment the creator pins, inviting replies about the clip's topic
- category_id: one of {cats}
- seo_keywords: 5-10 keywords people would search for to find this clip

VIDEO TITLE: {ctx.video_title or "(unknown)"}
CHANNEL: {ctx.channel or "(unknown)"}
SPOKEN LANGUAGE: {ctx.spoken_language}
CLIP TRANSCRIPT ({ctx.end - ctx.start:.0f} seconds):
\"\"\"{ctx.text[:6000]}\"\"\"
"""
