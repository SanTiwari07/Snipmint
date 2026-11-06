"""Free, deterministic metadata built only from the clip's own transcript.

Nothing is invented: titles, hooks and descriptions are the speaker's own sentences; tags and
hashtags are words that occur in the clip. The only fixed wording is short framing text
("From …", a question for the pinned comment) in the chosen language. It cannot translate:
the clip's sentences stay in the language they were spoken in.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..analyze.providers.heuristic import STOPWORDS, hook_strength, tokens
from .schemas import (
    CATEGORIES,
    DESCRIPTION_MAX,
    HOOK_MAX,
    TITLE_MAX,
    ClipMetadata,
    TitleVariant,
)

_SENT_END = re.compile(r"(?<=[.!?।])\s+")

# framing text per language (falls back to English)
TEMPLATES: dict[str, dict[str, str]] = {
    "en": {"from": "From “{title}”.", "comment": "What do you think about {kw}? Tell us in the comments 👇",
           "comment_plain": "What did you think of this? Tell us in the comments 👇",
           "topics": "{a}, {b} and {c}", "topics2": "{a} and {b}"},
    "hi": {"from": "“{title}” से।", "comment": "{kw} के बारे में आप क्या सोचते हैं? कमेंट में बताइए 👇",
           "comment_plain": "आपको यह कैसा लगा? कमेंट में बताइए 👇", "topics": "{a}, {b} और {c}", "topics2": "{a} और {b}"},
    "hinglish": {"from": "“{title}” se.", "comment": "{kw} ke baare mein aap kya sochte ho? Comments mein batao 👇",
                 "comment_plain": "Aapko yeh kaisa laga? Comments mein batao 👇", "topics": "{a}, {b} aur {c}",
                 "topics2": "{a} aur {b}"},
    "mr": {"from": "“{title}” मधून.", "comment": "{kw} बद्दल तुम्हाला काय वाटतं? कमेंटमध्ये सांगा 👇",
           "comment_plain": "तुम्हाला हे कसं वाटलं? कमेंटमध्ये सांगा 👇", "topics": "{a}, {b} आणि {c}",
           "topics2": "{a} आणि {b}"},
}  # fmt: skip

CATEGORY_WORDS: dict[str, set[str]] = {
    "27": {"learn", "lesson", "study", "school", "teacher", "explain", "science", "history", "exam", "course", "education"},
    "28": {"ai", "software", "code", "coding", "tech", "technology", "computer", "app", "startup", "data", "phone", "robot"},
    "20": {"game", "gaming", "minecraft", "level", "boss", "player", "stream", "fortnite"},
    "10": {"song", "music", "album", "singer", "guitar", "beat", "concert"},
    "17": {"match", "goal", "cricket", "football", "team", "coach", "tournament", "fitness", "workout"},
    "26": {"recipe", "cook", "makeup", "diy", "tutorial", "tips", "hack", "style"},
    "25": {"election", "government", "minister", "policy", "news", "president", "parliament"},
    "23": {"funny", "joke", "comedy", "laugh", "prank"},
}  # fmt: skip


@dataclass
class ClipContext:
    """What the generator knows about one clip. Built from the source, not from the person."""

    clip_id: str
    start: float
    end: float
    text: str
    video_title: str = ""
    channel: str = ""
    spoken_language: str = "unknown"
    hint_title: str = ""
    hint_hook: str = ""
    keywords: list[str] = field(default_factory=list)


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_END.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:–-")
    return f"{cut}…"


def keywords(ctx: ClipContext, top: int = 10) -> list[str]:
    """Words that matter in this clip: hints first, then frequent non-stopwords (ties: first seen)."""
    out: list[str] = []
    for k in ctx.keywords:
        k = k.strip().lower()
        if k and k not in out and k in ctx.text.lower():
            out.append(k)
    toks = [t for t in tokens(ctx.text) if t not in STOPWORDS and len(t) > 3 and not t.isdigit()]
    order = {t: i for i, t in reversed(list(enumerate(toks)))}
    for t, _n in sorted(Counter(toks).items(), key=lambda kv: (-kv[1], order[kv[0]])):
        if t not in out:
            out.append(t)
    return out[:top]


def _tpl(language: str) -> dict[str, str]:
    return TEMPLATES.get(language, TEMPLATES["en"])


def _hashtag(word: str) -> str:
    parts = re.findall(r"\w+", word, flags=re.UNICODE)
    return "#" + "".join(p[:1].upper() + p[1:] for p in parts) if parts else ""


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def ranked_hooks(ctx: ClipContext) -> list[str]:
    """Candidate opening lines, best first (the earlier part of the clip preferred)."""
    sents = sentences(ctx.text)
    early = sents[: max(1, int(len(sents) * 0.6) + 1)]
    scored = sorted(enumerate(early), key=lambda p: (-hook_strength(p[1]), p[0]))
    out = [shorten(s, HOOK_MAX) for _i, s in scored]
    if ctx.hint_hook and ctx.hint_hook.lower() in ctx.text.lower():
        out.insert(0, shorten(ctx.hint_hook, HOOK_MAX))
    seen: list[str] = []
    for h in out:
        if h not in seen:
            seen.append(h)
    return seen


def title_variants(ctx: ClipContext, language: str) -> list[TitleVariant]:
    hooks = ranked_hooks(ctx)
    kws = keywords(ctx, 3)
    cands: list[TitleVariant] = []
    if hooks:
        cands.append(TitleVariant(title=shorten(hooks[0], 70), reason="The clip's strongest opening line, in the speaker's words."))
    if ctx.hint_title and ctx.hint_title.strip():
        cands.append(TitleVariant(title=shorten(ctx.hint_title, 70), reason="The title from the moment analysis."))
    if len(kws) >= 2:
        t = _tpl(language)
        topic = (t["topics"].format(a=_cap(kws[0]), b=kws[1], c=kws[2]) if len(kws) >= 3
                 else t["topics2"].format(a=_cap(kws[0]), b=kws[1]))  # fmt: skip
        cands.append(TitleVariant(title=shorten(topic, 70), reason="Names the main topics, good for search."))
    for h in hooks[1:]:
        cands.append(TitleVariant(title=shorten(h, 70), reason="Another strong line from the clip."))
    if ctx.video_title:
        cands.append(TitleVariant(title=shorten(ctx.video_title, 70), reason="The source video's own title."))
    uniq: list[TitleVariant] = []
    for c in cands:
        if c.title.lower() not in {u.title.lower() for u in uniq}:
            uniq.append(c)
    n = 2
    while len(uniq) < 3:  # very short clips: number the only line we have
        base = uniq[0].title if uniq else (shorten(ctx.text, 60) or "Clip")
        uniq.append(TitleVariant(title=shorten(f"{base} ({n})", TITLE_MAX), reason="Only one usable line in this clip."))
        n += 1
    return uniq[:3]


def pick_title(variants: list[TitleVariant]) -> tuple[int, str]:
    """Prefer a question or a 30-70 character line: they read well on the Shorts feed."""

    def score(v: TitleVariant) -> float:
        s = 0.0
        if v.title.rstrip().endswith("?"):
            s += 1.0
        if 30 <= len(v.title) <= 70:
            s += 0.5
        return s

    best = max(range(len(variants)), key=lambda i: (score(variants[i]), -i))
    v = variants[best]
    why = "It asks a question viewers want answered" if v.title.rstrip().endswith("?") else "It is the clearest, right-length line"
    return best, f"{why} ({v.reason[:1].lower() + v.reason[1:]})"


def category_for(ctx: ClipContext) -> str:
    toks = set(tokens(f"{ctx.text} {ctx.video_title}"))
    best, hits = "22", 0
    for cid, words in CATEGORY_WORDS.items():
        n = len(toks & words)
        if n > hits:
            best, hits = cid, n
    return best if hits >= 2 else "22"


def build(ctx: ClipContext, language: str, variant: int = 0) -> ClipMetadata:
    """Every field from the transcript. ``variant`` rotates title, hook and comment choices."""
    variants = title_variants(ctx, language)
    if variant:
        variants = variants[variant % 3 :] + variants[: variant % 3]
        chosen = 0
        reason = "Another of the three candidates (regenerated)."
    else:
        chosen, reason = pick_title(variants)
    kws = keywords(ctx, 10)
    hooks = ranked_hooks(ctx) or [shorten(ctx.text, HOOK_MAX)]
    hook = hooks[variant % len(hooks)]
    sents = sentences(ctx.text)
    body = shorten(" ".join(sents[:3]), 600)
    t = _tpl(language)
    desc = body
    if ctx.video_title:
        desc = f"{body}\n\n{t['from'].format(title=shorten(ctx.video_title, 120))}"
    tags_out: list[str] = []
    for k in [*kws, *keywords(ClipContext(ctx.clip_id, 0, 0, ctx.video_title), 5)]:
        if k not in tags_out:
            tags_out.append(k)
    hashtags = ["#Shorts"]
    for k in kws:
        h = _hashtag(k)
        if h and len(hashtags) < 4 and h.lower() not in {x.lower() for x in hashtags}:
            hashtags.append(h)
    for generic in ("#Clips", "#Highlights"):
        if len(hashtags) < 3:
            hashtags.append(generic)
    kw_for_comment = kws[variant % len(kws)] if kws else ""
    comment = t["comment"].format(kw=kw_for_comment) if kw_for_comment else t["comment_plain"]
    cid = category_for(ctx)
    return ClipMetadata(
        title=variants[chosen].title,
        title_variants=variants,
        title_reason=reason,
        description=desc[:DESCRIPTION_MAX],
        hashtags=hashtags,
        tags=tags_out[:15],
        hook=hook,
        pinned_comment=comment,
        language=language,
        category_id=cid,
        category=CATEGORIES[cid],
        seo_keywords=kws[:10],
    )
