"""Reframing: the camera, the subject picker, layouts as ffmpeg filters, black bars."""

import json
import subprocess

import pytest

from snipmint.audio import ffmpeg as ff
from snipmint.captions.formats import get_format
from snipmint.ingest.source import Source
from snipmint.reframe import faces as faces_mod
from snipmint.reframe import framing
from snipmint.reframe.camera import JUMP_CONFIRM, SmoothedCamera, SubjectPicker
from snipmint.reframe.faces import Face
from snipmint.reframe.framing import FramingPlan, Region
from snipmint.reframe.media import detect_letterbox, video_size
from snipmint.render.ffmpeg_render import RenderSpec, render

needs_ffmpeg = pytest.mark.skipif(not (ff.available() and ff.has_libass()), reason="ffmpeg with libass needed")


def face(cx, w=100, cy=300):
    return Face(cx - w / 2, cy - w / 2, w, w, 0.9)


# ---------------------------------------------------------------------- camera


def test_camera_snaps_first_then_holds_inside_safe_zone():
    cam = SmoothedCamera(crop_w=600, frame_w=1920)
    cam.update_target(1000)
    assert cam.step() == 700  # snapped: centred on 1000
    cam.update_target(1100)  # inside the 150 px safe zone
    for _ in range(30):
        cam.step()
    assert cam.current == 1000


def test_camera_ignores_a_single_jump_but_follows_a_repeated_one():
    cam = SmoothedCamera(crop_w=600, frame_w=1920)
    cam.update_target(500)
    cam.step()
    cam.update_target(1500)  # one outlier
    cam.update_target(520)
    assert cam.target == 520
    for _ in range(JUMP_CONFIRM):
        cam.update_target(1500)
    assert cam.target == 1500
    before = cam.current
    cam.step()
    assert cam.current > before  # pans, does not teleport
    assert cam.current - before <= cam.fast


def test_camera_cuts_at_scene_change_and_stays_in_frame():
    cam = SmoothedCamera(crop_w=600, frame_w=1920)
    cam.update_target(300)
    cam.step()
    cam.begin_scene()
    cam.update_target(1700)
    assert cam.step() == 1920 - 600  # clamped to the right edge, no pan


def test_subject_picker_sticks_then_switches_to_a_dominant_face():
    p = SubjectPicker(crop_w=600, switch_after=3)
    a, b = face(400, 100), face(1500, 120)
    assert p.pick([a, b]).cx == 1500  # first pick: biggest
    small_b, big_a = face(1500, 100), face(400, 200)
    assert p.pick([big_a, small_b]).cx == 1500  # sticks for now
    assert p.pick([big_a, small_b]).cx == 1500
    assert p.pick([big_a, small_b]).cx == 400  # confirmed: switch


# ---------------------------------------------------------------------- filters


def test_track_filter_uses_sendcmd_and_freezes_for_previews():
    plan = FramingPlan(layout="track", src_w=1920, src_h=1080, box=Region(x=0, y=140, w=1920, h=800),
                       crop_w=450, crop_h=800, crop_y=0, xs=[(0.0, 100), (1.5, 140), (3.0, 180)])  # fmt: skip
    vf = plan.video_filter(1080, 1920, cmd_file="clip_01.cmd")
    assert vf.startswith("crop=1920:800:0:140,sendcmd=f=clip_01.cmd,crop@c=w=450:h=800:x=100:y=0")
    assert vf.endswith("scale=1080:1920,setsar=1")
    assert plan.sendcmd() == "1.500 crop@c x 140;\n3.000 crop@c x 180;\n"
    frozen = plan.video_filter(1080, 1920, cmd_file="clip_01.cmd", at=2.0)
    assert "sendcmd" not in frozen and "x=140" in frozen


def test_split_and_fit_filters():
    split = FramingPlan(layout="split", src_w=1920, src_h=1080,
                        regions=[Region(x=0, y=0, w=900, h=800), Region(x=1000, y=0, w=900, h=800)])  # fmt: skip
    vf = split.video_filter(1080, 1920)
    assert "split=2" in vf and "vstack" in vf and vf.count("scale=1080:960") == 2
    fit = FramingPlan(layout="fit", src_w=1920, src_h=1080).video_filter(1080, 1920)
    assert "gblur" in fit and "overlay" in fit


def test_window_fits_inside_content():
    assert framing._window(1920, 1080, 9 / 16) == (606, 1080)
    assert framing._window(1080, 1920, 9 / 16) == (1080, 1920)
    w, h = framing._window(1920, 800, 1.0)
    assert (w, h) == (800, 800)


