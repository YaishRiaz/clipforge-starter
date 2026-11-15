"""Where the facecam sits in a creator's stream, and what the vertical frame is made of.

Measured by eye on a real frame of SkiiMaskTimmy's stream (13 Nov 2026): the
facecam is a fixed box bottom-left. A creator with no layout file gets a plain
centre crop, which is also what a stream without a facecam needs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
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

    @property
    def cam_height(self) -> int:
        """Height of the facecam band once scaled to the full output width."""
        if self.facecam is None:
            return 0
        return round(OUT_W * self.facecam.h / self.facecam.w / 2) * 2

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


def for_moment(creator: str | None, moment, campaign_id: int | None = None) -> Layout:
    """The creator's layout, except an official trailer is shown whole.

    A trailer is composed for 16:9; centre-cropping it cuts off faces and the
    action, so the whole frame sits over a blurred copy of itself instead.
    """
    from dataclasses import replace

    lay = replace(load(creator), logo=logo_for(campaign_id))
    if moment["rubric_category"] == "official_trailer":
        return replace(lay, facecam=None, fit=True)
    return lay


def filter_graph(layout: Layout) -> str:
    """The ffmpeg video graph: facecam on top, gameplay below, or a centre crop."""
    if layout.fit:
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},boxblur=30:5[bgb];"
            f"[fg]scale={OUT_W}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p[v0]"
        )
    if layout.facecam is None:
        return (f"[0:v]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},setsar=1,fps=30,"
                f"format=yuv420p[v0]")
    cam = layout.facecam
    game_h = OUT_H - layout.cam_height
    return (
        f"[0:v]split=2[a][b];"
        f"[a]crop={cam.w}:{cam.h}:{cam.x}:{cam.y},scale={OUT_W}:{layout.cam_height},setsar=1[cam];"
        f"[b]scale=-2:{game_h},crop={OUT_W}:{game_h},setsar=1[game];"
        f"[cam][game]vstack,fps=30,format=yuv420p[v0]"
    )
