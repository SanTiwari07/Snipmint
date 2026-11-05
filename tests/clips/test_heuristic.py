from snipmint.analyze.providers.heuristic import hook_strength, pick_moments
from snipmint.analyze.schemas import AnalyzeOptions
from snipmint.analyze.signals import Signals, heatmap_curve
from snipmint.ingest.source import HeatPoint, SourceMeta

from .helpers import long_transcript, make_transcript


def _meta(t, **kw):
    return SourceMeta(
        id="yt-test", url="https://www.youtube.com/watch?v=xxxxxxxxxxx",
        title="Test video", duration=t.duration, **kw,
    )  # fmt: skip


def test_hook_strength_prefers_questions_and_you():
    strong = hook_strength("Why do you never see the biggest mistake?")
    assert strong > hook_strength("So um and then we went home.")
    assert hook_strength("") == 0


def test_planted_hook_wins_without_heatmap():
    t = long_transcript(120, hook_at=60)
    hook_time = t.segments[60].start
    opts = AnalyzeOptions(min_seconds=15, max_seconds=30, count=3)
    moments = pick_moments(t, Signals(), _meta(t), opts)
    assert len(moments) == 3
    best = moments[0]
    assert best.start <= hook_time + 0.5 and best.end > hook_time
    assert "mistake" in best.hook_text.lower() or "why" in best.hook_text.lower()
    assert best.provider == "heuristic" and best.why


def test_heatmap_pulls_selection():
    t = long_transcript(120, hook_at=5)
    peak = t.segments[90].start
    heat = [
        HeatPoint(start=s, end=s + 5, value=1.0 if peak - 5 <= s <= peak + 25 else 0.05)
        for s in range(0, int(t.duration), 5)
    ]
    meta = _meta(t, heatmap=heat)
    signals = Signals(heatmap=heatmap_curve(meta))
    opts = AnalyzeOptions(min_seconds=15, max_seconds=30, count=2)
    moments = pick_moments(t, signals, meta, opts)
    assert any(m.start <= peak + 25 and m.end >= peak for m in moments)
    assert any("replayed" in m.why.lower() for m in moments)


def test_no_overlap_and_deterministic():
    t = long_transcript(150, hook_at=70)
    opts = AnalyzeOptions(min_seconds=15, max_seconds=30, count=5)
    a = pick_moments(t, Signals(), _meta(t), opts)
    b = pick_moments(t, Signals(), _meta(t), opts)
    assert [(m.start, m.end) for m in a] == [(m.start, m.end) for m in b]
    spans = sorted((m.start, m.end) for m in a)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:], strict=False):
        inter = max(0, min(e1, e2) - max(s1, s2))
        assert inter <= 0.15 * min(e1 - s1, e2 - s2) + 1e-6


def test_silent_video_falls_back_to_even_spread():
    t = make_transcript(["Music."])
    meta = SourceMeta(id="yt-test", url="u", title="Beats", duration=600)
    opts = AnalyzeOptions(min_seconds=30, max_seconds=60, count=3)
    moments = pick_moments(t, Signals(), meta, opts)
    assert len(moments) == 3
    assert all(m.end - m.start == 45 for m in moments)


def test_silent_short_video_still_gets_the_requested_clips():
    t = make_transcript(["Hm."])
    meta = SourceMeta(id="up-x", url="file:x.mp4", title="Tone", duration=40)
    moments = pick_moments(t, Signals(), meta, AnalyzeOptions(min_seconds=10, max_seconds=20, count=2))
    assert [(m.start, m.end) for m in sorted(moments, key=lambda m: m.start)] == [(0.0, 15.0), (25.0, 40.0)]
