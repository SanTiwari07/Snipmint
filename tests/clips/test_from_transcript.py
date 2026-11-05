from snipmint.captions.ass import build_captions
from snipmint.captions.formats import get_format
from snipmint.captions.from_transcript import clip_words
from snipmint.captions.styles import list_styles, resolve_style

from .helpers import make_transcript


def test_words_are_shifted_and_clamped():
    t = make_transcript(["Hello there friend.", "This is a test."], word_dur=0.5, gap=0.5)
    analysis, words = clip_words(t.words, 1.0, 4.0)
    assert words[0].text == "friend."  # "there" (0.5-1.0) has its middle before 1.0
    assert all(0 <= w.start < w.end <= 3.0 for w in words)
    assert [tok.text for tok in analysis.tokens] == [w.text for w in words]
    assert len(analysis.sentences) == 2
    assert analysis.sentences[0].text == "friend."


def test_keywords_mark_emphasis():
    t = make_transcript(["Money is not the goal."])
    analysis, _ = clip_words(t.words, 0, 10, keywords=["MONEY", "goal"])
    flags = {tok.norm: tok.emphasis for tok in analysis.tokens}
    assert flags["money"] and flags["goal"] and not flags["not"]


def test_every_preset_builds_ass_from_speech():
    t = make_transcript(["Why do you never see it?", "Because nobody tells you the truth."])
    analysis, words = clip_words(t.words, 0, t.duration, keywords=["truth"])
    fmt = get_format("reels")
    for style in list_styles():
        resolved = resolve_style(style.id, {}, None)
        cap = build_captions(words, analysis, resolved, fmt)
        assert "[Events]" in cap.ass and "Dialogue:" in cap.ass, style.id
