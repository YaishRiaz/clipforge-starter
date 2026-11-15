"""ffprobe/ffmpeg helpers shared by ingest, moments and edit.

Everything here shells out; nothing decodes a whole file into memory
(PRD section 9: stream files, seek before decoding).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".flv", ".ts", ".m4a", ".mp3", ".wav"}


class MediaError(RuntimeError):
    """ffprobe/ffmpeg is missing, or refused the file."""


def _tool(name: str) -> str:
    exe = shutil.which(name)
    if exe is None:
        raise MediaError(f"{name} is not installed or not on PATH")
    return exe


def run(args: list[str], *, timeout: float = 3600) -> str:
    """Run ffmpeg/ffprobe and return stdout, raising MediaError on failure."""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise MediaError(f"{Path(args[0]).name} failed: {(proc.stderr or '').strip()[-1500:]}")
    return proc.stdout


@dataclass(frozen=True)
class Probe:
    duration: float
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool
    subtitle_streams: list[int]
    bytes: int
    language: str | None
    title: str | None

    @property
    def has_video(self) -> bool:
        return self.width is not None


def _fps(rate: str | None) -> float | None:
    if not rate or "/" not in rate:
        return None
    num, _, den = rate.partition("/")
    try:
        d = float(den)
        return round(float(num) / d, 3) if d else None
    except ValueError:
        return None


def probe(path: Path | str) -> Probe:
    """Read container and stream metadata. Cheap: no decoding."""
    path = Path(path)
    out = run([
        _tool("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=120)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned no JSON for {path.name}") from exc

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subs = [int(s["index"]) for s in streams if s.get("codec_type") == "subtitle"]

    duration = fmt.get("duration") or (video or {}).get("duration") or (audio or {}).get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        raise MediaError(f"no duration in {path.name}; is it a media file?")

    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    stream_tags = {k.lower(): v for k, v in ((audio or video or {}).get("tags") or {}).items()}
    return Probe(
        duration=duration,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        fps=_fps((video or {}).get("avg_frame_rate")),
        has_audio=audio is not None,
        subtitle_streams=subs,
        bytes=int(fmt.get("size") or (path.stat().st_size if path.is_file() else 0)),
        language=stream_tags.get("language") or tags.get("language"),
        title=tags.get("title"),
    )


def extract_subtitle(src: Path, stream_index: int, dest: Path) -> Path:
    """Pull one embedded subtitle track out to WebVTT."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        _tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-map", f"0:{stream_index}", "-c:s", "webvtt", str(dest),
    ], timeout=900)
    return dest


def extract_audio_wav(src: Path, dest: Path, *, start: float | None = None,
                      duration: float | None = None) -> Path:
    """16 kHz mono wav, which is what faster-whisper wants. Seeks before decoding."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error"]
    if start:
        args += ["-ss", str(start)]
    args += ["-i", str(src)]
    if duration:
        args += ["-t", str(duration)]
    args += ["-vn", "-ac", "1", "-ar", "16000", str(dest)]
    run(args, timeout=7200)
    return dest


def is_media(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
