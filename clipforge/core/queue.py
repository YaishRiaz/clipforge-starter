"""Job queue stored in SQLite.

Rules (PRD section 9):
- one heavy job at a time: a single worker claims one job;
- resumable: `stage` holds the last COMPLETED stage, so a restarted worker
  continues from the next one;
- a crash is not the job's fault: recovering an interrupted job does not
  spend one of its attempts.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

ACTIVE = ("queued", "running")


def _now() -> float:
    return time.time()


def enqueue(
    conn: sqlite3.Connection,
    job_type: str,
    ref_id: int | None = None,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 0,
    delay_seconds: float = 0.0,
    dedupe: bool = True,
) -> int:
    """Add a job. With dedupe, an active job of the same type+ref is reused."""
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if dedupe and ref_id is not None:
            row = conn.execute(
                "SELECT id FROM jobs WHERE type=? AND ref_id=? AND status IN (?,?)",
                (job_type, ref_id, *ACTIVE),
            ).fetchone()
            if row:
                conn.execute("COMMIT")
                return int(row["id"])
        cur = conn.execute(
            "INSERT INTO jobs (type, ref_id, payload_json, priority, run_after, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_type, ref_id, json.dumps(payload or {}), priority, now + delay_seconds, now, now),
        )
        conn.execute("COMMIT")
        return int(cur.lastrowid)
    except BaseException:
        conn.execute("ROLLBACK")
        raise


REPEATING = ("autopilot", "scan_inbox")      # re-enqueue themselves forever; a drain ignores them


def claim_next(conn: sqlite3.Connection, skip_types: tuple[str, ...] = ()) -> sqlite3.Row | None:
    """Atomically take the best queued job and mark it running."""
    now = _now()
    skip = "".join(f" AND type != '{t}'" for t in skip_types)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' AND run_after<=?" + skip +
            " ORDER BY priority DESC, id LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, heartbeat_at=?, updated_at=? WHERE id=?",
            (now, now, row["id"]),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        conn.execute("COMMIT")
        return job
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def set_stage(conn: sqlite3.Connection, job_id: int, stage: str) -> None:
    now = _now()
    conn.execute(
        "UPDATE jobs SET stage=?, heartbeat_at=?, updated_at=? WHERE id=?",
        (stage, now, now, job_id),
    )


def heartbeat(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (_now(), job_id))


def complete(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', last_error=NULL, updated_at=? WHERE id=?",
        (_now(), job_id),
    )


def fail(
    conn: sqlite3.Connection,
    job_id: int,
    error: str,
    *,
    max_attempts: int,
    backoff_seconds: float,
) -> str:
    """Record an error. Requeue with backoff, or mark failed when out of attempts."""
    now = _now()
    row = conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    attempts = int(row["attempts"]) if row else max_attempts
    if attempts >= max_attempts:
        status, run_after = "failed", now
    else:
        status, run_after = "queued", now + backoff_seconds * (2 ** (attempts - 1))
    conn.execute(
        "UPDATE jobs SET status=?, last_error=?, run_after=?, updated_at=? WHERE id=?",
        (status, error[-4000:], run_after, now, job_id),
    )
    return status


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """At worker start: jobs left 'running' by a crash go back to the queue.

    The stage is kept, and the attempt the crash consumed is refunded.
    """
    now = _now()
    cur = conn.execute(
        "UPDATE jobs SET status='queued', run_after=0, attempts=MAX(attempts-1,0), updated_at=? "
        "WHERE status='running'",
        (now,),
    )
    return cur.rowcount


def retry(conn: sqlite3.Connection, job_id: int) -> None:
    """Manual retry of a failed job from the dashboard."""
    conn.execute(
        "UPDATE jobs SET status='queued', attempts=0, run_after=0, updated_at=? WHERE id=? AND status='failed'",
        (_now(), job_id),
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {s: 0 for s in ("queued", "running", "done", "failed")}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        out[row["status"]] = row["n"]
    return out


def payload_of(job: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(job["payload_json"] or "{}")
    except json.JSONDecodeError:
        return {}
