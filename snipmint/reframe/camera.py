"""A calm virtual camera operator for the vertical crop.

``SmoothedCamera`` is ported from openshorts/main.py ``SmoothedCameraman`` (MIT, (c) 2024
OpenShorts): it only moves when the subject leaves a safe zone, pans at a steady speed, ignores a
single big jump until it repeats (detector noise) and cuts instead of panning at a scene change.
``SubjectPicker`` keeps the same person in frame unless another face is clearly bigger for a while.
"""

from __future__ import annotations

from .faces import Face

JUMP_CONFIRM = 3  # detections a big move must repeat before the camera follows it


class SmoothedCamera:
    def __init__(self, crop_w: float, frame_w: float, speed_scale: float = 1.0):
        self.crop_w = crop_w
        self.frame_w = frame_w
        self.safe = crop_w * 0.25
        self.slow = 3.0 * speed_scale  # px per frame at 30 fps
        self.fast = 15.0 * speed_scale
        self.current = frame_w / 2
        self.target = frame_w / 2
        self._pending: float | None = None
        self._pending_n = 0
        self._snap = True  # the first target is a cut, not a pan

    def begin_scene(self) -> None:
        self._pending, self._pending_n, self._snap = None, 0, True

    def update_target(self, cx: float | None) -> None:
        if cx is None:
            return
        if self._snap:
            self._snap = False
            self._pending, self._pending_n = None, 0
            self.target = self.current = cx
            return
        if abs(cx - self.target) > self.safe:
            if self._pending is not None and abs(cx - self._pending) <= self.safe:
                self._pending_n += 1
            else:
                self._pending, self._pending_n = cx, 1
            if self._pending_n < JUMP_CONFIRM:
                return
        self._pending, self._pending_n = None, 0
        self.target = cx

    def step(self) -> float:
        """Advance one frame; return the crop's left edge."""
        diff = self.target - self.current
        if abs(diff) > self.safe:
            speed = self.fast if abs(diff) > self.crop_w * 0.5 else self.slow
            step = speed if diff > 0 else -speed
            self.current = self.target if abs(step) >= abs(diff) else self.current + step
        half = self.crop_w / 2
        self.current = min(max(self.current, half), self.frame_w - half)
        return self.current - half


class SubjectPicker:
    """Which face to follow: stick with the current person; switch only when another face is
    clearly bigger (closer to camera / the speaker in a wide shot) for about a second."""

    def __init__(self, crop_w: float, switch_after: int = 5):
        self.crop_w = crop_w
        self.switch_after = switch_after
        self.current: Face | None = None
        self._challenger: Face | None = None
        self._count = 0

    def reset(self) -> None:
        self.current, self._challenger, self._count = None, None, 0

    def pick(self, faces: list[Face]) -> Face | None:
        if not faces:
            return self.current
        biggest = max(faces, key=lambda f: f.area)
        if self.current is None:
            self.current = biggest
            return biggest
        near = [f for f in faces if abs(f.cx - self.current.cx) < self.crop_w * 0.5]
        same = max(near, key=lambda f: f.area) if near else None
        if same is not None and biggest.area <= same.area * 1.6:
            self.current, self._challenger, self._count = same, None, 0
            return same
        # someone else dominates (or our subject left): confirm before switching
        if self._challenger is not None and abs(biggest.cx - self._challenger.cx) < self.crop_w * 0.5:
            self._count += 1
        else:
            self._challenger, self._count = biggest, 1
        if self._count >= self.switch_after or same is None and self._count >= 2:
            self.current, self._challenger, self._count = biggest, None, 0
            return biggest
        if same is not None:
            self.current = same
        return self.current
