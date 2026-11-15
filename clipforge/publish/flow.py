"""Publishing: which approved clip goes out next, the upload job, and submission.

Nothing is posted unless the clip is approved by Yaish (review mode) or scores
above his threshold (auto mode), passed the compliance check, belongs to a
campaign he joined, and a pacing slot is open.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..app import review, stats_view
from ..core import db, queue
from ..core.config import Settings
from . import pacing, youtube

log = logging.getLogger("clipforge.publish")

SUBMIT_WINDOW_MIN = 25
JOB = "publish_clip"
FMT = "%Y-%m-%d %H:%M:%S"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(FMT)


def to_local(text: str) -> datetime:
    return datetime.strptime(text, FMT).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def posted_times(conn: sqlite3.Connection) -> list[datetime]:
    return [to_local(r["posted_at"]) for r in conn.execute(
        "SELECT posted_at FROM posts WHERE posted_at IS NOT NULL")]


def candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Approved clips that are allowed to go out, best score first."""
    return conn.execute(
        "SELECT c.id, c.moment_id, m.score FROM clips c "
        "JOIN moments m ON m.id=c.moment_id JOIN sources s ON s.id=m.source_id "
        "JOIN campaigns k ON k.id=s.campaign_id "
        "WHERE c.status='approved' AND c.compliance_ok=1 AND k.joined_at IS NOT NULL "
        "AND k.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM posts p JOIN clips c2 ON c2.id=p.clip_id "
        "                WHERE c2.moment_id=c.moment_id AND c2.status!='rejected') "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.type=? AND j.ref_id=c.id "
        "                AND j.status='failed') "
        "ORDER BY m.score DESC, c.id", (JOB,)).fetchall()


def auto_approve(conn: sqlite3.Connection, threshold: float) -> int:
    n = 0
    for r in conn.execute(
            "SELECT c.id FROM clips c JOIN moments m ON m.id=c.moment_id "
            "WHERE c.status='ready' AND c.compliance_ok=1 AND m.score >= ?", (threshold,)).fetchall():
        try:
            review.approve(conn, r["id"])
            n += 1
        except review.ReviewError:
            pass
    return n


