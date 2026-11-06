"""Phase 3: per-clip metadata (fallback + mocked Gemini), uniqueness, edits and regeneration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from snipmint.ingest.source import Source, SourceMeta
from snipmint.jobs.runner import JobRunner
from snipmint.metadata import fallback, generator
from snipmint.metadata.schemas import FIELDS, ClipMetadata
from snipmint.server import create_app
from tests.clips.helpers import make_transcript

SID = "yt-BBBBBBBBBBB"
SENTENCES = [
    "Why do most people never save any money at all?",
    "Because nobody teaches budgeting in school.",
    "Start by tracking every expense for one month.",
    "Then move ten percent of your salary into savings first.",
    "Compound interest does the rest over twenty years.",
    "Investing early beats investing big, every single time.",
    "My first salary was small, but I still saved some of it.",
    "Debt is the real enemy of saving money.",
]


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))
    src = Source(SID)
    src.save_meta(SourceMeta(id=SID, url="https://www.youtube.com/watch?v=BBBBBBBBBBB", video_id="BBBBBBBBBBB",
                             title="Money Talks Podcast #12", channel="Money Talks", duration=40.0))  # fmt: skip
    src.write_json("transcript.json", json.loads(make_transcript(SENTENCES).model_dump_json()))
    return src


@pytest.fixture
def client(tmp_path, source):
    with TestClient(create_app(runner=JobRunner(tmp_path / "jobs"), static_dir=tmp_path / "x")) as c:
        yield c


CLIPS = [{"clip_id": "m01", "start": 0.0, "end": 12.0}, {"clip_id": "m02", "start": 12.0, "end": 26.0},
         {"clip_id": "m03", "start": 0.0, "end": 12.0}]  # fmt: skip


def test_fallback_uses_only_transcript_words(source):
    ctx = generator.context_for(source, "m01", 0, 12)
    meta = fallback.build(ctx, "en")
    ClipMetadata.model_validate(meta.model_dump())
    assert len(meta.title_variants) == 3 and all(v.reason for v in meta.title_variants)
    assert 3 <= len(meta.hashtags) <= 5 and meta.hashtags[0] == "#Shorts"
    assert meta.title.endswith("?")  # the question line wins
    words = set(fallback.tokens(ctx.text + " " + ctx.video_title))
    for tag in meta.tags:
        assert set(fallback.tokens(tag)) <= words, f"tag '{tag}' is not from the clip"
    assert meta.hook in ctx.text or meta.hook.rstrip("…") in ctx.text
    # deterministic
    assert fallback.build(ctx, "en") == meta


def test_generate_api_unique_titles_and_languages(client):
    r = client.post(f"/api/metadata/{SID}/generate", json={"clips": CLIPS, "language": "hi"})
    assert r.status_code == 200, r.text
    clips = r.json()["clips"]
    titles = [clips[c["clip_id"]]["metadata"]["title"] for c in CLIPS]
    assert len({generator.norm_title(t) for t in titles}) == 3, titles
    rec = clips["m01"]
    assert rec["provider"] == "fallback" and set(rec["origin"].values()) == {"fallback"}
    assert rec["metadata"]["language"] == "hi"
    assert "बताइए" in rec["metadata"]["pinned_comment"]
    assert "translate" in (rec["note"] or "")
    opts = client.get("/api/metadata/options").json()
    assert {"en", "hi", "hinglish", "mr"} <= {lang["code"] for lang in opts["languages"]}
    assert client.post(f"/api/metadata/{SID}/generate", json={"clips": CLIPS, "language": "xx"}).status_code == 422


def test_edit_marks_field_and_regenerate_keeps_it(client):
    client.post(f"/api/metadata/{SID}/generate", json={"clips": CLIPS[:2], "language": "en"})
    edited = client.put(f"/api/metadata/{SID}/m01", json={"changes": {"description": "My own words."}}).json()
    assert edited["origin"]["description"] == "edited"
    # regenerate one other field: description untouched
    hook = client.post(f"/api/metadata/{SID}/m01/regenerate", json={"field": "hook"}).json()
    assert hook["metadata"]["description"] == "My own words."
    # regenerate everything: edited field still kept
    allf = client.post(f"/api/metadata/{SID}/m01/regenerate", json={}).json()
    assert allf["metadata"]["description"] == "My own words." and allf["origin"]["description"] == "edited"
    # explicitly regenerating the edited field replaces it
    desc = client.post(f"/api/metadata/{SID}/m01/regenerate", json={"field": "description"}).json()
    assert desc["metadata"]["description"] != "My own words." and desc["origin"]["description"] == "fallback"


def test_regenerate_title_rotates_and_stays_unique(client):
    client.post(f"/api/metadata/{SID}/generate", json={"clips": CLIPS[:2], "language": "en"})
    before = client.get(f"/api/metadata/{SID}").json()["clips"]
    t1 = before["m01"]["metadata"]["title"]
    after = client.post(f"/api/metadata/{SID}/m01/regenerate", json={"field": "title"}).json()
    assert after["metadata"]["title"] != t1
    assert after["metadata"]["description"] == before["m01"]["metadata"]["description"]
    other = before["m02"]["metadata"]["title"]
    dup = client.put(f"/api/metadata/{SID}/m01", json={"changes": {"title": other.upper()}})
    assert dup.status_code == 409


def test_edit_validation(client):
    client.post(f"/api/metadata/{SID}/generate", json={"clips": CLIPS[:1], "language": "en"})
    too_long = client.put(f"/api/metadata/{SID}/m01", json={"changes": {"title": "x" * 101}})
    assert too_long.status_code == 422
    few = client.put(f"/api/metadata/{SID}/m01", json={"changes": {"hashtags": ["#one"]}})
    assert few.status_code == 422
    ok = client.put(f"/api/metadata/{SID}/m01", json={"changes": {"hashtags": ["one", "#two", "three four"]}})
    assert ok.json()["metadata"]["hashtags"] == ["#one", "#two", "#threefour"]
    assert client.put(f"/api/metadata/{SID}/m01", json={"changes": {"bogus": 1}}).status_code == 422


class FakeModels:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    def generate_content(self, model, contents, config):
        self.prompts.append(contents)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(parsed=None, text=json.dumps(payload), prompt_feedback=None, candidates=[])


DRAFT = {
    "title_variants": [{"title": "Why saving feels impossible", "reason": "curiosity"},
                       {"title": "Track every expense for a month", "reason": "actionable"},
                       {"title": "Pay yourself first", "reason": "short"}],
    "chosen_index": 1, "title_reason": "Concrete and actionable.",
    "description": "How to start saving: track expenses, save ten percent first.",
    "hashtags": ["Shorts", "#Saving", "#Money money"], "tags": ["saving", "budget"],
    "hook": "Why do most people never save?", "pinned_comment": "What's your first saving tip?",
    "category_id": "27", "seo_keywords": ["how to save money"],
}  # fmt: skip


def test_gemini_path_validated_and_normalised(source):
    models = FakeModels([DRAFT])
    client = SimpleNamespace(models=models)
    ctx = generator.context_for(source, "m01", 0, 12)
    rec = generator.generate(source, ctx, "hinglish", client=client)
    assert rec.provider == "gemini" and set(rec.origin.values()) == {"ai"}
    assert rec.metadata.title == "Track every expense for a month"
    assert rec.metadata.hashtags == ["#Shorts", "#Saving", "#Moneymoney"]
    assert rec.metadata.category == "Education"
    prompt = models.prompts[0]
    assert "Never invent" in prompt and "Hinglish" in prompt and "Why do most people never save" in prompt


def test_gemini_failure_falls_back_with_reason(source):
    client = SimpleNamespace(models=FakeModels([ValueError("bad request AIzaSyDUMMYDUMMYDUMMYDUMMY1234")]))
    ctx = generator.context_for(source, "m01", 0, 12)
    rec = generator.generate(source, ctx, "en", client=client)
    assert rec.provider == "fallback" and "free metadata builder" in rec.note
    assert "AIzaSy" not in rec.note


def test_gemini_bad_shape_falls_back(source):
    bad = {**DRAFT, "title_variants": DRAFT["title_variants"][:1]}
    rec = generator.generate(source, generator.context_for(source, "m01", 0, 12), "en",
                             client=SimpleNamespace(models=FakeModels([bad])))  # fmt: skip
    assert rec.provider == "fallback"


def test_every_field_can_be_regenerated(source):
    ctx = generator.context_for(source, "m01", 0, 12)
    generator.generate(source, ctx, "en")
    for f in FIELDS:
        rec = generator.generate(source, ctx, "en", fields=[f])
        ClipMetadata.model_validate(rec.metadata.model_dump())


def test_no_speech_clip_is_refused(source):
    with pytest.raises(generator.MetadataError):
        generator.context_for(source, "m09", 100, 120)
