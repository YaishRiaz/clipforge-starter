"""Where the facecam sits in a creator's stream, and what the vertical frame is made of.

Measured by eye on a real frame of SkiiMaskTimmy's stream (13 Nov 2026): the
facecam is a fixed box bottom-left. A creator with no layout file gets a plain
centre crop, which is also what a stream without a facecam needs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from ..core.config import package_root

OUT_W, OUT_H = 1080, 1920


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Layout:
    creator: str
    source_w: int
    source_h: int
    facecam: Box | None
    facecam_required: bool
    handle: str | None
    fit: bool = False
    logo: str | None = None
    cam_full: bool = False        # the camera fills the whole picture, so the centre crop is the camera
    cam_note: str = ""            # what the camera search found, for the reviewer
    game_full: bool = False       # show the whole 16:9 game under the facecam instead of a centre crop

    @property
    def cam_height(self) -> int:
        """Height of the facecam band once scaled to the full output width."""
        if self.facecam is None:
            return 0
        h = round(OUT_W * self.facecam.h / self.facecam.w / 2) * 2
        return min(h, MAX_FULL_CAM) if self.game_full else h

    @property
    def game_bottom(self) -> int | None:
        """With the whole game shown, where it ends; below it is empty space for text."""
        if not self.game_full:
            return None
        return self.cam_height + GAME_FULL_H if self.facecam else (OUT_H + GAME_FULL_H) // 2

    @property
    def cam_share(self) -> float:
        """Fraction of the vertical frame that shows the creator."""
        return self.cam_height / OUT_H


def layouts_dir() -> Path:
    return package_root() / "layouts"


def slug(creator: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (creator or "").lower())


def load(creator: str | None, *, directory: Path | None = None) -> Layout:
    path = (directory or layouts_dir()) / f"{slug(creator)}.json"
    if not slug(creator) or not path.is_file():
        return Layout(creator=slug(creator), source_w=1920, source_h=1080, facecam=None,
                      facecam_required=False, handle=None)
    data = json.loads(path.read_text(encoding="utf-8"))
    cam = data.get("facecam")
    w, h = data.get("source_size", [1920, 1080])
    return Layout(
        creator=data.get("creator", slug(creator)), source_w=int(w), source_h=int(h),
        facecam=Box(**{k: int(cam[k]) for k in ("x", "y", "w", "h")}) if cam else None,
        facecam_required=bool(data.get("facecam_required", False)),
        handle=data.get("handle"),
    )


def logo_for(campaign_id: int | None) -> str | None:
    """An official logo file Yaish placed at assets/logos/<campaign id>.png, if any."""
    path = package_root() / "assets" / "logos" / f"{campaign_id}.png"
    return str(path) if campaign_id is not None and path.is_file() else None


STREAM_HOSTS = ("twitch.tv", "kick.com")


def is_stream(url: str | None) -> bool:
    return any(h in (url or "").lower() for h in STREAM_HOSTS)


GAME_FULL_H = OUT_W * 9 // 16 // 2 * 2       # a 16:9 picture at full output width
MAX_FULL_CAM = 700                           # leave room under the game for the captions


def for_moment(creator: str | None, moment, campaign_id: int | None = None, *,
               produced: bool = False, game_full: bool = False) -> Layout:
    """The creator's layout, except an official trailer is shown whole.

    A trailer is composed for 16:9; centre-cropping it cuts off faces and the
    action, so the whole frame sits over a blurred copy of itself instead.
    """
    from dataclasses import replace

    lay = replace(load(creator), logo=logo_for(campaign_id))
    if moment["rubric_category"] == "official_trailer" or produced:
        # A trailer or an already-edited video (a podcast, a talk with its own graphics) is
        # composed for 16:9: a centre crop cuts its graphics in half and puts our text on top
        # of them. Shown whole, our hook and captions sit in the bands above and below.
        return replace(lay, facecam=None, fit=True)
    return replace(lay, game_full=True) if game_full else lay


def zoom_expr(zooms, scale: float) -> str:
    """Zoom factor over time: 1.0, rising to `scale` and back inside each (start, length) pulse."""
    pulses = "+".join(
        f"between(t,{s:.2f},{s + d:.2f})*pow(sin(PI*(t-{s:.2f})/{d:.2f}),2)" for s, d in zooms)
    return f"(1+{scale - 1:.3f}*({pulses}))"


def _punch(w: int, h: int, zooms, scale: float) -> str:
    """Scale a w x h picture up by the zoom factor each frame, then crop back to w x h."""
    z = zoom_expr(zooms, scale)
    return f",scale=w='trunc(iw*{z}/2)*2':h='trunc(ih*{z}/2)*2':eval=frame,crop={w}:{h}"


def filter_graph(layout: Layout, zooms=(), zoom_scale: float = 1.10) -> str:
    """The ffmpeg video graph: facecam on top, gameplay below, or a centre crop.

    `zooms` are (start, length) punch-ins. They touch only the gameplay, never
    the facecam band, and never a trailer shown whole.
    """
    if layout.game_full:
        # Chaotic third-person games lose the action to a centre crop: the chaser, the goal,
        # the other players are all off to the sides. The whole picture goes under the cam.
        if layout.facecam is None:
            return filter_graph(replace(layout, game_full=False, fit=True))
        cam = layout.facecam
        return (
            f"[0:v]split=3[a][b][c];"
            f"[c]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},boxblur=30:5[bg];"
            f"[a]crop={cam.w}:{cam.h}:{cam.x}:{cam.y},scale={OUT_W}:{layout.cam_height},setsar=1[cam];"
            f"[b]scale={OUT_W}:{GAME_FULL_H},setsar=1[game];"
            f"[bg][cam]overlay=0:0[top];"
            f"[top][game]overlay=0:{layout.cam_height},setsar=1,fps=30,format=yuv420p[v0]"
        )
    if layout.fit:
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},boxblur=30:5[bgb];"
            f"[fg]scale={OUT_W}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p[v0]"
        )
    if layout.facecam is None:
        punch = _punch(OUT_W, OUT_H, zooms, zoom_scale) if zooms else ""
        return (f"[0:v]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H}{punch},setsar=1,fps=30,"
                f"format=yuv420p[v0]")
    cam = layout.facecam
    game_h = OUT_H - layout.cam_height
    punch = _punch(OUT_W, game_h, zooms, zoom_scale) if zooms else ""
    return (
        f"[0:v]split=2[a][b];"
        f"[a]crop={cam.w}:{cam.h}:{cam.x}:{cam.y},scale={OUT_W}:{layout.cam_height},setsar=1[cam];"
        f"[b]scale=-2:{game_h},crop={OUT_W}:{game_h}{punch},setsar=1[game];"
        f"[cam][game]vstack,fps=30,format=yuv420p[v0]"
    )
