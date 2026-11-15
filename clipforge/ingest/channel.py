"""New footage from a channel the campaign approves wholesale.

Some briefs approve a creator's whole output ("SkiiMaskTimmy's Twitch streams,
live or VODs") rather than named videos. Yaish records that decision once, as an
approved channel on the campaign (`campaign-rules --add-channel`). From then on
this module lists the channel's newest VODs and registers them, newest first,
up to a daily cap that protects the free LLM budget.

Registration still goes through `sources.register`, so the campaign must be
active and joined, and the channel must be one Yaish approved.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from ..core.config import Settings
from . import download, sources
from .briefs import load_rules
from .rules import channel_root, normalize_url

log = logging.getLogger("clipforge.ingest.channel")

LISTING_LIMIT = 15


@dataclass
class Vod:
    url: str
    title: str
    duration: float | None


def videos_url(channel_url: str) -> str:
    root = channel_root(channel_url)
    if "twitch.tv" in root:
        return root + "/videos?filter=archives"
    return root + "/videos"


def list_vods(channel_url: str, limit: int = LISTING_LIMIT) -> list[Vod]:
    """The channel's newest past broadcasts, newest first (no downloads)."""
    out = download._run(
        download._ytdlp() + ["--flat-playlist", "--dump-single-json",
                             "--playlist-end", str(limit), videos_url(channel_url)],
        timeout=180)
    try:
        entries = json.loads(out).get("entries") or []
    except json.JSONDecodeError as exc:
        raise download.DownloadError(f"no listing came back for {channel_url}") from exc
    vods = []
    for e in entries:
        url = e.get("url") or e.get("webpage_url")
        if not url or e.get("live_status") == "is_live":
            continue
        dur = e.get("duration")
        vods.append(Vod(url=url, title=e.get("title") or "", duration=dur))
    return vods


def taken_today(conn: sqlite3.Connection, campaign_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sources WHERE campaign_id=? AND url IS NOT NULL "
        "AND created_at >= datetime('now','-1 day')", (campaign_id,)).fetchone()[0]


def scan(conn: sqlite3.Connection, settings: Settings, campaign_id: int, *,
         lister=list_vods) -> list[int]:
    """Register new VODs from each approved channel. Returns the new source ids."""
    ok, why = sources.campaign_may_ingest(conn, campaign_id)
    rules = load_rules(conn, campaign_id)
    if not ok or rules is None or not rules.approved_channels:
        return []
    room = settings.sources_per_day - taken_today(conn, campaign_id)
    new: list[int] = []
    for channel in rules.approved_channels:
        if room <= 0:
            break
        try:
            vods = lister(channel)
        except Exception as exc:
            log.warning("channel %s: listing failed: %s", channel, str(exc)[:200])
            continue
        for vod in vods:
            if room <= 0:
                break
            # Footage never crosses campaigns (hard limit 2): a VOD already taken for
            # any campaign is off the table, even when two campaigns share a creator.
            known = conn.execute("SELECT 1 FROM sources WHERE url=?",
                                 (normalize_url(vod.url),)).fetchone()
            if known:
                continue
            try:
                sid = sources.register(
                    conn, campaign_id, url=vod.url, title=vod.title, rules=rules,
                    media_kind="audio", from_channel=channel, length_approved=True)
            except sources.NotApproved as exc:
                log.warning("channel scan refused: %s", exc)
                return new
            new.append(sid)
            room -= 1
    if new:
        log.info("campaign %s: %d new VOD(s) from approved channels", campaign_id, len(new))
    return new