def test_split_regions_from_two_faces():
    samples = [[face(500, 160), face(1400, 150)] for _ in range(5)]
    regions = framing._split_regions(samples, 1920, 1080, 1080 / 960)
    assert regions and regions[0].x < regions[1].x
    for r in regions:
        assert r.x >= 0 and r.x + r.w <= 1920 and r.y + r.h <= 1080
        assert abs(r.w / r.h - 1080 / 960) < 0.02


# ---------------------------------------------------------------------- with ffmpeg


@pytest.fixture
def letterboxed(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))
    src = Source("yt-CCCCCCCCCCC")
    ff.run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=4",
        "-vf", "pad=640:480:0:60:black", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(src.video),
    ])  # fmt: skip
    return src


@needs_ffmpeg
def test_detects_black_bars(letterboxed):
    box = detect_letterbox(letterboxed.video, 0, 4, video_size(letterboxed.video))
    assert box is not None
    assert (box.w, box.h, box.y) == (640, 360, 60)


@needs_ffmpeg
def test_plan_without_face_detection_still_removes_bars(letterboxed, monkeypatch):
    monkeypatch.setattr(faces_mod, "available", lambda: False)
    fmt = get_format("reels")
    plan = framing.plan_framing(letterboxed, 0, 4, fmt, "auto")
    assert plan.layout == "center" and plan.box is not None and plan.box.h == 360
    fit = framing.plan_framing(letterboxed, 0, 4, fmt, "fit")
    assert fit.layout == "fit"
    # cached on disk
    assert any(letterboxed.path.joinpath("framing").iterdir())


@needs_ffmpeg
def test_moving_crop_renders(letterboxed, tmp_path):
    plan = FramingPlan(layout="track", src_w=640, src_h=480, box=Region(x=0, y=60, w=640, h=360),
                       crop_w=202, crop_h=360, xs=[(0.0, 0), (1.0, 200), (2.0, 438)])  # fmt: skip
    job = tmp_path / "job"
    job.mkdir()
    (job / "c.cmd").write_text(plan.sendcmd(), encoding="utf-8")
    (job / "empty.ass").write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
    (job / "fonts").mkdir()
    spec = RenderSpec(width=540, height=960, fps=30, duration=3.0, background="video",
                      background_file=str(letterboxed.video), background_start=0.5, audio=None,
                      audio_from_background=True, ass="empty.ass", quality="fast",
                      video_filter=plan.video_filter(540, 960, cmd_file="c.cmd"))  # fmt: skip
    out = render(job, spec, "out.mp4")
    probe = subprocess.run([ff.find_ffprobe(), "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-of", "json", str(out)], capture_output=True, check=True)  # fmt: skip
    streams = json.loads(probe.stdout)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (540, 960)
    assert any(s["codec_type"] == "audio" for s in streams)


# ---------------------------------------------------------------------- mixed layout


def test_two_person_shots_follow_scene_cuts():
    two = [face(500, 160), face(1400, 150)]
    one = [face(900, 300)]
    samples = [(t / 5, two if t / 5 < 4 or t / 5 >= 8 else one) for t in range(60)]  # 12 s
    shots = framing._two_person_shots(samples, [4.0, 8.0], 12.0, crop_w=606)
    assert shots == [(0.0, 4.0), (8.0, 12.0)]
    shots = framing._two_person_shots(samples, [4.0, 6.0, 8.0], 12.0, crop_w=606)
    assert shots == [(0.0, 4.0), (8.0, 12.0)]


def test_mixed_filter_overlays_split_during_two_shots():
    plan = FramingPlan(layout="mixed", src_w=1920, src_h=1080, crop_w=606, crop_h=1080,
                       xs=[(0.0, 600), (5.0, 700)], split_ranges=[(0.0, 4.0), (8.0, 12.0)],
                       regions=[Region(x=0, y=0, w=900, h=800), Region(x=1000, y=0, w=900, h=800)])  # fmt: skip
    vf = plan.video_filter(1080, 1920, cmd_file="c.cmd")
    assert "sendcmd=f=c.cmd" in vf and "vstack" in vf
    assert "overlay=enable='between(t,0.000,3.999)+between(t,8.000,11.999)'" in vf
    assert vf.count("[ma]") == 2 and "[qa]" in vf  # inner labels don't collide
    assert plan.layout_at(1.0) == "split" and plan.layout_at(5.0) == "track"
    assert "vstack" in plan.video_filter(1080, 1920, at=1.0)
    assert "vstack" not in plan.video_filter(1080, 1920, at=5.0)


