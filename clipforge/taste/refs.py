"""Finding and fetching reference Shorts for a niche.

Reference videos are watched, measured, and deleted: they are never edited,
re-uploaded or kept. Only the measurements stay (see analyze.py).
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.config import Settings
from ..ingest import download
from ..moments.rubric_build import API, QUERIES, BuildError, _get, parse_duration

log = logging.getLogger("clipforge.taste.refs")

MIN_SECONDS, MAX_SECONDS = 15, 60
NICHE_QUERIES = {
    "gta": QUERIES,
    "gaming": ("gaming funny moments", "gaming clips shorts", "gaming rage moments"),
    "minecraft": ("minecraft funny moments", "minecraft clips shorts", "minecraft challenge shorts"),
    "streamer": ("streamer funny moments", "twitch clips shorts", "stream highlights shorts"),
    "entertainment": ("funny moments shorts", "stream highlights shorts"),
}


def queries_for(niche: str) -> tuple[str, ...]:
    return NICHE_QUERIES.get(niche, (f"{niche} funny moments", f"{niche} clips", f"{niche} shorts"))


def refs_dir(settings: Settings, niche: str) -> Path:
    d = settings.data_dir / "taste" / niche / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find(settings: Settings, niche: str, *, n: int = 12, days: int = 180,
         queries: tuple[str, ...] | None = None, get=_get) -> list[dict]:
    """The niche's most-viewed recent Shorts: [{id, title, channel, views, seconds, url}].

    At most one per channel, so one creator's habits do not pass for the niche's.
    """
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for q in queries or queries_for(niche):
        data = get("search", {"part": "id", "q": q, "type": "video", "videoDuration": "short",
                              "order": "viewCount", "maxResults": 25, "publishedAfter": after,
                              "relevanceLanguage": "en"}, settings)
        ids += [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
    ids = list(dict.fromkeys(ids))
    found: list[dict] = []
    for k in range(0, len(ids), 50):
        data = get("videos", {"part": "snippet,statistics,contentDetails",
                              "id": ",".join(ids[k:k + 50])}, settings)
        for v in data.get("items", []):
            secs = parse_duration(v.get("contentDetails", {}).get("duration", ""))
            views = int(v.get("statistics", {}).get("viewCount", 0) or 0)
            if MIN_SECONDS <= secs <= MAX_SECONDS and views > 0:
                found.append({"id": v["id"], "title": v["snippet"]["title"],
                              "channel": v["snippet"].get("channelId", ""), "views": views,
                              "seconds": secs, "url": f"https://www.youtube.com/shorts/{v['id']}"})
    found.sort(key=lambda v: v["views"], reverse=True)
    out, seen = [], set()
    for v in found:
        if v["channel"] in seen:
            continue
        seen.add(v["channel"])
        out.append(v)
        if len(out) >= n:
            break
    return out


def fetch(url: str, dest_dir: Path, *, timeout: float = 300) -> Path:
    """Download one reference Short (720p at most) and return its file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    vid = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    for old in dest_dir.glob(f"{vid}.*"):
        if old.suffix == ".mp4":
            return old
    args = download._ytdlp() + [
        "--no-playlist", "--no-progress", "-f", "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format", "mp4", "--retries", "3", "-o", str(dest_dir / f"{vid}.%(ext)s"), url]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    got = sorted(dest_dir.glob(f"{vid}.*"))
    got = [g for g in got if g.suffix in (".mp4", ".webm", ".mkv")]
    if proc.returncode != 0 or not got:
        raise BuildError(f"could not download {url}: {(proc.stderr or '')[-300:]}")
    return got[0]


__all__ = ["API", "BuildError", "find", "fetch", "refs_dir", "queries_for"]
