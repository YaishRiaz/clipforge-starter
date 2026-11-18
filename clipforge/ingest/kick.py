"""Kick: live status, VOD listing and VOD playlists, from Kick's own public endpoints.

yt-dlp's Kick extractor needs a browser-impersonation add-on this laptop does not have, so the
pieces ClipForge needs are read directly. Everything here is read-only and needs no account:

- `kick.com/api/v2/channels/<slug>` says whether a channel is live and what it is playing.
- `kick.com/api/v2/channels/<slug>/videos` lists past broadcasts (and the one being written).
- `kick.com/api/v1/video/<uuid>` gives a VOD's HLS playlist, also while the stream is still live.

Live chat is not read: Kick now hands out chat connections only through its own web app or its
official developer API (an app registered on Yaish's Kick account). Live Kick moments are found
from the stream itself instead (see `ingest.live`).

A Kick VOD link looks like https://kick.com/<slug>/videos/<uuid>.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

log = logging.getLogger("clipforge.ingest.kick")

API = "https://kick.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}

_VOD = re.compile(r"kick\.com/([A-Za-z0-9_-]+)/videos/([0-9a-f-]{36})", re.I)
_CHANNEL = re.compile(r"kick\.com/([A-Za-z0-9_-]+)/?$", re.I)


class KickError(RuntimeError):
    pass


def is_kick(url: str | None) -> bool:
    return "kick.com/" in (url or "").lower()


def slug_of(url: str) -> str | None:
    """The channel name, from a channel link or a VOD link."""
    m = _VOD.search(url or "") or _CHANNEL.search((url or "").split("?")[0].rstrip("/"))
    return m.group(1).lower() if m else None


def vod_uuid(url: str) -> str | None:
    m = _VOD.search(url or "")
    return m.group(2).lower() if m else None


def vod_url(slug: str, uuid: str) -> str:
    return f"https://kick.com/{slug}/videos/{uuid}"


def _epoch(text: str | None) -> float | None:
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if "T" not in text:
        text = text.replace(" ", "T")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)            # Kick writes UTC without a zone
    return dt.timestamp()


def _get_json(url: str, get=None):
    if get is None:
        import httpx

        def get(u):
            return httpx.get(u, headers=HEADERS, timeout=30.0, follow_redirects=True)
    r = get(url)
    if r.status_code != 200:
        raise KickError(f"{url.rsplit('/api/', 1)[-1]}: HTTP {r.status_code}")
    return r.json()


# ---- channel, live status, VODs ---------------------------------------------

@dataclass(frozen=True)
class Live:
    slug: str
    stream_id: str
    started_at: float
    title: str
    games: tuple[str, ...]
    vod_url: str | None


def stream_info(slug: str, *, get=None) -> Live | None:
    """The channel's current broadcast, or None when it is offline."""
    ch = _get_json(f"{API}/v2/channels/{slug}", get)
    ls = ch.get("livestream")
    if not ls or not ls.get("is_live"):
        return None
    started = _epoch(ls.get("start_time") or ls.get("created_at")) or time.time()
    vod = None
    try:
        for v in _get_json(f"{API}/v2/channels/{slug}/videos", get) or []:
            if v.get("id") == ls.get("id") or (v.get("video") or {}).get("live_stream_id") == ls.get("id"):
                uuid = (v.get("video") or {}).get("uuid")
                vod = vod_url(slug, uuid) if uuid else None
                break
    except KickError as exc:
        log.info("kick %s: no VOD listing yet (%s)", slug, exc)
    return Live(slug=slug, stream_id=str(ls.get("id")), started_at=started,
                title=ls.get("session_title") or "", games=tuple(_games(ls)),
                vod_url=vod)


def _games(row: dict) -> list[str]:
    out = [c.get("name") for c in (row.get("categories") or []) if c.get("name")]
    cat = row.get("category") or {}
    if cat.get("name") and cat["name"] not in out:
        out.append(cat["name"])
    return out


@dataclass(frozen=True)
class Vod:
    url: str
    title: str
    duration: float | None
    started_at: float | None
    games: tuple[str, ...] = field(default_factory=tuple)
    live: bool = False


