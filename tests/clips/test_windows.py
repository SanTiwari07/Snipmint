from snipmint.analyze.windows import (
    auto_clip_count,
    candidate_spans,
    overlap_ratio,
    snap_to_words,
    split_sentences,
)

from .helpers import make_transcript


def test_split_sentences_on_punctuation_and_pauses():
    t = make_transcript(["Hello there.", "How are you?", "Fine"])
    sents = split_sentences(t.words)
    assert [s.text for s in sents] == ["Hello there.", "How are you?", "Fine"]
    assert sents[1].first == 2 and sents[1].last == 4


def test_candidate_spans_respect_band():
    t = make_transcript([f"Sentence number {i} is here." for i in range(40)])
    sents = split_sentences(t.words)
    spans = candidate_spans(sents, 10, 20)
    assert spans
    assert all(10 <= s.duration <= 20 for s in spans)
    assert all(s.start == sents[s.first_sentence].start for s in spans)


def test_snap_to_words_lands_in_silence():
    t = make_transcript(["one two three.", "four five six."], word_dur=0.5, gap=1.0)
    start, end = snap_to_words(0.6, 3.4, t.words, 10)
    assert start <= 0.5
    assert end >= 1.5


def test_overlap_and_counts():
    assert overlap_ratio((0, 10), (5, 15)) == 0.5
    assert overlap_ratio((0, 10), (20, 30)) == 0
    assert 3 <= auto_clip_count(60) <= auto_clip_count(3600) <= 10
