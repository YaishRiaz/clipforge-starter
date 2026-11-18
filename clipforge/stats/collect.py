"""Read public statistics for every posted video (PRD section 10).

Uses the read-only API key, so no OAuth and no audit. Each run appends one row
per video to `stats`; the dashboard shows the latest and the differences.
"""
from __future__ import annotations

import json
import sqlite3
import time

from ..core import db
from ..core.config import Settings
from ..publish import flow, youtube

INTERVAL_SECONDS = 6 * 3600
GEO_INTERVAL_SECONDS = 24 * 3600   # analytics lag a day or two, so once a day is plenty
# The countries clipping campaigns usually mean by "Tier 1".
TIER1 = ("US", "GB", "CA", "AU", "NZ")
GRACE_MINUTES = 60          # a brand-new video may not be listed yet


def due(conn: sqlite3.Connection, now: float | None = None) -> bool:
    last = float(db.kv_get(conn, "stats_last", "0") or 0)
    return (now or time.time()) - last >= INTERVAL_SECONDS


def collect(conn: sqlite3.Connection, settings: Settings) -> tuple[int, list[str]]:
    """Returns (rows written, alerts). An alert is raised once per video."""
    posts = conn.execute(
        "SELECT id, clip_id, video_id, posted_at FROM posts WHERE video_id IS NOT NULL").fetchall()
    db.kv_set(conn, "stats_last", str(time.time()))
    if not posts:
        return 0, []
    found = youtube.lookup(settings, [p["video_id"] for p in posts])
    written, alerts = 0, []
    for p in posts:
        item = found.get(p["video_id"])
        if item is None:
            if flow.minutes_since(p["posted_at"]) > GRACE_MINUTES and not db.kv_get(
                    conn, f"stat_gone_{p['id']}"):
                db.kv_set(conn, f"stat_gone_{p['id']}", "1")
                alerts.append(f"Clip {p['clip_id']} ({p['video_id']}) can no longer be found on "
                              "YouTube: removed, private, or blocked.")
            continue
        st = item.get("statistics", {})
        conn.execute("INSERT OR REPLACE INTO stats (post_id, views, likes, comments) VALUES (?,?,?,?)",
                     (p["id"], int(st.get("viewCount", 0)), int(st.get("likeCount", 0)),
                      int(st.get("commentCount", 0))))
        written += 1
        privacy = item.get("status", {}).get("privacyStatus")
        if privacy != "public" and not db.kv_get(conn, f"stat_priv_{p['id']}"):
            db.kv_set(conn, f"stat_priv_{p['id']}", "1")
            alerts.append(f"Clip {p['clip_id']} ({p['video_id']}) is {privacy}, not public.")
    return written, alerts


def geo_due(conn: sqlite3.Connection, now: float | None = None) -> bool:
    last = float(db.kv_get(conn, "geo_last", "0") or 0)
    return (now or time.time()) - last >= GEO_INTERVAL_SECONDS


def collect_geo(conn: sqlite3.Connection, settings: Settings, *, fetch=None, token=None) -> int:
    """Refresh every post's views per country. Returns posts updated.

    `geo_status` in kv says how it went: ok, needs_consent, or the error. A post
    with no rows yet (analytics lag behind by a day or two) keeps what it had.
    """
    from datetime import date

    db.kv_set(conn, "geo_last", str(time.time()))
    posts = conn.execute(
        "SELECT id, video_id, posted_at FROM posts WHERE video_id IS NOT NULL").fetchall()
    if not posts:
        return 0
    if not youtube.configured(settings):
        db.kv_set(conn, "geo_status", "needs_consent")
        return 0
    fetch = fetch or youtube.views_by_country
    done = 0
    try:
        token = token or youtube.access_token(settings)
        first = min((p["posted_at"] or "")[:10] or "2026-01-01" for p in posts)
        channel = fetch(token, None, first, date.today().isoformat())
        db.kv_set(conn, "geo_channel", json.dumps(channel))
        for p in posts:
            rows = fetch(token, p["video_id"], (p["posted_at"] or "")[:10] or "2026-01-01",
                         date.today().isoformat())
            if not rows:
                continue
            conn.execute("DELETE FROM geo WHERE post_id=?", (p["id"],))
            conn.executemany("INSERT INTO geo (post_id, country, views) VALUES (?,?,?)",
                             [(p["id"], c, v) for c, v in rows.items() if v > 0])
            done += 1
    except youtube.NeedsConsent:
        db.kv_set(conn, "geo_status", "needs_consent")
        return done
    except youtube.YouTubeError as exc:
        db.kv_set(conn, "geo_status", str(exc)[:200])
        return done
    db.kv_set(conn, "geo_status", "ok")
    return done