def list_vods(channel_url: str, *, get=None) -> list[Vod]:
    """The channel's broadcasts, newest first, the one being written included (marked live)."""
    slug = slug_of(channel_url)
    if not slug:
        raise KickError(f"not a Kick channel: {channel_url}")
    out = []
    for v in _get_json(f"{API}/v2/channels/{slug}/videos", get) or []:
        uuid = (v.get("video") or {}).get("uuid")
        if not uuid:
            continue
        dur = v.get("duration")
        out.append(Vod(url=vod_url(slug, uuid), title=v.get("session_title") or "",
                       duration=(dur / 1000.0) if dur else None,          # Kick counts milliseconds
                       started_at=_epoch(v.get("start_time") or v.get("created_at")),
                       games=tuple(_games(v)), live=bool(v.get("is_live"))))
    return out


def master_url(url: str, *, get=None) -> str:
    uuid = vod_uuid(url)
    if not uuid:
        raise KickError(f"not a Kick VOD link: {url}")
    src = (_get_json(f"{API}/v1/video/{uuid}", get) or {}).get("source")
    if not src or ".m3u8" not in src:
        raise KickError(f"Kick gave no playlist for {uuid}")
    return src


def pick_variant(master: str, base: str, max_height: int) -> str:
    """The tallest rendition no taller than max_height (the smallest when none fits)."""
    options: list[tuple[int, str]] = []
    pending = None
    for line in master.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            pending = int(m.group(1)) if m else 0
        elif line and not line.startswith("#") and pending is not None:
            options.append((pending, urljoin(base, line)))
            pending = None
    if not options:
        raise KickError("the Kick playlist lists no renditions")
    fitting = [o for o in options if o[0] <= max_height]
    return max(fitting)[1] if fitting else min(options)[1]


def playlist_url(url: str, max_height: int, *, get=None) -> str:
    """The media playlist of one rendition of a Kick VOD (live or finished)."""
    import httpx

    master = master_url(url, get=get)
    r = httpx.get(master, headers=HEADERS, timeout=30.0, follow_redirects=True) if get is None else get(master)
    if r.status_code != 200:
        raise KickError(f"Kick playlist: HTTP {r.status_code}")
    return pick_variant(r.text, master.rsplit("/", 1)[0] + "/", max_height)


def fetch(url: str, dest_dir: Path, settings, *, audio_only: bool = False, allow_long: bool = False,
          beat=None):
    """Download a whole Kick VOD (audio only for mining) with ffmpeg reading its playlist."""
    from ..core import media
    from . import download

    uuid = vod_uuid(url)
    slug = slug_of(url)
    info = next((v for v in list_vods(f"https://kick.com/{slug}") if vod_uuid(v.url) == uuid), None)
    if info and info.live:
        raise download.DownloadError("this Kick broadcast is still live; it is mined once it ends")
    cap = settings.max_source_hours * 3600
    if info and info.duration and not allow_long and info.duration > cap:
        raise download.TooLong(f"{info.duration / 3600:.1f} h is over the {settings.max_source_hours:g} h cap "
                               "(MAX_SOURCE_HOURS); approve it with: python -m clipforge source-release <id>")
    m3u8 = playlist_url(url, 160 if audio_only else settings.max_height)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (f"kick-{uuid}.m4a" if audio_only else f"kick-{uuid}.mp4")
    part = dest.with_name(dest.stem + ".part" + dest.suffix)
    codec = ["-vn", "-c:a", "copy"] if audio_only else ["-c", "copy", "-bsf:a", "aac_adtstoasc"]
    log.info("downloading Kick VOD %s (%s)", uuid, "audio only" if audio_only else "video")
    download._run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                   "-i", m3u8, *codec, str(part)], timeout=6 * 3600, beat=beat)
    part.replace(dest)
    started = info.started_at if info else None
    return download.Downloaded(
        path=dest, title=info.title if info else None, creator=slug,
        duration=info.duration if info else None,
        published_at=datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%d") if started else None,
        language=None)
