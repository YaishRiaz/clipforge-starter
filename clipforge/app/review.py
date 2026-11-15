"""The review flow: what Yaish can do to a finished clip (PRD sections 8 and 10).

Approving only marks a clip ready to post. Publishing is Phase 5, and nothing
here uploads anything.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..edit import compliance, layout as layout_mod, meta as meta_mod, package
from ..ingest import briefs

# A clip can only move between these; a posted clip is never touched from here.
MOVES = {
    "ready": {"approved", "rejected"},
    "approved": {"ready"},
    "rejected": {"ready"},
    "held": {"rejected"},
}
TITLE_MAX = meta_mod.TITLE_MAX


class ReviewError(ValueError):
    pass


def get(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT c.*, m.score AS score, m.hook AS hook, s.id AS source_id, s.creator AS creator, "
        "s.campaign_id AS campaign_id, k.name AS campaign_name, k.creator AS campaign_creator "
        "FROM clips c JOIN moments m ON m.id=c.moment_id JOIN sources s ON s.id=m.source_id "
        "JOIN campaigns k ON k.id=s.campaign_id WHERE c.id=?", (clip_id,)).fetchone()
    if row is None:
        raise ReviewError(f"no clip {clip_id}")
    return row


def meta_of(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["meta_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def move(conn: sqlite3.Connection, clip_id: int, to: str, note: str | None = None) -> sqlite3.Row:
    row = get(conn, clip_id)
    if to not in MOVES.get(row["status"], set()):
        raise ReviewError(f"clip {clip_id} is {row['status']}; it cannot become {to}")
    if to == "approved" and not row["compliance_ok"]:
        raise ReviewError(f"clip {clip_id} failed the brief check and cannot be approved")
    meta = meta_of(row)
    if note:
        meta["review_note"] = note[:300]
    conn.execute("UPDATE clips SET status=?, meta_json=? WHERE id=?",
                 (to, json.dumps(meta, ensure_ascii=False), clip_id))
    return get(conn, clip_id)


def approve(conn, clip_id: int) -> sqlite3.Row:
    return move(conn, clip_id, "approved")


def reject(conn, clip_id: int, note: str | None = None) -> sqlite3.Row:
    return move(conn, clip_id, "rejected", note)


def restore(conn, clip_id: int) -> sqlite3.Row:
    return move(conn, clip_id, "ready")


def edit(conn: sqlite3.Connection, clip_id: int, *, title: str, description: str) -> sqlite3.Row:
    """Change the title and description. Refused if it breaks something the brief requires."""
    row = get(conn, clip_id)
    if row["status"] not in ("ready", "held", "approved"):
        raise ReviewError(f"clip {clip_id} is {row['status']}; its text can no longer change")
    title, description = title.strip(), description.strip()
    if not title or len(title) > TITLE_MAX:
        raise ReviewError(f"the title must be 1-{TITLE_MAX} characters")
    if not description:
        raise ReviewError("the description cannot be empty")
    meta = meta_of(row)
    trial = dict(meta, title=title, description=description)
    rules = briefs.load_rules(conn, row["campaign_id"])
    lay = layout_mod.load(row["creator"] or row["campaign_creator"])
    findings = compliance.check(rules=rules, layout=lay, meta=trial,
                                duration=float(meta.get("duration") or 30), width=1080,
                                height=1920, has_audio=True, overlay_burned=True,
                                hook=meta.get("hook") or "", spoken="")
    broken = [f for f in findings if f.level == "FAIL"
              and f.code in ("hashtag", "mention", "disclosure", "caption")]
    if broken:
        raise ReviewError("; ".join(f.message for f in broken))
    meta.update(title=title, description=description)
    if row["path"] and row["thumb_path"] and Path(row["path"]).is_file():
        package.write(Path(row["path"]).parent, clip=Path(row["path"]),
                      thumb=Path(row["thumb_path"]), meta=meta)
    conn.execute("UPDATE clips SET meta_json=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False), clip_id))
    return get(conn, clip_id)


def by_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        "SELECT c.id FROM clips c WHERE c.status IN (" + marks + ") ORDER BY c.id", statuses
    ).fetchall()
