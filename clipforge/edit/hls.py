"""Fetch one stretch of a Twitch VOD without downloading the rest.

Twitch serves a VOD as ten-second fragmented-MP4 segments listed in a playlist.
yt-dlp's section download and ffmpeg's HLS seek both fail on these (they wrote
a 48-byte file after ten minutes, 13 Nov 2026), but the segments are plain HTTP
files: fetch the init segment and the few that cover the moment, join them, and
remux. Sixty seconds of 1080p60 is about 58 MB instead of an 18 GB VOD.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx

from ..core import media
from ..ingest import download

log = logging.getLogger("clipforge.hls")

SEGMENT_RETRIES = 4


class SectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Playlist:
    base: str
    init: str | None
    segments: list[tuple[str, float]]      # (uri, seconds)

    @property
    def duration(self) -> float:
        return sum(d for _, d in self.segments)


@dataclass(frozen=True)
class Section:
    path: Path
    offset: float          # source time of the file's first frame


def parse_playlist(text: str, base: str) -> Playlist:
    init = None
    segments: list[tuple[str, float]] = []
    pending: float | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            init = m.group(1) if m else None
        elif line.startswith("#EXTINF:"):
            pending = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and pending is not None:
            segments.append((line, pending))
            pending = None
    if not segments:
        raise SectionError("the playlist lists no segments")
    return Playlist(base=base, init=init, segments=segments)


def cover(playlist: Playlist, start: float, end: float) -> tuple[int, int, float]:
    """First and last segment index covering [start, end], and where the first begins."""
    if start < 0 or end <= start:
        raise SectionError(f"bad range {start}..{end}")
    if start >= playlist.duration:
        raise SectionError(f"{start:.0f}s is past the end of the video ({playlist.duration:.0f}s)")
    t = 0.0
    first = last = None
    offset = 0.0
    for i, (_, length) in enumerate(playlist.segments):
        if first is None and t + length > start:
            first, offset = i, t
        if first is not None:
            last = i
            if t + length >= end:
                break
        t += length
    assert first is not None and last is not None
    return first, last, offset


def playlist_url(url: str, max_height: int) -> str:
    from ..ingest import kick

    if kick.is_kick(url):
        try:
            return kick.playlist_url(url, max_height)
        except kick.KickError as exc:
            raise SectionError(str(exc)) from None
    out = download._run(
        download._ytdlp() + ["-g", "--no-playlist", "-f",
                             f"best[height<={max_height}]/bestvideo[height<={max_height}]+bestaudio",
                             url], timeout=120)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) != 1 or ".m3u8" not in lines[0]:
        raise SectionError("this video is not a single HLS stream, so it cannot be cut by segment")
    return lines[0]


def _get(client: httpx.Client, url: str) -> bytes:
    last: Exception | None = None
    for _ in range(SEGMENT_RETRIES):
        try:
            r = client.get(url)
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as exc:
            last = exc
    raise SectionError(f"could not fetch {url.rsplit('/', 1)[-1]}: {last}")


def is_youtube(url: str) -> bool:
    return bool(re.search(r"(?:youtube\.com|youtu\.be)/", url or ""))


def fetch_youtube_section(url: str, start: float, end: float, dest: Path, *, max_height: int,
                          beat: Callable[[], None] = lambda: None) -> Section:
    """A stretch of a YouTube video, cut by yt-dlp at exactly `start` (re-encoded, so no keyframe drift)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    args = download._ytdlp() + [
        "--no-playlist", "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
        "-f", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
        "--merge-output-format", "mp4", "-o", str(dest), url]
    try:
        download._run(args, timeout=1800, beat=beat)
    except (download.DownloadError, OSError) as exc:
        raise SectionError(f"yt-dlp could not cut {start:.0f}-{end:.0f}s: {str(exc)[-300:]}") from None
    if not dest.is_file() or dest.stat().st_size < 10_000:
        raise SectionError("yt-dlp wrote no usable section")
    return Section(path=dest, offset=start)


def fetch_section(url: str, start: float, end: float, dest: Path, *, max_height: int,
                  beat: Callable[[], None] = lambda: None) -> Section:
    """Write the video between start and end (padded to whole segments) to dest."""
    if is_youtube(url):
        return fetch_youtube_section(url, start, end, dest, max_height=max_height, beat=beat)
    dest.parent.mkdir(parents=True, exist_ok=True)
    m3u8 = playlist_url(url, max_height)
    base = m3u8.rsplit("/", 1)[0] + "/"
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        playlist = parse_playlist(_get(client, m3u8).decode("utf-8", "replace"), base)
        first, last, offset = cover(playlist, start, end)
        raw = dest.with_suffix(".raw.mp4" if playlist.init else ".raw.ts")
        with open(raw, "wb") as out:
            if playlist.init:
                out.write(_get(client, urljoin(base, playlist.init)))
            for i in range(first, last + 1):
                out.write(_get(client, urljoin(base, playlist.segments[i][0])))
                beat()
    adts = [] if playlist.init else ["-bsf:a", "aac_adtstoasc"]
    media.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(raw), "-c", "copy", *adts, str(dest)])
    raw.unlink(missing_ok=True)
    log.info("fetched %d segment(s) covering %.0f-%.0fs (%.0f MB)",
             last - first + 1, offset, offset + sum(d for _, d in playlist.segments[first:last + 1]),
             dest.stat().st_size / 1e6)
    return Section(path=dest, offset=offset)