@needs_ffmpeg
def test_mixed_layout_renders(letterboxed, tmp_path):
    plan = FramingPlan(layout="mixed", src_w=640, src_h=480, box=Region(x=0, y=60, w=640, h=360),
                       crop_w=202, crop_h=360, xs=[(0.0, 0), (1.0, 200)], split_ranges=[(0.0, 1.5)],
                       regions=[Region(x=0, y=0, w=300, h=266), Region(x=320, y=0, w=300, h=266)])  # fmt: skip
    job = tmp_path / "job"
    job.mkdir()
    (job / "c.cmd").write_text(plan.sendcmd(), encoding="utf-8")
    (job / "empty.ass").write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
    (job / "fonts").mkdir()
    spec = RenderSpec(width=540, height=960, fps=30, duration=3.0, background="video",
                      background_file=str(letterboxed.video), background_start=0.0, audio=None,
                      audio_from_background=True, ass="empty.ass", quality="fast",
                      video_filter=plan.video_filter(540, 960, cmd_file="c.cmd"))  # fmt: skip
    assert render(job, spec, "out.mp4").stat().st_size > 1000


def test_mixed_captions_switch_placement_per_shot():
    from snipmint.captions.formats import get_format as gf
    from snipmint.captions.from_transcript import clip_words
    from snipmint.captions.styles import resolve_style
    from snipmint.pipeline.clips import _clip_captions, _layout_overrides

    from .helpers import make_transcript

    t = make_transcript([f"Sentence number {i} goes here." for i in range(8)], word_dur=0.4, gap=0.4)
    analysis, words = clip_words(t.words, 0, t.duration)
    styles = {k: resolve_style("bold-pop", _layout_overrides({}, k), None) for k in ("track", "split", "mixed")}
    plan = FramingPlan(layout="mixed", src_w=1920, src_h=1080, split_ranges=[(0.0, 5.0)],
                       regions=[Region(x=0, y=0, w=10, h=10), Region(x=20, y=0, w=10, h=10)])  # fmt: skip
    ass, _font = _clip_captions(words, analysis, plan, styles, gf("reels"), False)
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    ys = {}
    for ln in lines:
        start = ln.split(",")[1]
        y = int(ln.split(r"\pos(")[1].split(")")[0].split(",")[1])
        ys.setdefault("split" if start < "0:00:05" else "track", set()).add(y)
    assert ys["split"] and ys["track"] and ys["split"].isdisjoint(ys["track"])


def test_loudness_filter_reaches_ffmpeg():
    from snipmint.pipeline.clips import LOUDNORM
    from snipmint.render.ffmpeg_render import build_command

    spec = RenderSpec(width=1080, height=1920, fps=30, duration=10, background="video", background_file="s.mp4",
                      background_start=5.0, audio=None, audio_from_background=True, audio_filter=LOUDNORM)  # fmt: skip
    args = build_command(spec, "out.mp4")
    assert args[args.index("-af") + 1] == LOUDNORM
    assert "0:a:0?" in args
    spec.audio_filter = None
    assert "-af" not in build_command(spec, "out.mp4")


@needs_ffmpeg
def test_voiceover_is_mixed_over_the_start(letterboxed, tmp_path):
    from snipmint.pipeline.clips import LOUDNORM

    job = tmp_path / "vo"
    job.mkdir()
    ff.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=880:duration=1.2", "-ar", "48000", str(job / "hook_01.wav")])
    (job / "empty.ass").write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
    (job / "fonts").mkdir()
    spec = RenderSpec(width=540, height=960, fps=30, duration=3.0, background="video",
                      background_file=str(letterboxed.video), background_start=0.0, audio=None,
                      audio_from_background=True, ass="empty.ass", quality="fast", audio_filter=LOUDNORM,
                      voiceover="hook_01.wav", voiceover_until=1.6)  # fmt: skip
    from snipmint.render.ffmpeg_render import build_command

    args = build_command(spec, "out.mp4")
    assert "-filter_complex" in args and "-af" not in args and "[aout]" in args
    out = render(job, spec, "out.mp4")
    probe = subprocess.run([ff.find_ffprobe(), "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                            "-of", "json", str(out)], capture_output=True, check=True)  # fmt: skip
    data = json.loads(probe.stdout)
    assert {s["codec_type"] for s in data["streams"]} == {"video", "audio"}
    assert abs(float(data["format"]["duration"]) - 3.0) < 0.2
