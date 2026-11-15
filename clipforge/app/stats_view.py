"""Numbers for the dashboard, computed from the posts/stats/payouts tables.

Before Phase 5 posts anything these are all zero, which is correct.
"""
from __future__ import annotations

import sqlite3

DEFAULTS = {"review_mode": "review", "auto_threshold": "60", "daily_cap": "3",
            "post_times": "10:00,14:00,18:00", "lkr_per_usd": "300"}


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
        out.append({**dict(r), "views": v,
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
