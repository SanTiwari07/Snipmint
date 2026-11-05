"""Gemini provider with a fake client: no network, no key."""

import json
import re
import types

import pytest

from snipmint.analyze import llm_pass, service
from snipmint.analyze.providers import gemini
from snipmint.analyze.schemas import AnalyzeOptions
from snipmint.analyze.signals import Signals
from snipmint.ingest.source import Source, SourceMeta

from .helpers import long_transcript


def _windows(prompt: str) -> list[dict]:
    blob = prompt.split("WINDOWS_JSON:\n", 1)[1].split("\n\nReturn only", 1)[0]
    return json.loads(blob)


class FakeModels:
    def __init__(self, block_ids=()):
        self.calls = 0
        self.block_ids = set(block_ids)

    def generate_content(self, model, contents, config):
        self.calls += 1
        schema = config["response_schema"] if isinstance(config, dict) else config.response_schema
        ws = _windows(contents)
        if self.block_ids & {w["id"] for w in ws}:
            fr = types.SimpleNamespace(name="PROHIBITED_CONTENT")
            return types.SimpleNamespace(prompt_feedback=None, candidates=[types.SimpleNamespace(finish_reason=fr)],
                                         parsed=None, text="")  # fmt: skip
        if schema.__name__ == "ScoreResponse":
            data = {"windows": [{"id": w["id"], "start": w["start"], "end": w["end"],
                                 "score": 95 if "mistake" in w["text"] else 10, "reason": "x"} for w in ws]}  # fmt: skip
        else:
            data = {"shorts": [{
                "start": w["start"] + 1, "end": w["start"] + 25, "source_window_id": w["id"],
                "predicted_score": 90 if "mistake" in w["text"] else 40,
                "video_description_for_tiktok": "tease #a", "video_description_for_instagram": "tease #b",
                "video_title_for_youtube_short": f"Title {w['id']}", "viral_hook_text": "The biggest mistake",
                "why": "Names a costly mistake", "keywords": ["mistake", "founders"],
            } for w in ws]}  # fmt: skip
        return types.SimpleNamespace(prompt_feedback=None, candidates=[], parsed=None, text=json.dumps(data))


def _meta(t):
    return SourceMeta(id="yt-test", url="u", title="Founder talk", duration=t.duration)


def test_gemini_two_pass_returns_written_copy():
    t = long_transcript(200, hook_at=120)
    fake = types.SimpleNamespace(models=FakeModels())
    moments = gemini.pick_moments(t, Signals(), _meta(t), AnalyzeOptions(min_seconds=15, max_seconds=30, count=3),
                                  client=fake)  # fmt: skip
    assert len(moments) == 3
    best = moments[0]
    assert best.provider == "gemini" and best.score == 90
    assert best.title.startswith("Title ") and best.hook_text == "The biggest mistake"
    assert best.keywords == ["mistake", "founders"]
    assert best.descriptions["tiktok"] == "tease #a"
    hook_time = t.segments[120].start
    window = next(w for w in llm_pass.build_windows(t, 90) if best.title == f"Title {w['id']}")
    assert window["start"] <= hook_time <= window["end"]
    for a, b in zip(moments, moments[1:], strict=False):
        assert not (a.start < b.end and b.start < a.end and min(a.end, b.end) - max(a.start, b.start) > 0.25 * 24)


def test_blocked_window_is_isolated_not_fatal():
    t = long_transcript(200, hook_at=120)
    fake = types.SimpleNamespace(models=FakeModels(block_ids={"w001"}))
    moments = gemini.pick_moments(t, Signals(), _meta(t), AnalyzeOptions(min_seconds=15, max_seconds=30, count=2),
                                  client=fake)  # fmt: skip
    assert moments


def test_service_falls_back_and_never_echoes_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path))
    t = long_transcript(120, hook_at=60)
    src = Source("yt-BBBBBBBBBBB")
    src.save_meta(_meta(t))
    src.write_json("transcript.json", t.model_dump())

    def boom(_key):
        raise RuntimeError("400 API key not valid: AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE123")

    monkeypatch.setattr(gemini, "make_client", boom)
    res = service.analyze(src, AnalyzeOptions(provider="gemini", min_seconds=15, max_seconds=30, count=2),
                          gemini_key="AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE123")  # fmt: skip
    assert res.provider == "heuristic" and res.fallback_reason
    assert all(m.fallback for m in res.moments)
    assert "AIza" not in res.fallback_reason
    for f in tmp_path.rglob("*.json"):
        assert "AIzaSyFAKE" not in f.read_text(encoding="utf-8"), f


def test_redact_and_transient_retry():
    assert "AIza" in gemini.redact("x AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ y")
    assert "ABCDEFGHIJKLMNOP" not in gemini.redact("x AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ y")

    attempts = {"n": 0}

    class Flaky:
        def generate_content(self, model, contents, config):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("503 UNAVAILABLE")
            return types.SimpleNamespace(prompt_feedback=None, candidates=[], parsed=None,
                                         text='{"windows": []}')  # fmt: skip

    call = gemini.make_call(types.SimpleNamespace(models=Flaky()), "m", sleep=lambda s: None)
    from snipmint.analyze.prompts import ScoreResponse

    assert call("p", ScoreResponse) == {"windows": []}
    assert attempts["n"] == 3


def test_build_windows_cover_the_transcript():
    t = long_transcript(150, hook_at=10)
    ws = llm_pass.build_windows(t, 90)
    assert ws[0]["start"] == t.segments[0].start
    assert ws[-1]["end"] == t.segments[-1].end
    assert all(re.fullmatch(r"w\d{3}", w["id"]) for w in ws)


