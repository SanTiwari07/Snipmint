"""Decide and describe how a landscape clip becomes vertical.

Layouts:
- ``track``: a 9:16 window that follows the speaker's face with a calm camera.
- ``split``: two people side by side become two stacked halves (podcasts, interviews).
- ``fit``:   no people on screen (slides, screen recordings, b-roll): the whole picture, scaled to
             the width, over a blurred copy of itself.
- ``center``: a fixed centre crop (also used when a source already has the target shape).

Black bars baked into the source (letterbox) are removed first in every layout.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from ..captions.formats import VideoFormat
from ..ingest.source import Source
from . import faces as faces_mod
from .camera import SmoothedCamera, SubjectPicker
from .faces import Face
from .media import Box, detect_letterbox, sample_frames, video_size

log = logging.getLogger("snipmint.reframe")

Framing = Literal["auto", "center", "track", "split", "fit"]
Layout = Literal["center", "track", "split", "fit", "mixed"]
SAMPLE_FPS = 5.0
RENDER_FPS = 30
ANALYSIS_WIDTH = 640


class Region(BaseModel):
    x: int
    y: int
    w: int
    h: int


class FramingPlan(BaseModel):
    layout: Layout
    reason: str = ""
    src_w: int
    src_h: int
    box: Region | None = None  # content area inside the source (black bars removed)
    crop_w: int = 0  # track / center window, in content pixels
    crop_h: int = 0
    crop_y: int = 0
    xs: list[tuple[float, int]] = Field(default_factory=list)  # (time in clip, left edge) change points
    regions: list[Region] = Field(default_factory=list)  # split: top, bottom
    # mixed: the shots (clip time) shown as stacked halves; the rest follows the speaker
    split_ranges: list[tuple[float, float]] = Field(default_factory=list)

    # ------------------------------------------------------------------ render

    def layout_at(self, t: float) -> Layout:
        """The layout on screen at clip time ``t`` (``mixed`` resolves to split or track)."""
        if self.layout != "mixed":
            return self.layout
        return "split" if any(a <= t < b for a, b in self.split_ranges) else "track"

    def x_at(self, t: float) -> int:
        x = self.xs[0][1] if self.xs else 0
        for tt, xx in self.xs:
            if tt > t:
                break
            x = xx
        return x

    def sendcmd(self) -> str:
        return "".join(f"{t:.3f} crop@c x {x};\n" for t, x in self.xs[1:])

    def video_filter(self, out_w: int, out_h: int, *, cmd_file: str | None = None, at: float | None = None) -> str:
        """The ffmpeg filter chain from the source frame to ``out_w`` x ``out_h``.

        With ``cmd_file`` (track layout) the window moves over time via sendcmd; with ``at`` it
        is frozen at that moment (preview frames)."""
        pre = f"crop={self.box.w}:{self.box.h}:{self.box.x}:{self.box.y}," if self.box else ""
        layout = self.layout_at(at) if at is not None else self.layout
        if layout == "mixed" and len(self.regions) == 2:
            # both pictures are made every frame; the stacked one is laid on top during the
            # two-person shots, so the switch lands exactly on the scene cuts
            enable = "+".join(f"between(t,{a:.3f},{b - 0.001:.3f})" for a, b in self.split_ranges)
            track = self._track_chain(out_w, out_h, cmd_file, None)
            return (
                f"{pre}split=2[ma][mb];"
                f"[ma]{track}[mt];"
                f"[mb]{self._split_chain(out_w, out_h, 'q')}[ms];"
                f"[mt][ms]overlay=enable='{enable}',setsar=1"
            )
        if layout == "split" and len(self.regions) == 2:
            return pre + self._split_chain(out_w, out_h, "s")
        if layout == "fit":
            return (
                f"{pre}split=2[fa][fb];"
                f"[fa]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
                f"gblur=sigma=24,eq=brightness=-0.08[fbg];"
                f"[fb]scale={out_w}:-2[ffg];"
                f"[fbg][ffg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,setsar=1"
            )
        return pre + self._track_chain(out_w, out_h, cmd_file if layout in ("track", "mixed") else None, at)

    def _track_chain(self, out_w: int, out_h: int, cmd_file: str | None, at: float | None) -> str:
        x = self.x_at(at) if at is not None else (self.xs[0][1] if self.xs else 0)
        win = f"crop@c=w={self.crop_w}:h={self.crop_h}:x={x}:y={self.crop_y}"
        if cmd_file and len(self.xs) > 1 and at is None:
            win = f"sendcmd=f={cmd_file}," + win
        return f"{win},scale={out_w}:{out_h},setsar=1"

    def _split_chain(self, out_w: int, out_h: int, tag: str) -> str:
        half = out_h // 2
        a, b = self.regions
        return (
            f"split=2[{tag}a][{tag}b];"
            f"[{tag}a]crop={a.w}:{a.h}:{a.x}:{a.y},scale={out_w}:{half},setsar=1[{tag}t];"
            f"[{tag}b]crop={b.w}:{b.h}:{b.x}:{b.y},scale={out_w}:{out_h - half},setsar=1[{tag}u];"
            f"[{tag}t][{tag}u]vstack"
        )


# ---------------------------------------------------------------------- planning helpers


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def _window(content_w: int, content_h: int, aspect: float) -> tuple[int, int]:
    """Largest window of ``aspect`` (w/h) that fits in the content."""
    h = content_h
    w = h * aspect
    if w > content_w:
        w = content_w
        h = w / aspect
    return _even(w), _even(h)


def _split_regions(samples: list[list[Face]], content_w: int, content_h: int, aspect_half: float) -> list[Region] | None:
    """Two stable faces, left and right: a window around each (face in the upper part)."""
    lefts, rights = [], []
    for fs in samples:
        big = sorted(fs, key=lambda f: -f.area)[:2]
        if len(big) == 2:
            a, b = sorted(big, key=lambda f: f.cx)
            lefts.append(a)
            rights.append(b)
    if len(lefts) < 3:
        return None
    regions = []
    for group in (lefts, rights):
        cx = statistics.median(f.cx for f in group)
        cy = statistics.median(f.cy for f in group)
        fh = statistics.median(f.h for f in group)
        rh = min(content_h, fh * 3.2)
        rw = rh * aspect_half
        if rw > content_w / 2:
            rw = content_w / 2
            rh = rw / aspect_half
        rw, rh = _even(rw), _even(rh)
        x = int(min(max(cx - rw / 2, 0), content_w - rw))
        y = int(min(max(cy - rh * 0.4, 0), content_h - rh))
        regions.append(Region(x=x // 2 * 2, y=y // 2 * 2, w=rw, h=rh))
    return regions


def _two_people(fs: list[Face], crop_w: float) -> bool:
    if len(fs) < 2:
        return False
    a, b = sorted(fs, key=lambda f: -f.area)[:2]
    return b.area >= 0.35 * a.area and abs(a.cx - b.cx) > crop_w * 0.9


def _track_xs(samples: list[tuple[float, list[Face]]], cuts: list[float], crop_w: int, content_w: int,
              duration: float, src_w: int) -> list[tuple[float, int]]:  # fmt: skip
    cam = SmoothedCamera(crop_w, content_w, speed_scale=max(0.5, src_w / 1920))
    picker = SubjectPicker(crop_w)
    cuts = sorted(c for c in cuts if 0 < c < duration)
    next_cut = 0
    xs: list[tuple[float, int]] = []
    last: int | None = None
    frames = int(duration * RENDER_FPS) + 1
    si = 0
    for f in range(frames):
        t = f / RENDER_FPS
        if next_cut < len(cuts) and t >= cuts[next_cut]:
            cam.begin_scene()
            picker.reset()
            next_cut += 1
        while si < len(samples) and samples[si][0] <= t + 1e-6:
            subject = picker.pick(samples[si][1])
            cam.update_target(subject.cx if subject else None)
            si += 1
        x = int(round(cam.step())) // 2 * 2
        if x != last:
            xs.append((round(t, 3), x))
            last = x
    return xs or [(0.0, (content_w - crop_w) // 2 // 2 * 2)]


# ---------------------------------------------------------------------- main


def plan_framing(
    source: Source,
    start: float,
    end: float,
    fmt: VideoFormat,
    framing: Framing = "auto",
    *,
    scene_cuts: list[float] | None = None,
    progress: Callable[[float], None] | None = None,
) -> FramingPlan:
    """Work out the framing of one clip (cached in the source folder)."""
    key = f"framing/{start:.2f}-{end:.2f}-{fmt.width}x{fmt.height}-{framing}.json"
    cached = source.file(key)
    if cached.is_file():
        try:
            return FramingPlan.model_validate(source.read_json(key))
        except ValueError:
            pass
    plan = _plan(source, start, end, fmt, framing, scene_cuts or [], progress)
    cached.parent.mkdir(parents=True, exist_ok=True)
    source.write_json(key, plan.model_dump())
    return plan


def _plan(source: Source, start: float, end: float, fmt: VideoFormat, framing: Framing,
          scene_cuts: list[float], progress: Callable[[float], None] | None) -> FramingPlan:  # fmt: skip
    out_w, out_h = int(fmt.width or 1080), int(fmt.height or 1920)
    aspect = out_w / out_h
    src_w, src_h = video_size(source.video)
    duration = max(0.5, end - start)
    raw_box = detect_letterbox(source.video, start, duration, (src_w, src_h))
    box = Region(x=raw_box.x, y=raw_box.y, w=raw_box.w, h=raw_box.h) if raw_box else None
    cw, ch = (box.w, box.h) if box else (src_w, src_h)
    crop_w, crop_h = _window(cw, ch, aspect)
    centre = [(0.0, ((cw - crop_w) // 2) // 2 * 2)]
    base = {"src_w": src_w, "src_h": src_h, "box": box, "crop_w": crop_w, "crop_h": crop_h,
            "crop_y": ((ch - crop_h) // 2) // 2 * 2, "xs": centre}  # fmt: skip
    note = " Black bars removed." if box else ""

    if abs(cw / ch - aspect) < 0.03:
        return FramingPlan(layout="center", reason="The video already has this shape." + note, **base)
    if framing == "center":
        return FramingPlan(layout="center", reason="Fixed centre crop." + note, **base)
    if framing == "fit":
        return FramingPlan(layout="fit", reason="Whole picture over a blurred background." + note, **base)

    # ---- look at the faces
    if not faces_mod.available():
        return FramingPlan(layout="center", reason="Face detection isn't installed; centre crop." + note, **base)
    try:
        samples: list[tuple[float, list[Face]]] = []
        detector = None
        rel_box = Box(box.w, box.h, box.x, box.y) if box else None
        for t, frame, scale in sample_frames(source.video, start, duration, fps=SAMPLE_FPS,
                                             width=ANALYSIS_WIDTH, box=rel_box, size=(src_w, src_h)):  # fmt: skip
            if detector is None:
                detector = faces_mod.FaceDetector(frame.shape[1], frame.shape[0])
            samples.append((t, [f.scaled(scale) for f in detector.detect(frame)]))
            if progress:
                progress(min(0.99, t / duration))
    except Exception as exc:  # no model, broken decode: never fail the clip over framing
        log.warning("Face analysis failed (%s); using a centre crop", exc)
        return FramingPlan(layout="center", reason="Face analysis failed; centre crop." + note, **base)
    if not samples:
        return FramingPlan(layout="center", reason="No frames could be read; centre crop." + note, **base)

    with_face = sum(1 for _, fs in samples if fs) / len(samples)
    two = sum(1 for _, fs in samples if _two_people(fs, crop_w)) / len(samples)
    vertical_out = aspect < 1
    rel_cuts = sorted(c - start for c in scene_cuts if start < c < end)

    # the speaker-following camera is needed by "track" and by the close-ups of "mixed"
    xs = _track_xs(samples, rel_cuts, crop_w, cw, duration, src_w)
    crop_y = base["crop_y"]
    if crop_h < ch and any(fs for _, fs in samples):  # shorter window: keep heads in the upper third
        cy = statistics.median(f.cy for _, fs in samples for f in fs)
        crop_y = int(min(max(cy - crop_h * 0.35, 0), ch - crop_h)) // 2 * 2
    base.update(xs=xs, crop_y=crop_y)

    fallback_note = ""
    if framing == "split" or (framing == "auto" and vertical_out):
        two_shots = _two_person_shots(samples, rel_cuts, duration, crop_w)
        covered = sum(b - a for a, b in two_shots) / duration
        regions = _split_regions(
            [fs for t, fs in samples if _two_people(fs, crop_w) and any(a <= t < b for a, b in two_shots)],
            cw, ch, out_w / (out_h / 2),
        ) if two_shots else None  # fmt: skip
        if regions and (covered >= 0.9 or (not rel_cuts and two >= (0.2 if framing == "split" else 0.5))):
            return FramingPlan(layout="split", regions=regions,
                               reason=f"Two people on screen in {two:.0%} of the clip: stacked halves." + note,
                               **base)  # fmt: skip
        if regions and covered >= (0.1 if framing == "split" else 0.15):
            return FramingPlan(
                layout="mixed", regions=regions, split_ranges=two_shots,
                reason=f"Stacked halves during the two-person shots ({covered:.0%} of the clip), "
                       "the speaker during close-ups." + note,
                **base,
            )  # fmt: skip
        if framing == "split":
            # asked for two people, but the clip is (almost) always one person: stacking the rare
            # two-shot frames would show empty halves most of the time
            fallback_note = f" Two people are on screen in only {two:.0%} of this clip, so it follows the speaker."
    if framing == "auto" and with_face < 0.3:
        return FramingPlan(layout="fit", reason=f"Faces in only {with_face:.0%} of the clip: whole picture." + note,
                           **base)  # fmt: skip
    reason = f"Following the speaker (faces in {with_face:.0%} of the clip)." + fallback_note + note
    return FramingPlan(layout="track", reason=reason, **base)


def _two_person_shots(samples: list[tuple[float, list[Face]]], cuts: list[float], duration: float,
                      crop_w: int) -> list[tuple[float, float]]:  # fmt: skip
    """The shots (between scene cuts) where two people are on screen most of the time, merged."""
    bounds = [0.0, *cuts, duration]
    shots: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        inside = [fs for t, fs in samples if a <= t < b]
        if len(inside) >= 2 and sum(_two_people(fs, crop_w) for fs in inside) / len(inside) >= 0.5:
            if shots and abs(shots[-1][1] - a) < 1e-3:
                shots[-1] = (shots[-1][0], round(b, 3))
            else:
                shots.append((round(a, 3), round(b, 3)))
    return shots