def schedule(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> dict:
    """The pacing numbers shared by `tick` and the dashboard."""
    cfg = stats_view.settings_map(conn)
    posted = posted_times(conn)
    now = now or datetime.now()
    cap = pacing.effective_cap(_int(cfg["daily_cap"], 3), settings.posts_per_day_ceiling,
                               min(posted, default=None), now)
    return {"cfg": cfg, "posted": posted, "now": now, "cap": cap,
            "times": pacing.parse_times(cfg["post_times"]), "gap": settings.min_gap_hours}


def next_slot_text(conn: sqlite3.Connection, settings: Settings) -> str:
    s = schedule(conn, settings)
    slot = pacing.next_slot(s["now"], s["times"], s["cap"], s["posted"], s["gap"])
    if slot is None:
        return "no posting times are set"
    when = "today" if slot.date() == s["now"].date() else "tomorrow"
    return f"{when} around {slot:%H:%M}"


def tick(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> int | None:
    """Called every few seconds: in auto mode approve, then queue one upload if a
    slot is open. Returns the clip id queued, or None."""
    s = schedule(conn, settings, now)
    cfg = s["cfg"]
    if cfg["review_mode"] == "auto":
        auto_approve(conn, float(_int(cfg["auto_threshold"], 60)))
    if not youtube.configured(settings):
        return None
    if conn.execute("SELECT 1 FROM jobs WHERE type=? AND status IN ('queued','running')",
                    (JOB,)).fetchone():
        return None
    cands = candidates(conn)
    if not cands or not pacing.is_due(s["now"], s["times"], s["cap"], s["posted"], s["gap"]):
        return None
    clip_id = int(cands[0]["id"])
    queue.enqueue(conn, JOB, clip_id)
    return clip_id


def enqueue_now(conn: sqlite3.Connection, clip_id: int) -> None:
    """Skip the pacing slot for one clip (manual override). Still needs approval."""
    if not any(r["id"] == clip_id for r in candidates(conn)):
        raise ValueError(f"clip {clip_id} is not approved and eligible to post")
    queue.enqueue(conn, JOB, clip_id)


# ---- the job ---------------------------------------------------------------

def _eligible(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row:
    row = review.get(conn, clip_id)
    if row is None:
        raise RuntimeError(f"no clip {clip_id}")
    if row["status"] == "posted" or conn.execute(
            "SELECT 1 FROM posts WHERE clip_id=?", (clip_id,)).fetchone():
        return row
    if not any(r["id"] == clip_id for r in candidates(conn)):
        raise RuntimeError(f"clip {clip_id} is no longer approved and eligible; not uploading")
    if not Path(row["path"] or "").is_file():
        raise RuntimeError(f"clip {clip_id} file is missing")
    return row


def stage_upload(ctx) -> None:
    conn, clip_id = ctx.conn, ctx.ref_id
    row = _eligible(conn, clip_id)
    if db.kv_get(conn, f"yt_video_{clip_id}"):
        return
    meta = review.meta_of(row)
    video = youtube.upload(
        ctx.settings, Path(row["path"]), title=meta["title"], description=meta["description"],
        tags=meta.get("hashtags", []),
        session=db.kv_get(conn, f"yt_session_{clip_id}"),
        on_session=lambda s: db.kv_set(conn, f"yt_session_{clip_id}", s),
        beat=ctx.beat)
    db.kv_set(conn, f"yt_video_{clip_id}", video["id"])


def stage_record(ctx) -> None:
    conn, clip_id = ctx.conn, ctx.ref_id
    if conn.execute("SELECT 1 FROM posts WHERE clip_id=?", (clip_id,)).fetchone():
        return
    video_id = db.kv_get(conn, f"yt_video_{clip_id}")
    if not video_id:
        raise RuntimeError("upload stage left no video id")
    now = datetime.now(timezone.utc)
    deadline = datetime.fromtimestamp(now.timestamp() + SUBMIT_WINDOW_MIN * 60, timezone.utc)
    conn.execute(
        "INSERT INTO posts (clip_id, publisher, video_id, url, posted_at, submit_deadline) "
        "VALUES (?,?,?,?,?,?)",
        (clip_id, "youtube", video_id, f"https://www.youtube.com/shorts/{video_id}",
         now.strftime(FMT), deadline.strftime(FMT)))
    conn.execute("UPDATE clips SET status='posted' WHERE id=?", (clip_id,))
    conn.execute(
        "UPDATE sources SET last_clip_posted_at=? WHERE id=(SELECT m.source_id FROM clips c "
        "JOIN moments m ON m.id=c.moment_id WHERE c.id=?)", (now.strftime(FMT), clip_id))


def stage_verify(ctx) -> None:
    """Best effort: confirm the video is public and on the intended channel."""
    conn, settings = ctx.conn, ctx.settings
    video_id = db.kv_get(conn, f"yt_video_{ctx.ref_id}")
    if not (video_id and settings.youtube_api_key):
        return
    item = youtube.lookup(settings, [video_id]).get(video_id)
    if item is None:
        log.info("video %s not visible to the API yet; skipping verification", video_id)
        return
    if settings.youtube_channel_id and item["snippet"]["channelId"] != settings.youtube_channel_id:
        raise RuntimeError(f"video {video_id} landed on channel {item['snippet']['channelId']}, "
                           "not the configured YOUTUBE_CHANNEL_ID")
    status = item.get("status", {}).get("privacyStatus")
    if status != "public":
        raise RuntimeError(f"video {video_id} is {status}, not public")


# ---- submission ------------------------------------------------------------

def mark_submitted(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if post is None:
        raise ValueError(f"no post {post_id}")
    if not post["submitted_at"]:
        conn.execute("UPDATE posts SET submitted_at=? WHERE id=?", (utc_now(), post_id))
        conn.execute("UPDATE clips SET status='submitted' WHERE id=? AND status='posted'",
                     (post["clip_id"],))
    return conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()


def submit_page(conn: sqlite3.Connection, post_id: int) -> str | None:
    row = conn.execute(
        "SELECT k.brief_url FROM posts p JOIN clips c ON c.id=p.clip_id "
        "JOIN moments m ON m.id=c.moment_id JOIN sources s ON s.id=m.source_id "
        "JOIN campaigns k ON k.id=s.campaign_id WHERE p.id=?", (post_id,)).fetchone()
    return row["brief_url"] if row else None


def minutes_since(posted_at: str) -> float:
    return (datetime.now(timezone.utc) - datetime.strptime(posted_at, FMT).replace(
        tzinfo=timezone.utc)).total_seconds() / 60


def copy_ready(row: sqlite3.Row) -> str:
    meta = review.meta_of(row)
    return f"Title:\n{meta.get('title', '')}\n\nDescription:\n{meta.get('description', '')}"

