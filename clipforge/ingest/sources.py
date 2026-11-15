"""Registering source footage, and the gate that decides what may be ingested.

This module is where hard limits 1, 2 and 7 live, so every path into `sources`
goes through `register`:

1. only footage a joined campaign names as approved, plus Yaish's own recordings;
2. never footage from another campaign;
7. never footage ingested for a campaign before its `joined_at`
   (`docs/verification.md` design change 7).

A refusal raises `NotApproved` and is recorded, not worked around.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from ..core.config import Settings
from .briefs import load_rules
from .rules import BriefRules, normalize_url

log = logging.getLogger("clipforge.ingest.sources")

OWN_CAMPAIGN_NAME = "Own recordings"


class NotApproved(RuntimeError):
    """The footage is not approved for this campaign. Never retried around."""


class NoDiskSpace(RuntimeError):
    """Free space is under the floor; downloads pause (PRD section 5)."""


def own_campaign_id(conn: sqlite3.Connection) -> int:
    """The row every `inbox/own/` recording belongs to, created on first use."""
    row = conn.execute(
        "SELECT id FROM campaigns WHERE platform='own' AND external_id='own'"
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at, "
        "allowed_platforms, country_ok) "
        "VALUES ('own', 'own', ?, 'active', datetime('now'), ?, 1)",
        (OWN_CAMPAIGN_NAME, '["youtube_shorts"]'),
    )
    log.info("created the own-footage campaign row")
    return int(cur.lastrowid)


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 2**30


def check_disk(settings: Settings) -> None:
    free = free_gb(settings.data_dir if settings.data_dir.exists() else settings.root)
    if free < settings.disk_floor_gb:
        raise NoDiskSpace(
            f"{free:.1f} GB free is under the {settings.disk_floor_gb:.0f} GB floor; "
            f"delete finished sources before downloading more"
        )


def campaign_may_ingest(conn: sqlite3.Connection, campaign_id: int) -> tuple[bool, str]:
    """Is this campaign one we are allowed to pull footage for right now?"""
    row = conn.execute(
        "SELECT platform, name, status, joined_at FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    if row is None:
        return False, f"no campaign {campaign_id}"
    if row["platform"] == "own":
        return True, ""
    if row["status"] != "active":
        return False, (f"campaign {campaign_id} ({row['name']}) is '{row['status']}', not active; "
                       f"Yaish joins a campaign himself before anything is ingested")
    if not row["joined_at"]:
        return False, (f"campaign {campaign_id} ({row['name']}) has no joined_at; "
                       f"nothing may be clipped for a campaign we have not joined")
    return True, ""


def register(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    url: str | None = None,
    path: Path | None = None,
    title: str | None = None,
    creator: str | None = None,
    published_at: str | None = None,
    rules: BriefRules | None = None,
    media_kind: str = "video",
    from_channel: str | None = None,
    length_approved: bool = False,
) -> int:
    """Record one piece of source footage, if the campaign approves it.

    Returns the source id, reusing the existing row when the same footage is
    offered twice. Raises NotApproved when the gate refuses.
    """
    if url is None and path is None:
        raise ValueError("register needs a url or a path")

    ok, why = campaign_may_ingest(conn, campaign_id)
    if not ok:
        raise NotApproved(why)

    is_own = conn.execute(
        "SELECT platform FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()["platform"] == "own"

    if url and not is_own:
        rules = rules if rules is not None else load_rules(conn, campaign_id)
        if rules is None:
            raise NotApproved(
                f"campaign {campaign_id} has no parsed brief yet, so nothing is approved; "
                f"run the parse_brief job first"
            )
        via_channel = bool(from_channel) and rules.approves_channel(from_channel)
        if from_channel and not via_channel:
            raise NotApproved(
                f"{from_channel} is not an approved channel for campaign {campaign_id}; "
                f"approve it with: python -m clipforge campaign-rules {campaign_id} "
                f"--add-channel {from_channel}"
            )
        if not via_channel and not rules.approves_url(url):
            raise NotApproved(
                f"{url} is not named as approved footage in campaign {campaign_id}'s brief "
                f"(hard limit 1). Approved links: "
                f"{rules.approved_source_urls or 'none in the brief'}"
            )

    stored_url = normalize_url(url) if url else None
    stored_path = str(path.resolve()) if path else None

    if stored_url:
        row = conn.execute(
            "SELECT id FROM sources WHERE campaign_id=? AND url=?", (campaign_id, stored_url)
        ).fetchone()
        if row:
            return int(row["id"])
    if stored_path:
        row = conn.execute(
            "SELECT id FROM sources WHERE campaign_id=? AND path=?", (campaign_id, stored_path)
        ).fetchone()
        if row:
            return int(row["id"])

    # Footage already on disk needs no download; a link does.
    status = "downloaded" if stored_path else "new"
    if media_kind not in ("video", "audio"):
        raise ValueError(f"media_kind must be 'video' or 'audio', not {media_kind!r}")
    cur = conn.execute(
        "INSERT INTO sources (campaign_id, creator, title, url, path, published_at, status, "
        "media_kind, length_approved, via_channel) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, creator, title, stored_url, stored_path, published_at, status,
         media_kind, int(length_approved), int(bool(url and not is_own and via_channel))),
    )
    source_id = int(cur.lastrowid)
    log.info("registered source %s for campaign %s (%s)",
             source_id, campaign_id, stored_url or stored_path)
    return source_id


def hold(conn: sqlite3.Connection, source_id: int, reason: str) -> None:
    """Park a source for Yaish to decide on, with the reason on the record."""
    conn.execute(
        "UPDATE sources SET status='held', held_reason=? WHERE id=?", (reason, source_id)
    )
    log.warning("source %s held: %s", source_id, reason)


def release(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    """Lift a hold, because Yaish approved this source (PRD section 5).

    Records that its length is approved, so re-running the job does not hold it
    again, and puts it back where the pipeline can pick it up.
    """
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"no source {source_id}")
    has_file = bool(row["path"]) and Path(row["path"]).is_file()
    conn.execute(
        "UPDATE sources SET status=?, held_reason=NULL, length_approved=1 WHERE id=?",
        ("downloaded" if has_file else "new", source_id),
    )
    log.info("source %s released by hand (was: %s)", source_id, row["held_reason"])
    return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def mark(conn: sqlite3.Connection, source_id: int, status: str, **fields: object) -> None:
    """Set a source's status, and any columns alongside it."""
    sets = ["status=?"]
    values: list[object] = [status]
    for key, value in fields.items():
        sets.append(f"{key}=?")
        values.append(value)
    values.append(source_id)
    conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", values)


def campaign_of(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT c.* FROM campaigns c JOIN sources s ON s.campaign_id=c.id WHERE s.id=?",
        (source_id,),
    ).fetchone()
