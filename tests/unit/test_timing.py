import pytest

from snipmint.models import WordTiming
from snipmint.script import analyze_script
from snipmint.timing.estimate import estimate_timings, syllable_estimate
from snipmint.timing.native import map_words_to_tokens, native_timings
from snipmint.timing.quality import enforce_quality


def toks(text):
    return analyze_script(text).tokens


def assert_ordered(pairs, lo=None, hi=None):
    prev = lo if lo is not None else -1
    for s, e in pairs:
        assert s <= e and s >= prev - 1e-9
        prev = e
    if hi is not None:
        assert prev <= hi + 1e-9


# ------------------------------------------------------------------- native


def test_native_exact_match_ignores_punctuation_and_case():
    t = toks("Hello, World! It's fine.")
    ew = [("Hello", 0.1, 0.4), ("World", 0.5, 0.9), ("It's", 1.0, 1.2), ("fine", 1.3, 1.7)]
    out = map_words_to_tokens(t, ew, (0, 2))
    assert out == [(0.1, 0.4), (0.5, 0.9), (1.0, 1.2), (1.3, 1.7)]


def test_native_number_spoken_as_several_words():
    t = toks("In 2026 we ship.")
    ew = [("In", 0.0, 0.2), ("twenty", 0.3, 0.6), ("twenty", 0.6, 0.8), ("six", 0.8, 1.1),
          ("we", 1.2, 1.3), ("ship", 1.4, 1.8)]
    out = map_words_to_tokens(t, ew, (0, 2))
    assert out[0] == (0.0, 0.2)
    assert out[1][0] == pytest.approx(0.3) and out[1][1] == pytest.approx(1.1)  # "2026" spans them
    assert out[2] == (1.2, 1.3) and out[3] == (1.4, 1.8)


def test_native_unspoken_token_is_interpolated():
    t = toks("Pay $ now please.")
    ew = [("Pay", 0.0, 0.3), ("now", 0.8, 1.0), ("please", 1.1, 1.5)]
    # the lone "$" is punctuation so it attaches to "Pay"; five tokens -> four words
    t = toks("Pay five dollars now.")
    ew = [("Pay", 0.0, 0.3), ("now", 1.0, 1.3)]
    out = map_words_to_tokens(t, ew, (0, 1.5))
    assert out[0] == (0.0, 0.3) and out[3] == (1.0, 1.3)
    assert 0.3 <= out[1][0] < out[1][1] <= out[2][0] < out[2][1] <= 1.0
    assert_ordered(out, 0, 1.5)


def test_native_extra_spoken_words_extend_previous_token():
    t = toks("Hello there.")
    ew = [("Hello", 0.0, 0.3), ("uh", 0.35, 0.5), ("there", 0.6, 0.9)]
    out = map_words_to_tokens(t, ew, (0, 1))
    assert out[0][1] == pytest.approx(0.5)
    assert out[1] == (0.6, 0.9)


def test_native_no_engine_words_spreads_over_span():
    t = toks("one two three")
    out = map_words_to_tokens(t, [], (2.0, 3.0))
    assert out[0][0] == pytest.approx(2.0) and out[-1][1] == pytest.approx(3.0)
    assert_ordered(out, 2.0, 3.0)


def test_native_timings_offsets_trim_and_tempo():
    a = analyze_script("Hello world. Bye now.")
    # second sentence: tokens 2..3, clip starts at 5s, 0.1s trimmed from the clip, 2x tempo
    wt = native_timings(
        a.tokens[2:4], 2, [("Bye", 0.3, 0.5), ("now", 0.6, 0.9)],
        clip_start=5.0, trim_offset=0.1, tempo=2.0, clip_duration=0.5,
    )
    assert [w.token_idx for w in wt] == [2, 3]
    assert wt[0].start == pytest.approx(5.0 + 0.2 / 2.0)
    assert wt[1].end == pytest.approx(5.0 + 0.8 / 2.0)
    assert all(w.source == "native" for w in wt)
    assert wt[0].text == "Bye"


def test_native_timings_clamped_to_clip():
    a = analyze_script("Go.")
    wt = native_timings(a.tokens, 0, [("Go", 0.0, 9.0)], clip_start=1.0, trim_offset=0.0,
                        tempo=1.0, clip_duration=0.5)
    assert wt[0].end == pytest.approx(1.5)


# ----------------------------------------------------------------- estimate


@pytest.mark.parametrize("word,expected", [
    ("a", 1), ("the", 1), ("hello", 2), ("beautiful", 3), ("make", 1), ("table", 2),
    ("2026", 6), ("नमस्ते", 4),
])
def test_syllable_estimate(word, expected):
    assert syllable_estimate(word) == pytest.approx(expected, abs=1.01)
    assert syllable_estimate(word) >= 1


def test_estimate_fills_span_and_is_monotonic():
    t = toks("Wake up early, grind hard, and win.")
    out = estimate_timings(t, 10, (1.0, 4.0))
    assert [w.token_idx for w in out] == list(range(10, 10 + len(t)))
    assert out[0].start == pytest.approx(1.0)
    assert out[-1].end == pytest.approx(4.0)
    assert_ordered([(w.start, w.end) for w in out], 1.0, 4.0)
    assert all(w.source == "estimated" for w in out)


def test_estimate_gap_after_comma():
    t = toks("Wait, what")
    out = estimate_timings(t, 0, (0.0, 1.0))
    assert out[1].start > out[0].end  # pause after the comma


def test_estimate_single_word_and_empty():
    t = toks("Go")
    out = estimate_timings(t, 0, (0.5, 0.9))
    assert (out[0].start, out[0].end) == (pytest.approx(0.5), pytest.approx(0.9))
    assert estimate_timings([], 0, (0, 1)) == []


# ------------------------------------------------------------------ quality


def wt(i, s, e):
    return WordTiming(token_idx=i, text=str(i), start=s, end=e)


def test_quality_fixes_overlap_and_min_duration():
    words = [wt(0, 0.0, 0.5), wt(1, 0.4, 0.42), wt(2, 0.42, 1.0)]
    out = enforce_quality(words)
    for a, b in zip(out, out[1:], strict=False):
        assert a.end <= b.start + 1e-9
    assert all(w.end - w.start >= 0.06 - 1e-9 for w in out)


def test_quality_respects_sentence_limits():
    words = [wt(0, 0.0, 0.5), wt(1, 0.5, 0.52), wt(2, 1.0, 1.02)]
    limits = [(0.0, 0.55), (0.0, 0.55), (1.0, 1.5)]
    out = enforce_quality(words, limits)
    assert out[1].end <= 0.55 + 1e-9
    assert out[1].end - out[1].start >= 0.06 - 1e-9
    assert out[2].start >= 1.0
    for a, b in zip(out, out[1:], strict=False):
        assert a.end <= b.start + 1e-9


def test_quality_noop_on_clean_input():
    words = [wt(0, 0.0, 0.3), wt(1, 0.4, 0.8)]
    out = enforce_quality(words)
    assert [(w.start, w.end) for w in out] == [(0.0, 0.3), (0.4, 0.8)]
    assert enforce_quality([]) == []
