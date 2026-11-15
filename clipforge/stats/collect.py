"""Read public statistics for every posted video (PRD section 10).

Uses the read-only API key, so no OAuth and no audit. Each run appends one row
per video to `stats`; the dashboard shows the latest and the differences.
"""
from __future__ import annotations

import sqlite3
import time

from ..core import db
from ..core.config import Settings
from ..publish import flow, youtube

INTERVAL_SECONDS = 6 * 3600
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
