"""Publishing: which approved clip goes out next, the upload job, and submission.

Nothing is posted unless the clip is approved by Yaish (review mode) or scores
above his threshold (auto mode), passed the compliance check, belongs to a
campaign he joined, and a pacing slot is open.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
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


RETRY_AFTER = 3600       # a failed upload (an outage, a rate limit) is tried again after this long
MAX_FAILURES = 2         # a clip that failed this many times waits for Yaish


def candidates(conn: sqlite3.Connection, now: float | None = None) -> list[dict]:
    """Approved clips that are allowed to go out: priority campaigns first, then best score.
    A campaign whose brief opens later (`post_from`) has nothing eligible until then."""
    from ..core import priority
    from ..ingest.briefs import load_rules

    now = time.time() if now is None else now
    rows = _approved(conn)
    rules, prio, out = {}, {}, []
    for r in rows:
        cid = r["campaign_id"]
        if cid not in rules:
            rules[cid] = load_rules(conn, cid)
            prio[cid] = priority.is_priority(conn, cid, now)
        if rules[cid] is not None and not rules[cid].may_post(now):
            continue
        out.append({**dict(r), "priority": prio[cid]})
    out.sort(key=lambda c: not c["priority"])          # stable: score order stays inside each group
    return out


def _approved(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.id, c.moment_id, m.score, c.post_rank, k.id AS campaign_id FROM clips c "
        "JOIN moments m ON m.id=c.moment_id JOIN sources s ON s.id=m.source_id "
        "JOIN campaigns k ON k.id=s.campaign_id "
        "WHERE c.status='approved' AND c.compliance_ok=1 AND k.joined_at IS NOT NULL "
        "AND k.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM posts p JOIN clips c2 ON c2.id=p.clip_id "
        "                WHERE c2.moment_id=c.moment_id AND c2.status!='rejected') "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.type=? AND j.ref_id=c.id "
        "                AND j.status='failed' AND j.updated_at > ?) "
        "AND (SELECT COUNT(*) FROM jobs j WHERE j.type=? AND j.ref_id=c.id AND j.status='failed') < ? "
        "ORDER BY m.score DESC, c.id", (JOB, time.time() - RETRY_AFTER, JOB, MAX_FAILURES)).fetchall()


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


def choose(cands: list, prime: list, now: datetime, times: list):
    """Which approved clip goes out now. Yaish's own order wins; otherwise prime slots (the
    target audience's evening) get the best clip and the others the weakest, so the best are
    still there when the next prime slot comes round."""
    urgent = [c for c in cands if c.get("priority")]
    if urgent:                     # a priority campaign (GTA VI from launch) goes first, best first
        ranked = sorted((c for c in urgent if c["post_rank"] is not None), key=lambda c: c["post_rank"])
        return ranked[0] if ranked else max(urgent, key=lambda c: c["score"] or 0)
    ranked = sorted((c for c in cands if c["post_rank"] is not None), key=lambda c: c["post_rank"])
    if ranked:
        return ranked[0]
    return cands[0] if not prime or pacing.current_slot(now, times) in prime else cands[-1]


def plan(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None,
         limit: int = 50) -> list[dict]:
    """The approved clips in the order they will go out, each with its expected slot."""
    s = schedule(conn, settings, now)
    cands = list(candidates(conn))
    prime = pacing.parse_times(s["cfg"]["prime_times"])
    posted, t, out = list(s["posted"]), s["now"], []
    while cands and len(out) < limit:
        slot = pacing.next_slot(t, s["times"], s["cap"], posted, s["gap"])
        if slot is None:
            break
        pick = choose(cands, prime, slot, s["times"])
        out.append({"clip_id": int(pick["id"]), "score": pick["score"], "at": slot,
                    "manual": pick["post_rank"] is not None, "urgent": bool(pick.get("priority")),
                    "prime": pacing.current_slot(slot, s["times"]) in prime})
        cands.remove(pick)
        posted.append(slot)
        t = max(slot, t) + timedelta(minutes=1)
    return out


def reorder(conn: sqlite3.Connection, settings: Settings, clip_id: int, how: str,
            now: datetime | None = None) -> None:
    """Move one clip up, down or to the top. The whole list becomes Yaish's order."""
    order = [p["clip_id"] for p in plan(conn, settings, now, limit=500)]
    if clip_id not in order:
        raise ValueError(f"clip {clip_id} is not waiting to post")
    i = order.index(clip_id)
    order.pop(i)
    j = {"top": 0, "up": max(0, i - 1), "down": min(len(order), i + 1),
         "bottom": len(order)}.get(how)
    if j is None:
        raise ValueError(f"unknown move {how!r}")
    order.insert(j, clip_id)
    for rank, cid in enumerate(order, start=1):
        conn.execute("UPDATE clips SET post_rank=? WHERE id=?", (rank, cid))


def reset_order(conn: sqlite3.Connection) -> int:
    return conn.execute("UPDATE clips SET post_rank=NULL WHERE post_rank IS NOT NULL").rowcount


def tick(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> int | None:
    """Called every few seconds: in auto mode approve, then queue one upload if a
    slot is open. Returns the clip id queued, or None."""
    s = schedule(conn, settings, now)
    cfg = s["cfg"]
    from ..core import priority

    priority.boost(conn)
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
    # Prime slots (the target audience's evening) get the best clip; the others take the
    # weakest, so the best ones are still there when the next prime slot comes round.
    pick = choose(cands, pacing.parse_times(cfg["prime_times"]), s["now"], s["times"])
    clip_id = int(pick["id"])
    queue.enqueue(conn, JOB, clip_id, priority=50)
    return clip_id


def enqueue_now(conn: sqlite3.Connection, clip_id: int) -> None:
    """Skip the pacing slot for one clip (manual override). Still needs approval."""
    if not any(r["id"] == clip_id for r in candidates(conn)):
        raise ValueError(f"clip {clip_id} is not approved and eligible to post")
    queue.enqueue(conn, JOB, clip_id, priority=50)


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
    from ..ingest.briefs import load_rules

    rules = load_rules(conn, int(meta["campaign_id"])) if meta.get("campaign_id") else None
    video = youtube.upload(
        ctx.settings, Path(row["path"]), title=meta["title"], description=meta["description"],
        tags=meta.get("hashtags", []), paid=bool(rules and rules.paid_promotion),
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

