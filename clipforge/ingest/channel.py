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
    started_at: float | None = None       # epoch seconds, where the site says
    games: tuple[str, ...] | None = None  # None: the site did not say
    live: bool = False


def videos_url(channel_url: str) -> str:
    root = channel_root(channel_url)
    if "twitch.tv" in root:
        return root + "/videos?filter=archives"
    return root + "/videos"


def list_vods(channel_url: str, limit: int = LISTING_LIMIT) -> list[Vod]:
    """The channel's newest past broadcasts, newest first (no downloads)."""
    from . import kick

    if kick.is_kick(channel_url):
        return [Vod(url=v.url, title=v.title, duration=v.duration, started_at=v.started_at,
                    games=v.games, live=v.live) for v in kick.list_vods(channel_url)[:limit]]
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


def _ends_inside(rules, vod: Vod) -> bool:
    """A broadcast that began before the footage start but ran past it (a long stream that
    rolled into launch day) still holds eligible footage; its window is set when it is mined."""
    if not rules.footage_after or vod.started_at is None or not vod.duration:
        return False
    from .rules import _epoch

    ok_game, _ = rules.footage_ok(None, vod.games)
    return ok_game and vod.started_at + vod.duration > _epoch(rules.footage_after)


GAME_START = "game_start:"          # kv prefix: seconds into a VOD where the brief's game began


def eligible_from(conn: sqlite3.Connection, rules, vod: Vod) -> float:
    """Seconds into the VOD where eligible footage begins: the brief's start time, and the
    moment the live watcher saw the brief's game come on, whichever is later."""
    from ..core import db
    from .rules import _epoch

    start = 0.0
    if rules.footage_after and vod.started_at is not None:
        start = max(start, _epoch(rules.footage_after) - vod.started_at)
    seen = db.kv_get(conn, GAME_START + normalize_url(vod.url))
    if seen:
        start = max(start, float(seen))
    return start


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
            if vod.live:
                continue                  # still being written; the live watcher has it
            ok, why = rules.footage_ok(vod.started_at, vod.games)
            if not ok and not _ends_inside(rules, vod):
                log.info("channel scan skips %s: %s", vod.url, why)
                continue
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
            start = eligible_from(conn, rules, vod)
            if start > 0:
                from .briefs import set_window

                set_window(conn, campaign_id, vod.url, start)
            new.append(sid)
            room -= 1
    if new:
        log.info("campaign %s: %d new VOD(s) from approved channels", campaign_id, len(new))
    return new