@pytest.mark.parametrize("n,size", [(10, 8), (16, 8), (3, 8), (1, 3)])
def test_score_batches_are_near_equal(n, size):
    batches = llm_pass.score_batches(list(range(n)), size)
    assert sum(len(b) for b in batches) == n
    assert max(len(b) for b in batches) - min(len(b) for b in batches) <= 1


# ---------------------------------------------------------------------- proofreading + silent videos


class ScriptedModels:
    """Answers each schema with a fixed payload and records what it was sent."""

    def __init__(self, answers):
        self.answers = answers
        self.contents = []

    def generate_content(self, model, contents, config):
        self.contents.append(contents)
        schema = config["response_schema"] if isinstance(config, dict) else config.response_schema
        return types.SimpleNamespace(prompt_feedback=None, candidates=[], parsed=None,
                                     text=json.dumps(self.answers[schema.__name__]))  # fmt: skip


def test_proofread_accepts_only_clean_single_word_swaps():
    from snipmint.analyze.schemas import Moment
    from snipmint.transcribe.models import Segment, Transcript, Word

    words = [Word(text=f" {w}", start=i, end=i + 0.5) for i, w in enumerate(["I", "met", "Kenu", "Reeves", "at", "the", "fern", "show"])]
    t = Transcript(language="en", duration=10, segments=[Segment(start=0, end=10, text="x", words=words)])
    fixes = {"fixes": [{"i": 2, "text": "Keanu"}, {"i": 7, "text": "Between Two Ferns"},
                       {"i": 99, "text": "x"}, {"i": 3, "text": "Reeves"}]}  # fmt: skip
    client = types.SimpleNamespace(models=ScriptedModels({"FixResponse": fixes}))
    m = Moment(id="m01", start=0, end=10, score=50)
    assert gemini.proofread(client, "m", t, [m], "Keanu Reeves interview") == {2: "Keanu"}


def test_service_applies_fixes_to_later_captions(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path))
    t = long_transcript(120, hook_at=60)
    src = Source("yt-DDDDDDDDDDD")
    src.save_meta(_meta(t))
    src.write_json("transcript.json", t.model_dump())
    fake = types.SimpleNamespace(models=FakeModels())
    real_generate = fake.models.generate_content

    def generate(model, contents, config):
        schema = config["response_schema"] if isinstance(config, dict) else config.response_schema
        if schema.__name__ == "FixResponse":
            first = json.loads(contents.split("CLIPS_JSON:\n", 1)[1])[0][0]
            return types.SimpleNamespace(prompt_feedback=None, candidates=[], parsed=None,
                                         text=json.dumps({"fixes": [{"i": first[0], "text": "Gemini!"}]}))  # fmt: skip
        return real_generate(model, contents, config)

    fake.models.generate_content = generate
    monkeypatch.setattr(gemini, "make_client", lambda key: fake)
    res = service.analyze(src, AnalyzeOptions(provider="gemini", min_seconds=15, max_seconds=30, count=2),
                          gemini_key="k", gemini_model="gemini-test")  # fmt: skip
    assert res.provider == "gemini"
    fixed = service.load_transcript(src)
    assert sum(w.text.strip() == "Gemini!" for w in fixed.words) == 1
    assert "Gemini!" in res.moments[0].text or "Gemini!" in " ".join(m.text for m in res.moments)
    # the raw transcript on disk is untouched
    assert "Gemini!" not in src.file("transcript.json").read_text(encoding="utf-8")


def test_silent_video_is_watched_by_gemini(tmp_path, monkeypatch):
    from snipmint.audio import ffmpeg as ff
    from snipmint.transcribe.models import Transcript

    if not ff.available():
        pytest.skip("ffmpeg needed")
    video = tmp_path / "source.mp4"
    ff.run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=6", "-pix_fmt", "yuv420p", str(video)])
    meta = SourceMeta(id="up-x", url="file:x.mp4", title="Skate tricks", duration=120)
    answer = {"shorts": [
        {"start": 10, "end": 30, "predicted_score": 88, "video_title_for_youtube_short": "Kickflip",
         "viral_hook_text": "Wait for it", "why": "Clean landing"},
        {"start": 12, "end": 28, "predicted_score": 70},  # overlaps the first: dropped
        {"start": 60, "end": 90, "predicted_score": 60, "video_title_for_youtube_short": "Grind"},
    ]}  # fmt: skip
    models = ScriptedModels({"VisualResponse": answer})
    deleted = []
    files = types.SimpleNamespace(
        upload=lambda file: types.SimpleNamespace(name="files/abc"),
        get=lambda name: types.SimpleNamespace(state=types.SimpleNamespace(name="ACTIVE")),
        delete=lambda name: deleted.append(name),
    )
    client = types.SimpleNamespace(models=models, files=files)
    moments = gemini.pick_moments(Transcript(language="en", duration=120), Signals(), meta,
                                  AnalyzeOptions(min_seconds=15, max_seconds=30, count=3), client=client,
                                  video=video)  # fmt: skip
    assert [m.title for m in moments] == ["Kickflip", "Grind"]
    assert deleted == ["files/abc"]
    assert isinstance(models.contents[0], list) and models.contents[0][0].name == "files/abc"
    assert (tmp_path / "gemini_upload.mp4").is_file()

    long_meta = meta.model_copy(update={"duration": 3 * 3600})
    with pytest.raises(Exception, match="up to 45 minutes"):
        gemini.visual_moments(client, "m", video, long_meta, AnalyzeOptions())
