"""Cross-post kits: the free route to TikTok and Instagram.

ClipForge never touches those apps. After a clip is live on YouTube, the kit
(video plus copy-ready caption per platform the campaign allows) goes to Telegram
and Yaish posts it from his own phone and account.
"""
from __future__ import annotations

import json
import sqlite3

from ..app import review

PLATFORMS = {"tiktok": "TikTok", "instagram": "Instagram Reels"}


def platforms_for(conn: sqlite3.Connection, clip_id: int) -> list[str]:
    row = conn.execute(
        "SELECT k.allowed_platforms FROM clips c JOIN moments m ON m.id=c.moment_id "
        "JOIN sources s ON s.id=m.source_id JOIN campaigns k ON k.id=s.campaign_id "
        "WHERE c.id=?", (clip_id,)).fetchone()
    try:
        allowed = [str(p).lower() for p in json.loads((row and row["allowed_platforms"]) or "[]")]
    except json.JSONDecodeError:
        allowed = []
    return [p for p in PLATFORMS if any(a.startswith(p) for a in allowed)]


def caption(row: sqlite3.Row) -> str:
    meta = review.meta_of(row)
    title = (meta.get("title") or "").strip()
    desc = (meta.get("description") or "").strip()
    return f"{title}\n\n{desc}".strip() if title and title not in desc else desc


def kit(conn: sqlite3.Connection, clip_id: int) -> list[tuple[str, str]]:
    """(platform label, caption) for each platform this clip may go to by hand."""
    row = review.get(conn, clip_id)
    return [(PLATFORMS[p], caption(row)) for p in platforms_for(conn, clip_id)] if row else []
