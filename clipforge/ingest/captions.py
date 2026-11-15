"""Captions that come with the footage.

PRD section 6 step 1: use supplied captions when they exist and only transcribe
locally when they do not. On this laptop that is the difference between seconds
and roughly real time, so it is always worth looking first.

The segment list written here is the same shape Phase 2's transcriber produces,
so everything downstream reads one format.
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..core import media

log = logging.getLogger("clipforge.ingest.captions")

_TIMESTAMP = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
# YouTube's auto-captions carry per-word timing tags and styling inside the cue.
_INLINE_TAG = re.compile(r"<[^>]+>")


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _seconds(hours: str | None, minutes: str, seconds: str, frac: str) -> float:
    return (int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
            + int(frac.ljust(3, "0")) / 1000)


def parse_vtt(text: str) -> list[Segment]:
    """Read WebVTT or SRT into segments, in order, with the cruft stripped.

    Auto-generated captions repeat the previous line at the top of each cue as a
    rolling window; those repeats are dropped so the transcript reads once.
    """
    segments: list[Segment] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        match = _TIMESTAMP.search(lines[i])
        if not match:
            i += 1
            continue
        g = match.groups()
        start = _seconds(g[0], g[1], g[2], g[3])
        end = _seconds(g[4], g[5], g[6], g[7])
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip() and not _TIMESTAMP.search(lines[i]):
            body.append(lines[i])
            i += 1
        cue = " ".join(body)
        cue = html.unescape(_INLINE_TAG.sub("", cue))
        cue = re.sub(r"\s+", " ", cue).strip()
        if not cue:
            continue
        if segments and cue == segments[-1].text:
            segments[-1].end = max(segments[-1].end, end)
            continue
        if segments:
            previous = segments[-1].text
            if cue.startswith(previous + " "):
                cue = cue[len(previous) + 1:].strip()
            elif previous.endswith(cue):
                continue
        if cue and end > start:
            segments.append(Segment(start, end, cue))
    return segments


def find_sidecar(video: Path) -> Path | None:
    """A .vtt or .srt the downloader wrote next to the video, English first."""
    candidates = sorted(
        p for p in video.parent.glob(f"{glob_escape(video.stem)}*")
        if p.suffix.lower() in (".vtt", ".srt")
    )
    if not candidates:
        return None
    english = [p for p in candidates if re.search(r"\.en(-[A-Za-z0-9]+)?\.", p.name)]
    return (english or candidates)[0]


def glob_escape(stem: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", stem)


def from_media(video: Path, work_dir: Path) -> list[Segment]:
    """Captions for this file: a sidecar file, else an embedded subtitle track."""
    sidecar = find_sidecar(video)
    if sidecar is not None:
        segments = parse_vtt(sidecar.read_text(encoding="utf-8", errors="replace"))
        if segments:
            log.info("using supplied captions %s (%d segments)", sidecar.name, len(segments))
            return segments
    try:
        probe = media.probe(video)
    except media.MediaError as exc:
        log.warning("could not probe %s for subtitles: %s", video.name, exc)
        return []
    for index in probe.subtitle_streams:
        dest = work_dir / f"{video.stem}.embedded.{index}.vtt"
        try:
            media.extract_subtitle(video, index, dest)
        except media.MediaError as exc:
            log.warning("subtitle stream %s of %s would not extract: %s", index, video.name, exc)
            continue
        segments = parse_vtt(dest.read_text(encoding="utf-8", errors="replace"))
        if segments:
            log.info("using embedded subtitle track %s (%d segments)", index, len(segments))
            return segments
    return []


def save(segments: list[Segment], dest: Path, *, source: str) -> Path:
    """Write the transcript Phase 2 and Phase 3 read."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {"source": source, "segments": [asdict(s) for s in segments]},
            indent=1, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return dest


def load(path: Path) -> list[Segment]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Segment(float(s["start"]), float(s["end"]), s["text"]) for s in data["segments"]]


def as_text(segments: list[Segment], *, with_times: bool = True) -> str:
    """The transcript as the LLM sees it."""
    if not with_times:
        return " ".join(s.text for s in segments)
    return "\n".join(f"[{int(s.start // 60):02d}:{s.start % 60:05.2f}] {s.text}" for s in segments)


def word_count(segments: list[Segment]) -> int:
    return sum(len(s.text.split()) for s in segments)
