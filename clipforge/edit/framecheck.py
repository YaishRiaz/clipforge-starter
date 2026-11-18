"""Look at a few frames of the finished clip before it reaches Yaish.

The machine checks already catch a black camera band and a wrong size. This is the eye test a
reviewer would do first: is the caption readable and alone, is anything cut off, is the camera
where the brief wants it. It only warns; a vision-model hiccup never blocks a clip.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from ..core import media
from ..core.config import Settings
from ..taste import analyze

log = logging.getLogger("clipforge.framecheck")

POINTS = (0.12, 0.35, 0.6, 0.85)

PROMPT = (
    "These are frames from one finished vertical (9:16) short-video edit, each labelled with its second. "
    "Judge only what is visible. Report a problem ONLY if you can clearly see it in a frame: "
    "caption_overlap = two different caption lines drawn on top of each other so the text is garbled; "
    "caption_cut_off = caption or title text running off the edge of the picture or hidden behind something; "
    "caption_unreadable = caption text too small, low contrast or covered; "
    "black_area = a large empty or black region that holds no picture; "
    "camera_missing = no live webcam of the streamer's face is visible in any frame "
    "(answer false if the face is on screen at all). "
    "Answer with JSON only, matching this schema:\n"
)


class FrameProblems(BaseModel):
    caption_overlap: bool = False
    caption_cut_off: bool = False
    caption_unreadable: bool = False
    black_area: bool = False
    camera_missing: bool = False
    seconds: list[float] = Field(default_factory=list, description="seconds of the frames where a problem is visible")
    note: str = ""


LABELS = {
    "caption_overlap": "two caption lines are drawn over each other",
    "caption_cut_off": "text runs off the picture or is hidden",
    "caption_unreadable": "caption text is hard to read",
    "black_area": "a large empty or black area in the picture",
    "camera_missing": "no webcam of the streamer is visible",
}


def _grab(clip: Path, duration: float, dest: Path) -> list[tuple[float, Path]]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for p in POINTS:
        t = round(duration * p, 2)
        path = dest / f"fc_{t:.2f}.jpg"
        subprocess.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                        "-i", str(clip), "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(path)],
                       capture_output=True, timeout=120)
        if path.is_file():
            out.append((t, path))
    return out


def check(clip: Path, *, settings: Settings, work: Path, cam_expected: bool, post=None) -> list[str]:
    """Plain-English problems seen in the clip's frames; empty when none, or when it could not look."""
    frames: list[tuple[float, Path]] = []
    try:
        frames = _grab(clip, media.probe(clip).duration, work / "framecheck")
        if len(frames) < 2:
            return []
        report = analyze.ask(frames, PROMPT, FrameProblems, settings, post=post)
    except Exception as exc:
        log.warning("frame check skipped for %s: %s", clip.name, exc)
        return []
    finally:
        for _t, f in frames:
            f.unlink(missing_ok=True)
    found = [LABELS[k] for k in LABELS if getattr(report, k) and (k != "camera_missing" or cam_expected)]
    if found and report.seconds:
        found[0] += " (around " + ", ".join(f"{s:g} s" for s in report.seconds[:3]) + ")"
    return found
