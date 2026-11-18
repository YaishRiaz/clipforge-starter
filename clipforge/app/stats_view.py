"""Numbers for the dashboard, computed from the posts/stats/payouts tables.

Before Phase 5 posts anything these are all zero, which is correct.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

DEFAULTS = {"review_mode": "review", "auto_threshold": "60", "daily_cap": "3",
            "post_times": "10:00,14:00,18:00", "prime_times": "", "lkr_per_usd": "300", "live_clips": "4"}


def latest_views(conn: sqlite3.Connection) -> dict[int, int]:
    return {r["post_id"]: r["views"] or 0 for r in conn.execute(
        "SELECT post_id, views FROM stats s WHERE captured_at = "
        "(SELECT MAX(captured_at) FROM stats WHERE post_id=s.post_id)")}


def views_gained(conn: sqlite3.Connection, days: float) -> int:
    """Views added in the last `days`, summed over posts."""
    total = 0
    for post_id, now in latest_views(conn).items():
        before = conn.execute(
            "SELECT views FROM stats WHERE post_id=? AND captured_at <= datetime('now', ?) "
            "ORDER BY captured_at DESC LIMIT 1", (post_id, f"-{days} days")).fetchone()
        total += max(0, now - ((before["views"] or 0) if before else 0))
    return total


def estimate(views: int, rate: float | None, cap: float | None, min_views: int | None) -> float:
    if not rate or views < (min_views or 0):
        return 0.0
    value = views * rate / 1000.0
    return round(min(value, cap) if cap else value, 2)


def post_rows(conn: sqlite3.Connection) -> list[dict]:
    views = latest_views(conn)
    out = []
    for r in conn.execute(
        "SELECT p.*, c.meta_json, k.id AS campaign_id, k.name AS campaign, k.rate_per_1k, "
        "k.clip_cap, k.min_views FROM posts p JOIN clips c ON c.id=p.clip_id "
        "JOIN moments m ON m.id=c.moment_id JOIN sources s ON s.id=m.source_id "
        "JOIN campaigns k ON k.id=s.campaign_id ORDER BY p.id DESC"):
        v = views.get(r["id"], 0)
        try:
            title = json.loads(r["meta_json"] or "{}").get("title")
        except json.JSONDecodeError:
            title = None
        out.append({**dict(r), "views": v, "title": title or f"Clip {r['clip_id']}",
                    "estimate": estimate(v, r["rate_per_1k"], r["clip_cap"], r["min_views"])})
    return out


def overview(conn: sqlite3.Connection, lkr_per_usd: float) -> dict:
    posts = post_rows(conn)
    approved = sum(p["estimate"] for p in posts if p["campaign_review_status"] == "approved")
    pending = sum(p["estimate"] for p in posts if p["campaign_review_status"] == "pending")
    actual = conn.execute("SELECT COALESCE(SUM(amount_usd),0) FROM payouts").fetchone()[0]
    reviewed = [p for p in posts if p["campaign_review_status"] != "pending"]
    return {
        "views_today": views_gained(conn, 1), "views_7d": views_gained(conn, 7),
        "views_30d": views_gained(conn, 30),
        "approved_usd": approved, "pending_usd": pending, "actual_usd": actual,
        "lkr_per_usd": lkr_per_usd, "posted": len(posts),
        "approval_rate": (sum(p["campaign_review_status"] == "approved" for p in reviewed)
                          / len(reviewed)) if reviewed else None,
        "top": sorted(posts, key=lambda p: p["views"], reverse=True)[:5],
    }


def settings_map(conn: sqlite3.Connection) -> dict[str, str]:
    from ..core import db

    return {k: db.kv_get(conn, f"setting_{k}", v) or v for k, v in DEFAULTS.items()}


def series(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """Every recorded view count of each post, oldest first."""
    out: dict[int, list[int]] = {}
    for r in conn.execute("SELECT post_id, views FROM stats ORDER BY captured_at"):
        out.setdefault(r["post_id"], []).append(r["views"] or 0)
    return out


def spark(values: list[int], width: int = 120, height: int = 30) -> str:
    """SVG polyline points for a small trend line; empty when there is nothing to draw."""
    if len(values) < 2:
        return ""
    top = max(values) or 1
    step = (width - 4) / (len(values) - 1)
    return " ".join(f"{2 + i * step:.1f},{height - 3 - (v / top) * (height - 6):.1f}"
                    for i, v in enumerate(values))


def daily_gains(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    """Views gained on each of the last `days` days (UTC), all posts together."""
    totals = []
    for back in range(days, -1, -1):
        row = conn.execute(
            "SELECT COALESCE(SUM(v),0) FROM (SELECT (SELECT views FROM stats s WHERE s.post_id=p.id "
            "AND s.captured_at < datetime('now','start of day', ?) ORDER BY captured_at DESC LIMIT 1) v "
            "FROM posts p)", (f"{1 - back} days",)).fetchone()[0]
        day = (datetime.now(timezone.utc) - timedelta(days=back)).strftime("%d %b")
        totals.append((day, int(row or 0)))
    out = [{"day": d, "views": max(0, t - totals[i][1])} for i, (d, t) in enumerate(totals[1:])]
    top = max((o["views"] for o in out), default=0) or 1
    for o in out:
        o["pct"] = round(100 * o["views"] / top)
    return out


def review_counts(posts: list[dict]) -> dict:
    out = {"posted": len(posts), "submitted": sum(bool(p["submitted_at"]) for p in posts),
           "approved": 0, "rejected": 0, "pending": 0}
    for p in posts:
        out[p["campaign_review_status"]] += 1
    return out


def campaign_images(conn: sqlite3.Connection) -> dict[int, str]:
    """Thumbnail of each campaign, from the listing it was joined from."""
    return {r["cid"]: r["image"] for r in conn.execute(
        "SELECT k.id cid, d.image FROM campaigns k JOIN discovered d "
        "ON d.external_id=k.external_id AND d.source=k.platform WHERE d.image LIKE 'https://%'")}


def geo_summary(rows) -> dict | None:
    """{'total', 'tier1' (share 0-1), 'top': [{country, views, pct}]} for (country, views) rows."""
    from ..stats.collect import TIER1

    by: dict[str, int] = {}
    for country, views in rows:
        by[country] = by.get(country, 0) + int(views)
    total = sum(by.values())
    if not total:
        return None
    top = sorted(by.items(), key=lambda kv: -kv[1])[:6]
    return {"total": total, "tier1": sum(v for c, v in by.items() if c in TIER1) / total,
            "top": [{"country": c, "views": v, "pct": round(100 * v / total),
                     "tier1": c in TIER1} for c, v in top]}


def geo(conn: sqlite3.Connection) -> dict:
    """Viewer geography: overall, per post and per campaign, plus how the last fetch went."""
    from ..core import db

    rows = conn.execute(
        "SELECT g.post_id, g.country, g.views, s.campaign_id FROM geo g JOIN posts p ON p.id=g.post_id "
        "JOIN clips c ON c.id=p.clip_id JOIN moments m ON m.id=c.moment_id "
        "JOIN sources s ON s.id=m.source_id").fetchall()
    posts: dict[int, list] = {}
    camps: dict[int, list] = {}
    for r in rows:
        posts.setdefault(r["post_id"], []).append((r["country"], r["views"]))
        camps.setdefault(r["campaign_id"], []).append((r["country"], r["views"]))
    try:
        channel = json.loads(db.kv_get(conn, "geo_channel", "") or "{}")
    except json.JSONDecodeError:
        channel = {}
    # The channel-wide report is fuller: YouTube withholds the split for small videos.
    overall = list(channel.items()) or [(r["country"], r["views"]) for r in rows]
    return {"status": db.kv_get(conn, "geo_status", "") or "never",
            "all": geo_summary(overall),
            "posts": {k: geo_summary(v) for k, v in posts.items()},
            "campaigns": {k: geo_summary(v) for k, v in camps.items()}}


DRAIN_DAYS = 3            # warn when a campaign's budget would be gone within this many days
DRAIN_MIN_HOURS = 6       # a burn rate from less history than this is noise


def budgets(conn: sqlite3.Connection) -> dict[int, dict]:
    """Each joined campaign's live budget, its burn per day and a warning when it is nearly gone."""
    out: dict[int, dict] = {}
    for d in conn.execute(
            "SELECT id, campaign_id, budget_left, budget_total, ended FROM discovered "
            "WHERE campaign_id IS NOT NULL").fetchall():
        log = conn.execute(
            "SELECT budget_left, julianday(captured_at) AS at FROM budget_log WHERE discovered_id=? "
            "AND captured_at >= datetime('now', '-3 days') ORDER BY captured_at, rowid",
            (d["id"],)).fetchall()
        per_day = None
        if len(log) >= 2:
            hours = (log[-1]["at"] - log[0]["at"]) * 24
            spent = log[0]["budget_left"] - log[-1]["budget_left"]
            if hours >= DRAIN_MIN_HOURS and spent > 0:
                per_day = spent / hours * 24
        left, total = d["budget_left"], d["budget_total"]
        days = left / per_day if per_day and left is not None else None
        warn = None
        if d["ended"]:
            warn = "This campaign has ended"
        elif left is not None and left <= 0:
            warn = "The budget is used up"
        elif days is not None and days < DRAIN_DAYS:
            warn = f"At this rate the budget runs out in about {max(1, round(days))} day{'s' if round(days) > 1 else ''}"
        elif left is not None and total and left / total < 0.05:
            warn = "Under 5% of the budget is left"
        out[d["campaign_id"]] = {"left": left, "total": total, "per_day": per_day,
                                 "days_left": days, "warn": warn}
    return out
