"""The ffmpeg pass that makes the vertical clip, and the thumbnail from its first frame."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..core import media
from .layout import OUT_W, Layout, filter_graph


LOGO_W, LOGO_Y = 520, 150     # width of the official logo and its distance from the top
EMOJI_SIZE = 150


def build_command(src: Path, *, ss: float, duration: float, layout: Layout,
                  ass_name: str, dest: Path, zooms=(), zoom_scale: float = 1.10,
                  pops=(), pop_y: int = 1300) -> list[str]:
    """ffmpeg argv. `ass_name` is relative to the working directory render() uses,
    which avoids escaping a Windows drive letter inside the filtergraph.

    `pops` are emoji (edit.emoji.Pop): each picture is shown between its start and end, at the
    right edge just above the captions, under the caption text.
    """
    graph = filter_graph(layout, zooms, zoom_scale)
    extra: list[str] = []
    last = "v0"
    if layout.logo:
        graph += f";[1:v]scale={LOGO_W}:-1[lg];[v0][lg]overlay=(W-w)/2:{LOGO_Y}[v0l]"
        extra += ["-i", str(layout.logo)]
        last = "v0l"
    for i, p in enumerate(pops):
        n = 1 + len(extra) // 2
        graph += (f";[{n}:v]scale={EMOJI_SIZE}:-1:flags=lanczos[e{i}]"
                  f";[{last}][e{i}]overlay={OUT_W - EMOJI_SIZE - 60}:{pop_y}"
                  f":enable='between(t,{p.start:.2f},{p.end:.2f})'[p{i}]")
        extra += ["-i", str(p.path)]
        last = f"p{i}"
    graph += f";[{last}]ass={ass_name}[vout]"
    return [
        media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{ss:.3f}", "-i", str(src), *extra, "-t", f"{duration:.3f}",
        "-filter_complex", graph, "-map", "[vout]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(dest),
    ]


def render(src: Path, *, ss: float, duration: float, layout: Layout, ass_path: Path,
           dest: Path, timeout: float = 1800, zooms=(), zoom_scale: float = 1.10,
           pops=(), pop_y: int = 1300) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = build_command(src, ss=ss, duration=duration, layout=layout,
                         ass_name=ass_path.name, dest=dest.resolve(), zooms=zooms, zoom_scale=zoom_scale,
                         pops=pops, pop_y=pop_y)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          cwd=str(ass_path.parent))
    if proc.returncode != 0:
        raise media.MediaError(f"ffmpeg failed rendering {dest.name}: {(proc.stderr or '')[-1500:]}")
    return dest


def thumbnail(clip: Path, dest: Path) -> Path:
    media.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(clip), "-frames:v", "1", "-q:v", "2", str(dest)])
    return dest


def cam_band_blank(clip: Path, layout: Layout, *, floor: int = 40) -> bool:
    """True when the facecam band of a rendered clip is black throughout.

    A facecam box that misses the camera (a stream that moved it) renders a
    solid black band, which must not reach Yaish as if the cam were showing.
    """
    if layout.facecam is None or layout.fit:
        return False
    band = f"crop={OUT_W}:{layout.cam_height}:0:0,fps=2,signalstats,metadata=print:key=lavfi.signalstats.YMAX"
    proc = subprocess.run(
        [media._tool("ffmpeg"), "-hide_banner", "-loglevel", "info", "-i", str(clip),
         "-vf", band, "-an", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300)
    values = [int(float(v)) for v in re.findall(r"YMAX=([0-9.]+)", proc.stderr or "")]
    return bool(values) and max(values) < floor
