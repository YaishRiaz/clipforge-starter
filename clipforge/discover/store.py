"""The discovered-campaign list: refresh it, filter it, and record Yaish's choices.

Joining a campaign is Yaish's click on the platform. This module only tracks the
campaign and, once he says he joined, creates the campaign row ClipForge works
from (`joined_at` = the moment he says it, which is the clip-after limit).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Callable

import httpx

from ..core import db, queue
from . import sources as src

log = logging.getLogger("clipforge.discover")

REFRESH_EVERY_HOURS = 3.0
KV_REFRESHED = "discover_refreshed_at"
SORTS = {
    "rate": "rate_max DESC, budget_left DESC",
    "budget": "budget_left DESC",
    "new": "first_seen_at DESC, id DESC",
    "seen": "last_seen_at DESC",
}


def due(conn: sqlite3.Connection, hours: float = REFRESH_EVERY_HOURS) -> bool:
    stamp = db.kv_get(conn, KV_REFRESHED)
    if not stamp:
        return True
    return conn.execute("SELECT ? < datetime('now', ?)", (stamp, f"-{hours} hours")).fetchone()[0] == 1


def _upsert(conn: sqlite3.Connection, f: src.Found) -> bool:
    """Insert or refresh one row, keeping Yaish's state. True when it is new."""
    existing = conn.execute("SELECT id FROM discovered WHERE source=? AND external_id=?",
                            (f.source, f.external_id)).fetchone()
    values = (f.title, f.brand, f.category, json.dumps(f.platforms), f.rate_min, f.rate_max,
              f.budget_total, f.budget_left, int(f.requires_application), int(f.locked),
              int(f.ended), int(f.gaming), f.url, f.image, f.ends_at, f.min_views)
    if existing:
        conn.execute(
            "UPDATE discovered SET title=?, brand=?, category=?, platforms_json=?, rate_min=?, "
            "rate_max=?, budget_total=?, budget_left=?, requires_application=?, locked=?, "
            "ended=?, gaming=?, url=?, image=?, ends_at=?, min_views=?, "
            "last_seen_at=datetime('now') WHERE id=?", (*values, existing["id"]))
        return False
    conn.execute(
        "INSERT INTO discovered (title, brand, category, platforms_json, rate_min, rate_max, "
        "budget_total, budget_left, requires_application, locked, ended, gaming, url, image, "
        "ends_at, min_views, source, external_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*values, f.source, f.external_id))
    return True


def refresh(conn: sqlite3.Connection, *,
            fetch: Callable[[str], str] = src.fetch_page,
            connectors: dict | None = None) -> dict:
    """Read each platform's public list. Returns counts and the new rows' ids."""
    connectors = connectors or src.CONNECTORS
    report: dict = {"seen": 0, "new": [], "errors": []}
    for name, (urls, parse) in connectors.items():
        for url in urls:
            try:
                found = parse(fetch(url))
            except Exception as exc:                  # one site down must not stop the rest
                report["errors"].append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
                log.warning("discover %s failed: %s", url, exc)
                continue
            for f in found:
                report["seen"] += 1
                if _upsert(conn, f):
                    row = conn.execute("SELECT id FROM discovered WHERE source=? AND external_id=?",
                                       (f.source, f.external_id)).fetchone()
                    report["new"].append(int(row["id"]))
    conn.execute(
        "UPDATE discovered SET state='joined', campaign_id=(SELECT k.id FROM campaigns k "
        "WHERE k.external_id=discovered.external_id) "
        "WHERE state != 'joined' AND EXISTS (SELECT 1 FROM campaigns k "
        "WHERE k.external_id=discovered.external_id "
        "AND k.joined_at IS NOT NULL)")
    if report["seen"]:
        db.kv_set(conn, KV_REFRESHED, conn.execute("SELECT datetime('now')").fetchone()[0])
    return report


def query(conn: sqlite3.Connection, *, source: str = "", gaming: bool = False,
          youtube: bool = True, min_rate: float = 0, min_budget: float = 0,
          hide_application: bool = False, hide_locked: bool = True, text: str = "",
          state: str = "", sort: str = "rate", limit: int = 200) -> list[dict]:
    where = ["ended=0"]
    params: list = []
    if source:
        where.append("source=?")
        params.append(source)
    if gaming:
        where.append("gaming=1")
    if youtube:
        where.append("platforms_json LIKE '%\"youtube\"%'")
    if min_rate:
        where.append("COALESCE(rate_max, 0) >= ?")
        params.append(min_rate)
    if min_budget:
        where.append("COALESCE(budget_left, 0) >= ?")
        params.append(min_budget)
    if hide_application:
        where.append("requires_application=0")
    if hide_locked:
        where.append("locked=0")
    if text:
        where.append("(title LIKE ? OR brand LIKE ? OR category LIKE ?)")
        params += [f"%{text}%"] * 3
    if state:
        where.append("state=?")
        params.append(state)
    else:
        where.append("state != 'hidden'")
    sql = (f"SELECT * FROM discovered WHERE {' AND '.join(where)} "
           f"ORDER BY {SORTS.get(sort, SORTS['rate'])} LIMIT ?")
    rows = []
    for r in conn.execute(sql, (*params, limit)).fetchall():
        d = dict(r)
        d["platforms"] = json.loads(d.pop("platforms_json") or "[]")
        rows.append(d)
    return rows


def get(conn: sqlite3.Connection, discovered_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM discovered WHERE id=?", (discovered_id,)).fetchone()
    if row is None:
        raise ValueError(f"no discovered campaign {discovered_id}")
    return row


def set_state(conn: sqlite3.Connection, discovered_id: int, state: str) -> None:
    if state not in ("new", "watching", "hidden"):
        raise ValueError(f"cannot set state {state!r} by hand")
    row = get(conn, discovered_id)
    if row["state"] == "joined":
        raise ValueError("already joined")
    conn.execute("UPDATE discovered SET state=? WHERE id=?", (state, discovered_id))


_DOC = re.compile(r"docs\.google\.com/document/d/([\w-]+)")


def brief_from_link(link: str, client: httpx.Client | None = None) -> str | None:
    """Text of a Google Doc brief Yaish can already read, else None."""
    m = _DOC.search(link)
    if not m:
        return None
    url = f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt"
    try:
        own = client or httpx.Client(timeout=30, follow_redirects=True)
        try:
            r = own.get(url)
        finally:
            if client is None:
                own.close()
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or "<html" in r.text[:200].lower():
        return None
    return r.text.strip() or None


def mark_joined(conn: sqlite3.Connection, discovered_id: int, *, brief: str = "",
                client: httpx.Client | None = None) -> int:
    """Yaish says he joined. Creates the active campaign and queues the brief parse.

    `brief` is pasted text or a Google Doc link. Without one, the campaign is
    created but nothing can be ingested until the brief is added.
    """
    row = get(conn, discovered_id)
    if row["state"] == "joined" and row["campaign_id"]:
        return int(row["campaign_id"])
    brief = (brief or "").strip()
    text = None
    link = None
    if brief.lower().startswith("http"):
        link = brief
        text = brief_from_link(brief, client)
    elif brief:
        text = brief
    platforms = json.loads(row["platforms_json"])
    conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, creator, category, rate_per_1k, "
        "min_views, budget_left, deadline, allowed_platforms, brief_url, brief_text, status, "
        "joined_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',datetime('now')) "
        "ON CONFLICT (platform, external_id) DO UPDATE SET status='active', "
        "joined_at=COALESCE(campaigns.joined_at, datetime('now')), "
        "brief_text=COALESCE(excluded.brief_text, campaigns.brief_text), "
        "brief_url=COALESCE(excluded.brief_url, campaigns.brief_url)",
        (row["source"], row["external_id"], row["title"], row["brand"], row["category"],
         row["rate_max"], row["min_views"], row["budget_left"], row["ends_at"],
         json.dumps(platforms), link or row["url"], text))
    campaign_id = int(conn.execute(
        "SELECT id FROM campaigns WHERE platform=? AND external_id=?",
        (row["source"], row["external_id"])).fetchone()["id"])
    conn.execute("UPDATE discovered SET state='joined', campaign_id=? WHERE id=?",
                 (campaign_id, discovered_id))
    if text:
        queue.enqueue(conn, "parse_brief", ref_id=campaign_id)
    log.info("campaign %s created from discovered %s (brief %s)", campaign_id, discovered_id,
             "saved" if text else "missing")
    return campaign_id
