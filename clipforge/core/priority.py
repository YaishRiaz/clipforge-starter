"""Campaigns that jump the queue.

Yaish's rule (18 Nov 2026): from the moment GTA VI is released, every GTA VI campaign gets
maximum priority. A launch is when the most views are there to be had, and footage of it ages
by the hour. Priority means three things:

- its clips post before any other approved clip (still in the normal posting slots, still
  only after approval and the brief's own opening time);
- its jobs (downloads, mining, live stretches, clip making) run before other work;
- the live watcher takes more of its streams (live.PRIORITY_CLIPS).

A campaign is a GTA VI campaign when its name, brief or footage rules name the game. Yaish can
force any campaign on or off with the kv key `campaign_priority_<id>` ("1" or "0"), and move the
release time with `priority_gta6_from` (ISO, UTC).
"""
from __future__ import annotations

import re
import sqlite3
import time

from . import db

GTA6_FROM = "2026-11-19T00:00:00Z"        # GTA VI release day
KV_FROM = "priority_gta6_from"
KV_FORCE = "campaign_priority_{}"
JOB_PRIORITY = 30                          # above live work (10), below a due upload (50)

_GTA6 = re.compile(r"\b(gta\s*(6|vi)|grand\s+theft\s+auto\s+(6|vi))\b", re.I)


def _epoch(iso: str) -> float:
    from ..ingest.rules import _epoch as parse

    return parse(iso)


def is_gta6(conn: sqlite3.Connection, campaign_id: int) -> bool:
    row = conn.execute("SELECT name, brief_text, rules_json FROM campaigns WHERE id=?",
                       (campaign_id,)).fetchone()
    if row is None:
        return False
    return any(_GTA6.search(row[k] or "") for k in ("name", "brief_text", "rules_json"))


def released(conn: sqlite3.Connection, now: float | None = None) -> bool:
    return (now or time.time()) >= _epoch(db.kv_get(conn, KV_FROM, GTA6_FROM) or GTA6_FROM)


def is_priority(conn: sqlite3.Connection, campaign_id: int | None, now: float | None = None) -> bool:
    if campaign_id is None:
        return False
    forced = db.kv_get(conn, KV_FORCE.format(campaign_id))
    if forced in ("0", "1"):
        return forced == "1"
    return released(conn, now) and is_gta6(conn, campaign_id)


def campaigns(conn: sqlite3.Connection, now: float | None = None) -> set[int]:
    return {int(r["id"]) for r in conn.execute(
        "SELECT id FROM campaigns WHERE status='active' AND joined_at IS NOT NULL").fetchall()
        if is_priority(conn, int(r["id"]), now)}


def job_priority(conn: sqlite3.Connection, campaign_id: int | None, base: int = 0) -> int:
    return max(base, JOB_PRIORITY) if is_priority(conn, campaign_id) else base


# Which campaign a queued job works for, by its type.
_CAMPAIGN_OF = {
    "ingest_source": "SELECT campaign_id FROM sources WHERE id=?",
    "mine_source": "SELECT campaign_id FROM sources WHERE id=?",
    "make_clip": "SELECT s.campaign_id FROM moments m JOIN sources s ON s.id=m.source_id WHERE m.id=?",
}


def boost(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Raise every waiting job of a priority campaign above other work. Returns how many."""
    wanted = campaigns(conn, now)
    if not wanted:
        return 0
    n = 0
    for job in conn.execute("SELECT id, type, ref_id, payload_json FROM jobs "
                            "WHERE status='queued' AND priority < ?", (JOB_PRIORITY,)).fetchall():
        cid = None
        if job["type"] in _CAMPAIGN_OF and job["ref_id"] is not None:
            row = conn.execute(_CAMPAIGN_OF[job["type"]], (job["ref_id"],)).fetchone()
            cid = row[0] if row else None
        elif job["type"] == "live_moment":
            m = re.search(r'"campaign_id":\s*(\d+)', job["payload_json"] or "")
            cid = int(m.group(1)) if m else None
        if cid in wanted:
            conn.execute("UPDATE jobs SET priority=? WHERE id=?", (JOB_PRIORITY, job["id"]))
            n += 1
    return n
