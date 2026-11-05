"""Bridge: Whisper words of one clip -> the ScriptAnalysis + WordTiming the caption engine uses.

With this, every caption preset (karaoke, highlighter, keyword emphasis, ...) works on real
speech instead of a narrated script.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..models import ScriptAnalysis, Sentence, Token, WordTiming
from ..script.analyze import detect_language, fold
from ..transcribe.models import Word

SENTENCE_END = (".", "?", "!", "…", "।")
SENTENCE_GAP = 0.7


def clip_words(
    words: Iterable[Word],
    start: float,
    end: float,
    keywords: Iterable[str] = (),
) -> tuple[ScriptAnalysis, list[WordTiming]]:
    """Words whose middle lies in [start, end), shifted so the clip starts at 0.

    ``keywords`` (case-insensitive) mark tokens as emphasised, which presets with emphasis
    enabled draw bigger or in the accent colour.
    """
    keys = {fold(k) for k in keywords if k and fold(k)}
    length = max(0.0, end - start)
    picked: list[Word] = []
    for w in words:
        mid = (w.start + w.end) / 2
        if start <= mid < end and w.text.strip():
            picked.append(w)

    tokens: list[Token] = []
    sentences: list[Sentence] = []
    timings: list[WordTiming] = []
    parts: list[str] = []
    pos = 0
    sent_idx = 0
    sent_first = 0
    for i, w in enumerate(picked):
        text = re.sub(r"\s+", " ", w.text.strip())
        if parts:
            pos += 1  # the joining space
        s_char, e_char = pos, pos + len(text)
        pos = e_char
        parts.append(text)
        norm = fold(text)
        tokens.append(Token(text=text, norm=norm, start_char=s_char, end_char=e_char,
                            sentence_idx=sent_idx, emphasis=bool(norm and norm in keys)))  # fmt: skip
        ws = min(length, max(0.0, w.start - start))
        we = min(length, max(ws + 0.02, w.end - start))
        timings.append(WordTiming(token_idx=i, text=text, start=round(ws, 3), end=round(we, 3),
                                  source="aligned"))  # fmt: skip
        nxt = picked[i + 1] if i + 1 < len(picked) else None
        if nxt is None or text.endswith(SENTENCE_END) or nxt.start - w.end >= SENTENCE_GAP:
            sentences.append(Sentence(
                idx=sent_idx, text=" ".join(parts[sent_first : i + 1]), paragraph_idx=0,
                token_start=sent_first, token_end=i + 1, ends_paragraph=nxt is None,
            ))  # fmt: skip
            sent_idx += 1
            sent_first = i + 1

    full = " ".join(parts)
    analysis = ScriptAnalysis(
        text=full, language_hint=detect_language(full) if full else "en", tokens=tokens,
        sentences=sentences, word_count=len(tokens), char_count=len(full),
        paragraph_count=1 if tokens else 0,
    )  # fmt: skip
    return analysis, timings
