"""Visual regression: one preview frame per caption preset, compared by perceptual hash.

Hashes live in ``tests/golden/preview_hashes.json``. Regenerate after an intentional design change with
``UPDATE_GOLDEN=1 pytest tests/unit/test_visual_regression.py``.
"""

import io
import json
import os

import pytest
from PIL import Image

from snipmint.audio import ffmpeg as ff
from snipmint.captions.styles import list_styles
from snipmint.pipeline.make import preview_frame
from snipmint.project import ProjectConfig

from .test_make import SCRIPT

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(not (ff.available() and ff.has_libass()), reason="ffmpeg with libass needed"),
]

GOLDEN = os.path.join(os.path.dirname(__file__), "..", "golden", "preview_hashes.json")
# Hamming distance (of 4096 bits) tolerated between platforms. Anti-aliasing and libass/FreeType versions
# move a few edge pixels; a 2 % font-size change moves ~30 bits and the two closest presets differ by 30.
MAX_DISTANCE = 15


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))


def dhash(png: bytes, size: int = 64) -> int:
    """Difference hash: ``size`` x ``size`` bits comparing horizontally adjacent pixels of a grey thumbnail."""
    img = Image.open(io.BytesIO(png)).convert("L").resize((size + 1, size), Image.Resampling.BOX)
    px = img.load()
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (px[x, y] > px[x + 1, y])
    return bits


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _render(preset: str, overrides: dict | None = None) -> bytes:
    fmt = "youtube" if preset == "subtitle-classic" else "reels"
    return preview_frame(
        ProjectConfig.model_validate({
            "script": SCRIPT,
            "format": fmt,
            "captions": {"preset": preset, "overrides": overrides or {}},
            "voice": {"engine": "fake", "voice_id": "v1"},
        })
    )  # fmt: skip


def test_dhash_is_stable_and_discriminating():
    a = Image.new("RGB", (64, 64), "black")
    for x in range(32):
        for y in range(64):
            a.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    a.save(buf, "PNG")
    h = dhash(buf.getvalue())
    assert h == dhash(buf.getvalue())
    buf2 = io.BytesIO()
    a.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(buf2, "PNG")
    assert distance(h, dhash(buf2.getvalue())) > MAX_DISTANCE


def test_preview_frames_match_golden_hashes():
    presets = [s.id for s in list_styles()]
    current = {p: dhash(_render(p)) for p in presets}
    if os.environ.get("UPDATE_GOLDEN"):
        with open(GOLDEN, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({p: f"{h:01024x}" for p, h in current.items()}, fh, indent=2)
            fh.write("\n")
    with open(GOLDEN, encoding="utf-8") as fh:
        golden = {p: int(h, 16) for p, h in json.load(fh).items()}
    assert set(golden) == set(presets), "run with UPDATE_GOLDEN=1 after adding or removing a preset"
    off = {p: distance(current[p], golden[p]) for p in presets if distance(current[p], golden[p]) > MAX_DISTANCE}
    assert not off, f"preview frame changed (Hamming distance per preset): {off}"


def test_presets_look_different_from_each_other():
    hashes = {p.id: dhash(_render(p.id)) for p in list_styles() if p.id != "subtitle-classic"}
    ids = list(hashes)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            assert distance(hashes[a], hashes[b]) > 0, f"{a} and {b} render identically"


def test_tolerance_catches_small_style_regressions():
    base = dhash(_render("bold-pop"))
    for overrides in ({"font": {"size": 98}}, {"layout": {"offset_y": 30}}):
        assert distance(base, dhash(_render("bold-pop", overrides))) > MAX_DISTANCE, overrides
