"""Removing the downloaded video once the clips are made (storage management)."""

from __future__ import annotations

import pytest

from snipmint import config
from snipmint.errors import IngestError
from snipmint.ingest.source import Source, SourceMeta
from snipmint.pipeline.source import ensure_video


@pytest.fixture
def source(tmp_path, monkeypatch) -> Source:
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path))
    src = Source("abc123")
    src.video.write_bytes(b"v" * 1000)
    src.audio.write_bytes(b"a" * 200)
    src.file("source.f137.mp4.part").write_bytes(b"p" * 50)
    src.write_json("transcript.json", {"words": []})
    src.write_json("signals.json", {})
    src.write_json("moments.latest.json", {"moments": []})
    return src


def test_delete_media_keeps_the_analysis(source: Source):
    freed = source.delete_media()
    assert freed == 1250
    assert not source.video.exists() and not source.audio.exists()
    assert not source.file("source.f137.mp4.part").exists()
    for kept in ("transcript.json", "signals.json", "moments.latest.json"):
        assert source.file(kept).is_file()


def test_delete_media_twice_is_harmless(source: Source):
    source.delete_media()
    assert source.delete_media() == 0


def test_delete_media_waits_for_a_running_prepare(source: Source):
    with source.lock:  # prepare_source holds this lock while it works
        assert source.delete_media() == 0
    assert source.video.is_file()


@pytest.mark.parametrize("value,expected", [(None, True), ("1", True), ("0", False), ("false", False),
                                            ("Off", False), ("yes", True)])
def test_setting(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path))
    if value is None:
        monkeypatch.delenv("SNIPMINT_DELETE_SOURCE", raising=False)
    else:
        monkeypatch.setenv("SNIPMINT_DELETE_SOURCE", value)
    assert config.delete_source_after_clips() is expected


def test_ensure_video_is_a_noop_when_present(source: Source):
    ensure_video(source)  # would raise or download if it tried


def test_ensure_video_asks_for_a_new_upload_when_the_file_came_from_the_computer(source: Source):
    source.save_meta(SourceMeta(id=source.id, url="file:talk.mp4", title="talk"))
    source.delete_media()
    with pytest.raises(IngestError, match="Upload it again"):
        ensure_video(source)


def test_ensure_video_downloads_a_link_again(source: Source, monkeypatch):
    source.save_meta(SourceMeta(id=source.id, url="https://www.youtube.com/watch?v=_c2e1Ncl3Sc", title="t"))
    source.delete_media()
    calls = []

    def fake_download(url, dest, *, progress=None, cancel=None):
        calls.append(url)
        dest.write_bytes(b"new")
        return dest

    monkeypatch.setattr("snipmint.pipeline.source.youtube.download", fake_download)
    ensure_video(source)
    assert calls == ["https://www.youtube.com/watch?v=_c2e1Ncl3Sc"] and source.video.read_bytes() == b"new"
