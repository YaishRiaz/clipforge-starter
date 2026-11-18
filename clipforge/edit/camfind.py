"""Find where the streamer's webcam sits in a clip's footage, by looking at it.

A streamer changes overlay from stream to stream, so a fixed box in layouts/<creator>.json is
only right for the streams it was measured on. Three frames of the footage go to a vision
model, which says whether the camera is a small overlay (and where), fills the whole screen, or
is absent. A hand-measured box in the layout file is kept when the model's box agrees with it,
because it is exact; the model's box is used when it does not.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ..core import media
from ..core.config import Settings
from ..taste import analyze
from .layout import Box

log = logging.getLogger("clipforge.camfind")

POINTS = (0.2, 0.5, 0.8)
WIDE_POINTS = (0.1, 0.3, 0.5, 0.7, 0.9)      # a second look after a reviewer says the camera is missing
AGREE_IOU = 0.5
MIN_SIDE, MAX_AREA = 0.08, 0.45          # fractions of the frame: smaller is not a cam, larger is a scene

PROMPT = (
    "These are frames from one Twitch stream VOD. For EACH frame say where the streamer's webcam "
    "(the live video of the streamer's face) is. Answer 'overlay' when the webcam is a small box "
    "placed over the game or desktop, and give its box. Answer 'full_screen' when the webcam fills "
    "the whole picture (a just-chatting scene). Answer 'none' when no webcam is visible. The box is "
    "the live video area only: leave out the decorative border, any sub-goal, follower or alert bar "
    "under it, and chat boxes. Give the box in thousandths of the image (integers 0 to 1000): left, "
    "top, width, height. Answer with JSON only, matching this schema:\n"
)


class FrameCam(BaseModel):
    kind: str = Field("none", description="overlay | full_screen | none")
    left: float = 0.0
    top: float = 0.0
    width: float = 0.0
    height: float = 0.0


class CamReport(BaseModel):
    frames: list[FrameCam] = Field(default_factory=list)


@dataclass(frozen=True)
class Detection:
    kind: str                     # overlay | full_screen | none | unknown
    box: Box | None = None
    note: str = ""


def iou(a: Box, b: Box) -> float:
    x1, y1 = max(a.x, b.x), max(a.y, b.y)
    x2, y2 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union else 0.0


def _even(n: float) -> int:
    return int(round(n / 2) * 2)


def _to_box(f: FrameCam, w: int, h: int) -> Box | None:
    if f.kind != "overlay":
        return None
    if max(f.left, f.top, f.width, f.height) > 1.5:           # thousandths, as the prompt asks
        f = FrameCam(kind=f.kind, left=f.left / 1000, top=f.top / 1000,
                     width=f.width / 1000, height=f.height / 1000)
    if f.width < MIN_SIDE or f.height < MIN_SIDE or f.width * f.height > MAX_AREA:
        return None
    left, top = max(0.0, f.left), max(0.0, f.top)
    width, height = min(f.width, 1.0 - left), min(f.height, 1.0 - top)
    if width < MIN_SIDE or height < MIN_SIDE:
        return None
    return Box(_even(left * w), _even(top * h), _even(width * w), _even(height * h))


def _mean(boxes: list[Box]) -> Box:
    return Box(*(_even(sum(getattr(b, k) for b in boxes) / len(boxes)) for k in ("x", "y", "w", "h")))


def camera_shaped(box: Box) -> Box:
    """A webcam is never taller than it is wide. A box that is has swallowed the panel under
    the camera (a stats or reward box), so keep its top, at the usual 16:9 camera shape."""
    if box.h <= box.w:
        return box
    return Box(box.x, box.y, box.w, round(box.w * 9 / 16 / 2) * 2)


def decide(report: CamReport, w: int, h: int, known: Box | None = None) -> Detection:
    """One answer from the per-frame reports: agreeing boxes win, then full screen, then none."""
    frames = report.frames
    if not frames:
        return Detection("unknown", note="the model returned no frames")
    boxes = [b for b in (_to_box(f, w, h) for f in frames) if b is not None]
    agreeing = [b for b in boxes if sum(iou(b, o) >= AGREE_IOU for o in boxes) >= 2]
    if agreeing:
        box = camera_shaped(_mean(agreeing))
        if known is not None and iou(box, known) >= AGREE_IOU:
            return Detection("overlay", known, "the layout file's box matches what is on screen")
        return Detection("overlay", box, f"{len(agreeing)} of {len(frames)} frames agree")
    kinds = [f.kind for f in frames]
    if kinds.count("full_screen") * 2 > len(frames):
        return Detection("full_screen", None, "the webcam fills the screen")
    if kinds.count("none") * 2 > len(frames):
        return Detection("none", None, "no webcam is visible")
    if len(boxes) == 1:
        return Detection("overlay", camera_shaped(boxes[0]), "seen in one frame only")
    return Detection("unknown", note="the frames disagree")


def _grab(footage: Path, times: list[float], dest: Path) -> list[tuple[float, Path]]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for t in times:
        path = dest / f"cam_{t:.2f}.jpg"
        subprocess.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                        "-i", str(footage), "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "4", str(path)],
                       capture_output=True, timeout=120)
        if path.is_file():
            out.append((t, path))
    return out


def locate(footage: Path, *, ss: float, duration: float, settings: Settings, work: Path,
           known: Box | None = None, post=None, points=POINTS) -> Detection:
    """Look at three frames of the clip's footage. Never raises: a failure is `unknown`."""
    try:
        info = media.probe(footage)
        frames = _grab(footage, [round(ss + duration * p, 2) for p in points], work / "camframes")
        if not frames:
            return Detection("unknown", note="no frame could be read")
        report = analyze.ask(frames, PROMPT, CamReport, settings, post=post)
        return decide(report, info.width or 1920, info.height or 1080, known)
    except Exception as exc:
        log.warning("facecam search failed for %s: %s", footage.name, exc)
        return Detection("unknown", note=f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        for f in (work / "camframes").glob("*.jpg"):
            f.unlink(missing_ok=True)
