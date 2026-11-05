"""Free, offline moment picker: no AI service, no API key.

Scores every run of whole sentences in the requested length band on a handful of signals
(YouTube "most replayed" heatmap, how strong the opening line is, loudness, keyword density,
clean sentence boundaries, speech pace and camera-cut density), then keeps the best
non-overlapping spans. Weights live in ``weights.json`` next to this file.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...ingest.source import SourceMeta
from ...transcribe.models import Transcript, Word
from ..schemas import AnalyzeOptions, Moment
from ..signals import Signals
from ..windows import (
    Sentence,
    Span,
    auto_clip_count,
    candidate_spans,
    overlap_ratio,
    snap_to_words,
    split_sentences,
)

WEIGHTS_FILE = Path(__file__).with_name("weights.json")
DEFAULT_WEIGHTS = {
    "heatmap": 0.30, "hook": 0.20, "energy": 0.15, "density": 0.15,
    "complete": 0.10, "pace": 0.05, "cuts": 0.05,
}  # fmt: skip
MAX_OVERLAP = 0.15
MIN_SPEECH_WORDS_PER_MIN = 5

STOPWORDS = set(["a", "an", "the", "and", "or", "but", "so", "if", "then", "than", "that", "this", "these", "those", "there", "here", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "doing", "have", "has", "had", "having", "i", "me", "my", "mine", "we", "us", "our", "you", "your", "yours", "he", "him", "his", "she", "her", "it", "its", "they", "them", "their", "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not", "only", "own", "same", "too", "very", "can", "will", "just", "should", "now", "of", "at", "by", "for", "with", "about", "against", "between", "into", "through", "during", "before", "after", "above", "below", "to", "from", "up", "down", "in", "out", "on", "off", "over", "under", "again", "further", "once", "also", "yeah", "yes", "okay", "ok", "like", "um", "uh", "oh", "well", "really", "actually", "basically", "gonna", "wanna", "kind", "sort", "thing", "things", "know", "mean", "think", "going", "get", "got", "go", "say", "said", "one", "two", "lot", "let", "right", "hai", "ka", "ki", "ke", "ko", "se", "mein", "main", "aur", "bhi", "to", "toh", "ye", "yeh", "wo", "woh", "hum", "tum", "aap", "kya", "nahi", "na", "ho", "tha", "thi", "the"])  # fmt: skip
STOPWORDS |= set(
    "it's that's there's i'm you're we're they're don't can't won't didn't isn't wasn't i've i'll "
    "find make made want need take come came look see way back even still much many every".split()
)
DANGLING_START = {"and", "so", "but", "because", "that", "which", "then", "also", "or", "plus",
                  "like", "um", "uh", "yeah", "okay", "ok", "well", "anyway"}  # fmt: skip
FILLER_START = {"um", "uh", "so", "and", "but", "because", "like", "yeah", "okay", "ok", "well"}
CURIOSITY = {
    "never", "nobody", "secret", "mistake", "mistakes", "truth", "actually", "why", "how",
    "wrong", "worst", "best", "biggest", "nobody's", "everyone", "stop", "crazy", "insane",
    "surprising", "shocking", "real", "problem", "lie", "lied", "fail", "failed", "rich", "money",
    "क्यों", "कैसे", "सच", "गलती", "kyun", "kaise", "sach", "galti",
}  # fmt: skip
SECOND_PERSON = {"you", "your", "you're", "youre", "yourself", "you've", "you'll", "aap", "tum", "आप", "तुम"}
NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                "hundred", "thousand", "million", "billion", "first", "second", "third"}  # fmt: skip
_TOKEN = re.compile(r"[\w'’]+", re.UNICODE)


def load_weights() -> dict[str, float]:
    try:
        data = json.loads(WEIGHTS_FILE.read_text(encoding="utf-8"))
        return {k: float(data.get(k, v)) for k, v in DEFAULT_WEIGHTS.items()}
    except (OSError, ValueError):
        return dict(DEFAULT_WEIGHTS)


def tokens(text: str) -> list[str]:
    return [t.lower().replace("’", "'") for t in _TOKEN.findall(text)]


# ---------------------------------------------------------------------------- features


def hook_strength(sentence: str) -> float:
    """0..1: how well this line works as the first thing a viewer hears."""
    toks = tokens(sentence)
    if not toks:
        return 0.0
    score = 0.0
    if sentence.rstrip().endswith("?"):
        score += 0.3
    if SECOND_PERSON & set(toks):
        score += 0.2
    if any(t.isdigit() for t in toks) or NUMBER_WORDS & set(toks):
        score += 0.15
    if CURIOSITY & set(toks):
        score += 0.25
    if 4 <= len(toks) <= 14:
        score += 0.1
    elif len(toks) > 30:
        score -= 0.1
    if toks[0] in FILLER_START:
        score -= 0.3
    return max(0.0, min(1.0, score))


@dataclass
class _Ctx:
    words: list[Word]
    sentences: list[Sentence]
    signals: Signals
    meta: SourceMeta
    idf: dict[str, float]
    sent_tokens: list[list[str]]
    hooks: list[float]
    loud_z: np.ndarray
    median_pace: float
    weights: dict[str, float]
    keyword_cache: dict[tuple[int, int], list[tuple[str, float]]] = field(default_factory=dict)


def _build_ctx(transcript: Transcript, signals: Signals, meta: SourceMeta) -> _Ctx:
    words = transcript.words
    sentences = split_sentences(words)
    sent_tokens = [[t for t in tokens(s.text)] for s in sentences]
    df: Counter[str] = Counter()
    for toks in sent_tokens:
        df.update({t for t in toks if t not in STOPWORDS and len(t) > 2 and not t.isdigit()})
    n = max(1, len(sentences))
    idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}
    total_dur = max(1e-6, sum(s.end - s.start for s in sentences))
    median_pace = len(words) / total_dur
    weights = load_weights()
    if not signals.heatmap:  # no heatmap: share its weight among the others
        spare = weights.pop("heatmap")
        rest = sum(weights.values()) or 1.0
        weights = {k: v + spare * v / rest for k, v in weights.items()}
        weights["heatmap"] = 0.0
    return _Ctx(
        words=words, sentences=sentences, signals=signals, meta=meta, idf=idf,
        sent_tokens=sent_tokens, hooks=[hook_strength(s.text) for s in sentences],
        loud_z=signals.loudness_z(), median_pace=median_pace, weights=weights,
    )  # fmt: skip


def _keywords(ctx: _Ctx, span: Span, top: int = 5) -> list[tuple[str, float]]:
    key = (span.first_sentence, span.last_sentence)
    if key not in ctx.keyword_cache:
        tf: Counter[str] = Counter()
        for toks in ctx.sent_tokens[span.first_sentence : span.last_sentence + 1]:
            tf.update(t for t in toks if t in ctx.idf)
        scored = sorted(((t, c * ctx.idf[t]) for t, c in tf.items()), key=lambda x: -x[1])
        ctx.keyword_cache[key] = scored[:20]
    return ctx.keyword_cache[key][:top]


def _features(ctx: _Ctx, span: Span) -> dict[str, float]:
    s0 = ctx.sentences[span.first_sentence]
    s1 = ctx.sentences[span.last_sentence]
    f: dict[str, float] = {}

    heat = ctx.signals.mean_heat(span.start, span.end)
    f["heatmap"] = heat if heat is not None else 0.0

    # best hook in the first two sentences (a weak greeting before a great line still works)
    f["hook"] = max(ctx.hooks[span.first_sentence],
                    0.8 * ctx.hooks[min(span.first_sentence + 1, span.last_sentence)])  # fmt: skip

    if ctx.loud_z.size:
        hop = ctx.signals.hop
        seg = ctx.loud_z[int(span.start / hop) : max(int(span.start / hop) + 1, int(span.end / hop))]
        z = float(np.mean(seg)) if seg.size else 0.0
        peaks = float(np.mean(seg > 1.5)) if seg.size else 0.0
        f["energy"] = max(0.0, min(1.0, (z + 1.0) / 3.0 + peaks))
    else:
        f["energy"] = 0.5

    kws = _keywords(ctx, span, top=10)
    n_tok = sum(len(t) for t in ctx.sent_tokens[span.first_sentence : span.last_sentence + 1]) or 1
    f["density"] = sum(v for _, v in kws) / n_tok  # normalised later across spans

    first_tok = (tokens(s0.text) or [""])[0]
    clean_start = first_tok not in DANGLING_START
    clean_end = s1.text.rstrip().endswith((".", "?", "!", "।", "…"))
    f["complete"] = 0.5 * clean_start + 0.5 * clean_end

    n_words = s1.last - s0.first + 1
    pace = n_words / max(1e-6, span.duration)
    ratio = pace / max(1e-6, ctx.median_pace)
    f["pace"] = max(0.0, 1.0 - abs(1.0 - ratio)) if ratio < 1.6 else 0.6

    cuts_per_3s = ctx.signals.cuts_between(span.start, span.end) * 3.0 / max(1.0, span.duration)
    f["cuts"] = 1.0 if cuts_per_3s <= 1.0 else max(0.0, 1.0 - (cuts_per_3s - 1.0))
    return f


_WHY = {
    "heatmap": "Most replayed part of the video",
    "hook": "Opens with a strong line",
    "energy": "High-energy delivery",
    "density": "Packed with key ideas",
    "complete": "Starts and ends on full sentences",
    "pace": "Clear, steady speech",
    "cuts": "Stable shot",
}


def _why(ctx: _Ctx, feats: dict[str, float], span: Span) -> str:
    contrib = sorted(((k, ctx.weights.get(k, 0) * v) for k, v in feats.items()), key=lambda x: -x[1])
    parts = []
    for k, _ in contrib[:2]:
        text = _WHY[k]
        if k == "hook" and ctx.sentences[span.first_sentence].text.rstrip().endswith("?"):
            text = "Opens with a question"
        parts.append(text)
    return " · ".join(parts)


def _shorten(text: str, max_words: int) -> str:
    words = text.strip().split()
    out = " ".join(words[:max_words])
    out = out.rstrip(",;:-–— ")
    return out + ("…" if len(words) > max_words else "")


def _hook_text(ctx: _Ctx, span: Span) -> str:
    n = span.last_sentence - span.first_sentence + 1
    upto = span.first_sentence + max(1, math.ceil(n * 0.4))
    best = max(range(span.first_sentence, upto), key=lambda i: ctx.hooks[i])
    return _shorten(ctx.sentences[best].text, 9)


def _title(ctx: _Ctx, span: Span, hook: str) -> str:
    mid = (span.start + span.end) / 2
    for ch in ctx.meta.chapters:
        if ch.start <= mid < ch.end and ch.title and not re.fullmatch(r"(?i)(intro|outro)\W*", ch.title):
            return ch.title[:80]
    title = hook.rstrip("….?! ")
    return (title[:1].upper() + title[1:])[:80]


# ---------------------------------------------------------------------------- main


def speech_is_sparse(transcript: Transcript, duration: float) -> bool:
    n = len(transcript.words)
    minutes = max(1e-6, (duration or transcript.duration) / 60)
    return n < 8 or n / minutes < MIN_SPEECH_WORDS_PER_MIN


def _no_speech_moments(meta: SourceMeta, signals: Signals, opts: AnalyzeOptions, count: int) -> list[Moment]:
    """Music/gaming/b-roll: use the heatmap peaks, else spread clips evenly."""
    length = (opts.min_seconds + opts.max_seconds) / 2
    dur = meta.duration
    if dur <= length:
        return [Moment(id="m01", start=0.0, end=round(dur, 3), score=50, why="Whole video",
                       title=meta.title[:80], hook_text="")]  # fmt: skip
    candidates: list[tuple[float, float]] = []
    if signals.heatmap:
        step = 5.0
        t = 0.0
        while t + length <= dur:
            candidates.append((t, signals.mean_heat(t, t + length) or 0.0))
            t += step
        candidates.sort(key=lambda x: -x[1])
    else:
        gap = (dur - length) / max(1, count)
        candidates = [(gap * (i + 0.5), 0.5) for i in range(count)]
    picked: list[Moment] = []
    for start, heat in candidates:
        span = (start, start + length)
        if any(overlap_ratio(span, (m.start, m.end)) > MAX_OVERLAP for m in picked):
            continue
        why = "Most replayed part of the video" if signals.heatmap else "Evenly spaced (no speech found)"
        picked.append(Moment(id=f"m{len(picked) + 1:02d}", start=round(start, 3), end=round(start + length, 3),
                             score=int(40 + 59 * heat), why=why, title=meta.title[:80]))  # fmt: skip
        if len(picked) >= count:
            break
    return sorted(picked, key=lambda m: m.start)


def pick_moments(
    transcript: Transcript, signals: Signals, meta: SourceMeta, opts: AnalyzeOptions
) -> list[Moment]:
    duration = meta.duration or transcript.duration
    count = opts.count or auto_clip_count(duration)
    if speech_is_sparse(transcript, duration):
        return _no_speech_moments(meta, signals, opts, count)

    ctx = _build_ctx(transcript, signals, meta)
    spans = candidate_spans(ctx.sentences, opts.min_seconds, opts.max_seconds)
    if not spans:  # band longer than the whole video, or very long sentences
        spans = candidate_spans(ctx.sentences, min(opts.min_seconds, duration * 0.5), max(opts.max_seconds, duration))
    if not spans:
        return _no_speech_moments(meta, signals, opts, count)

    feats = [_features(ctx, s) for s in spans]
    max_density = max((f["density"] for f in feats), default=0.0) or 1.0
    for f in feats:
        f["density"] = f["density"] / max_density
    raw = np.array([sum(ctx.weights.get(k, 0.0) * v for k, v in f.items()) for f in feats])
    order = np.argsort(-raw, kind="stable")
    ranks = np.empty_like(order)
    ranks[order] = np.arange(len(order))
    percentile = 1.0 - ranks / max(1, len(order) - 1)
    top = float(raw.max()) or 1.0

    picked: list[tuple[Span, dict[str, float], float]] = []
    for i in order:
        span = spans[int(i)]
        if any(overlap_ratio((span.start, span.end), (p.start, p.end)) > MAX_OVERLAP for p, _, _ in picked):
            continue
        score = 100 * (0.5 * float(raw[i]) / top + 0.5 * float(percentile[i]))
        picked.append((span, feats[int(i)], score))
        if len(picked) >= count:
            break

    moments: list[Moment] = []
    for span, f, score in picked:
        start, end = snap_to_words(span.start, span.end, ctx.words, duration)
        hook = _hook_text(ctx, span)
        text = " ".join(s.text for s in ctx.sentences[span.first_sentence : span.last_sentence + 1])
        moments.append(Moment(
            id="", start=start, end=end, score=max(1, min(99, round(score))),
            why=_why(ctx, f, span), title=_title(ctx, span, hook), hook_text=hook,
            keywords=[k for k, _ in _keywords(ctx, span, top=5)], provider="heuristic", text=text,
        ))  # fmt: skip
    moments.sort(key=lambda m: -m.score)
    for i, m in enumerate(moments, 1):
        m.id = f"m{i:02d}"
    return moments
