"""Keep the pipeline fed without Yaish typing a command between stages.

One pass, every 15 minutes (a repeating job, like the inbox scan):

1. approved channels: register new VODs, newest first, up to SOURCES_PER_DAY;
2. downloaded sources (tagged, or caption-less and still to be transcribed): queue
   mining, within the same daily budget of distinct sources,
   because mining is what spends the free LLM tokens;
3. when fewer than CLIP_BUFFER clips are waiting (ready, approved, or being
   cut), queue the best unclipped moments of joined campaigns.

It never posts and never approves anything: review mode and the posting slots
stay exactly as Yaish set them. Turn it off with setting `autopilot` = off.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from ..core import db, queue
from ..core.config import Settings
from ..ingest import channel

log = logging.getLogger("clipforge.autopilot")

JOB = "autopilot"
EVERY = 15 * 60


def enabled(conn: sqlite3.Connection) -> bool:
    return (db.kv_get(conn, "setting_autopilot", "on") or "on").lower() not in ("off", "0", "no")


def _active_job(conn: sqlite3.Connection, job_type: str, ref_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM jobs WHERE type=? AND ref_id=? AND status IN ('queued','running')",
        (job_type, ref_id)).fetchone() is not None


def clips_waiting(conn: sqlite3.Connection) -> int:
    made = conn.execute(
        "SELECT COUNT(*) FROM clips c WHERE c.status IN ('ready','approved') "
        "AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.clip_id=c.id)").fetchone()[0]
    cutting = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE type='make_clip' AND status IN ('queued','running')"
    ).fetchone()[0]
    return made + cutting


def alert_new_campaigns(conn: sqlite3.Connection, ids: list[int], notify) -> int:
    """Tell Yaish about new open gaming campaigns that allow YouTube. Returns how many."""
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT title, brand, rate_max, budget_left, url FROM discovered WHERE id IN ({marks}) "
        "AND gaming=1 AND ended=0 AND locked=0 AND requires_application=0 "
        "AND platforms_json LIKE '%\"youtube\"%' AND COALESCE(rate_max,0) >= 1 "
        "AND COALESCE(budget_left,0) >= 1000 ORDER BY rate_max DESC LIMIT 5", ids
    ).fetchall() if ids else []
    if rows:
        lines = [f"{r['brand']}: {r['title']} (${r['rate_max']:g}/1k, ${r['budget_left']:,.0f} left)\n{r['url']}"
                 for r in rows]
        notify("New gaming campaigns that allow YouTube:\n\n" + "\n\n".join(lines))
    return len(rows)


def discover_pass(conn: sqlite3.Connection, settings: Settings, *, fetch=None, notify=None) -> dict:
    from ..discover import store

    if not store.due(conn):
        return {}
    kwargs = {"fetch": fetch} if fetch else {}
    report = store.refresh(conn, **kwargs)
    if report["new"]:
        if notify is None:
            from ..app import telegram as tg

            def notify(text: str) -> None:
                if tg.configured(settings):
                    tg.send(settings, text)
        try:
            alert_new_campaigns(conn, report["new"], notify)
        except Exception as exc:
            log.warning("campaign alert failed: %s", type(exc).__name__)
    return report


def run(conn: sqlite3.Connection, settings: Settings, *, lister=channel.list_vods,
        fetch=None, notify=None) -> dict:
    out = {"new_sources": 0, "mining": 0, "clipping": 0}
    if not enabled(conn):
        return out
    try:
        discover_pass(conn, settings, fetch=fetch, notify=notify)
    except Exception as exc:                          # discovery never blocks clipping
        log.warning("discover pass failed: %s", type(exc).__name__)

    for row in conn.execute(
            "SELECT id FROM campaigns WHERE status='active' AND joined_at IS NOT NULL "
            "AND platform != 'own'").fetchall():
        for sid in channel.scan(conn, settings, row["id"], lister=lister):
            queue.enqueue(conn, "ingest_source", ref_id=sid)
            out["new_sources"] += 1

    mined_today = conn.execute(
        "SELECT COUNT(DISTINCT ref_id) FROM jobs WHERE type='mine_source' "
        "AND created_at >= ?", (time.time() - 86400,)).fetchone()[0]
    room = settings.sources_per_day - mined_today
    for row in conn.execute(
            "SELECT s.id FROM sources s JOIN campaigns k ON k.id=s.campaign_id "
            "WHERE s.status IN ('tagged','downloaded') AND s.path IS NOT NULL "
            "AND k.status='active' AND k.joined_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM moments m WHERE m.source_id=s.id) "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.type='mine_source' AND j.ref_id=s.id "
            "                AND j.status='failed') "
            "ORDER BY s.id").fetchall():
        if room <= 0:
            break
        if _active_job(conn, "mine_source", row["id"]):
            continue
        queue.enqueue(conn, "mine_source", ref_id=row["id"])
        out["mining"] += 1
        room -= 1

    deficit = settings.clip_buffer - clips_waiting(conn)
    if deficit > 0:
        for row in conn.execute(
                "SELECT m.id FROM moments m JOIN sources s ON s.id=m.source_id "
                "JOIN campaigns k ON k.id=s.campaign_id "
                "WHERE m.status='candidate' AND k.status='active' AND k.joined_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.type='make_clip' AND j.ref_id=m.id "
                "                AND j.status IN ('queued','running','failed')) "
                "AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.moment_id=m.id) "
                "ORDER BY m.score DESC, m.id LIMIT ?", (deficit,)).fetchall():
            queue.enqueue(conn, "make_clip", ref_id=row["id"])
            out["clipping"] += 1

    if any(out.values()):
        log.info("autopilot: %s", out)
    return out


def ensure_scheduled(conn: sqlite3.Connection) -> bool:
    """Make sure one repeating autopilot job exists. True if one was added."""
    if conn.execute("SELECT 1 FROM jobs WHERE type=? AND status IN ('queued','running')",
                    (JOB,)).fetchone():
        return False
    queue.enqueue(conn, JOB, payload={"repeat_seconds": EVERY}, dedupe=False, priority=-10)
    return True


def runway(conn: sqlite3.Connection) -> dict:
    """Clips on hand against posting slots until the warm-up date (setting `warmup_date`)."""
    import datetime as dt

    cap = int(db.kv_get(conn, "setting_daily_cap", "3") or 3)
    ready = conn.execute(
        "SELECT COUNT(*) FROM clips c WHERE c.status IN ('ready','approved') "
        "AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.clip_id=c.id)").fetchone()[0]
    approved = conn.execute(
        "SELECT COUNT(*) FROM clips c WHERE c.status='approved' "
        "AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.clip_id=c.id)").fetchone()[0]
    out = {"ready": ready, "approved": approved, "cap": cap, "date": None, "days": None,
           "slots": None, "short": None}
    stamp = (db.kv_get(conn, "setting_warmup_date", "") or "").strip()
    try:
        target = dt.date.fromisoformat(stamp)
    except ValueError:
        return out
    days = max(0, (target - dt.date.today()).days + 1)
    out.update(date=stamp, days=days, slots=days * cap, short=max(0, days * cap - ready))
    return out
