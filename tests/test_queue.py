"""Queue behaviour and crash recovery (Phase 0 acceptance)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clipforge.core import db, queue

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def conn(home: Path):
    c = db.open_db(home / "data" / "clipforge.db")
    yield c
    c.close()


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        CLIPFORGE_DATA_DIR=str(home / "data"),
        CLIPFORGE_DB=str(home / "data" / "clipforge.db"),
        WORKER_RETRY_BACKOFF_SECONDS="0",
        WORKER_POLL_SECONDS="0.2",
        PYTHONPATH=str(REPO),
    )
    return env


def _cli(home: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "clipforge", *args],
        env=_env(home), cwd=REPO, capture_output=True, text=True, timeout=120, **kw,
    )


def _runs(home: Path, job_id: int, stage: str) -> int:
    path = home / "data" / "selftest" / str(job_id) / f"{stage}.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _job(conn, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def test_schema_has_all_tables(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"campaigns", "sources", "moments", "clips", "posts", "stats", "payouts", "jobs", "kv"} <= names
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(conn):
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    assert db.migrate(conn) == before


def test_claim_order_and_dedupe(conn):
    low = queue.enqueue(conn, "selftest", ref_id=1)
    high = queue.enqueue(conn, "selftest", ref_id=2, priority=5)
    assert queue.enqueue(conn, "selftest", ref_id=1) == low          # deduped
    assert queue.claim_next(conn)["id"] == high                      # priority first
    assert queue.claim_next(conn)["id"] == low
    assert queue.claim_next(conn) is None                            # one at a time, none left


def test_delayed_job_is_not_claimed_early(conn):
    queue.enqueue(conn, "selftest", delay_seconds=3600)
    assert queue.claim_next(conn) is None


def test_fail_requeues_then_gives_up(conn):
    job_id = queue.enqueue(conn, "selftest")
    for attempt in (1, 2):
        queue.claim_next(conn)
        assert queue.fail(conn, job_id, "boom", max_attempts=3, backoff_seconds=0) == "queued"
        assert _job(conn, job_id)["attempts"] == attempt
    queue.claim_next(conn)
    assert queue.fail(conn, job_id, "boom", max_attempts=3, backoff_seconds=0) == "failed"
    assert "boom" in _job(conn, job_id)["last_error"]


def test_recover_refunds_the_attempt(conn):
    job_id = queue.enqueue(conn, "selftest")
    queue.claim_next(conn)
    queue.set_stage(conn, job_id, "a")
    assert queue.recover_interrupted(conn) == 1
    job = _job(conn, job_id)
    assert (job["status"], job["stage"], job["attempts"]) == ("queued", "a", 0)


def test_dummy_job_runs_end_to_end(home, conn):
    assert _cli(home, "selftest").returncode == 0
    out = _cli(home, "worker", "--drain")
    assert out.returncode == 0, out.stderr
    job = _job(conn, 1)
    assert (job["status"], job["stage"]) == ("done", "c")
    assert [_runs(home, 1, s) for s in "abc"] == [1, 1, 1]


def test_failing_job_ends_failed_without_blocking_others(home, conn):
    _cli(home, "selftest", "--fail-at", "b")
    _cli(home, "selftest")
    out = _cli(home, "worker", "--drain")
    assert out.returncode == 0, out.stderr
    assert _job(conn, 1)["status"] == "failed"
    assert _job(conn, 1)["stage"] == "a"            # completed stages are kept
    assert _runs(home, 1, "a") == 1                 # and never re-run on retry
    assert _job(conn, 2)["status"] == "done"


def test_process_crash_mid_stage_resumes(home, conn):
    """The worker process dies inside stage b; a restart finishes the job."""
    _cli(home, "selftest", "--crash-at", "b")
    first = _cli(home, "worker", "--drain")
    assert first.returncode == 9                    # hard exit
    job = _job(conn, 1)
    assert (job["status"], job["stage"]) == ("running", "a")

    second = _cli(home, "worker", "--drain")
    assert second.returncode == 0, second.stderr
    job = _job(conn, 1)
    assert (job["status"], job["stage"], job["attempts"]) == ("done", "c", 1)
    assert _runs(home, 1, "a") == 1                 # completed stage not repeated
    assert _runs(home, 1, "b") == 2                 # interrupted stage re-run from the top
    assert _runs(home, 1, "c") == 1


def test_forced_kill_and_restart(home, conn):
    """PRD Phase 0 acceptance: survive a forced restart (kill -9)."""
    _cli(home, "selftest", "--sleep", "3")
    proc = subprocess.Popen(
        [sys.executable, "-m", "clipforge", "worker"],
        env=_env(home), cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if _runs(home, 1, "b") >= 1:            # stage a finished, b under way
                break
            time.sleep(0.1)
        else:
            pytest.fail("worker never reached stage b")
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert _job(conn, 1)["status"] == "running"     # left dangling by the kill

    out = _cli(home, "worker", "--drain")
    assert out.returncode == 0, out.stderr
    job = _job(conn, 1)
    assert (job["status"], job["stage"]) == ("done", "c")
    assert _runs(home, 1, "a") == 1


# --- peak memory ------------------------------------------------------------
# PRD section 9 caps peak use at 4 GB, and Phase 2's acceptance is moments found
# "inside the memory limit", so the worker has to actually measure it.

def test_rss_is_readable():
    from clipforge.core.worker import rss_mb

    assert rss_mb() > 0


def test_a_job_records_its_peak_memory(conn, tmp_path, monkeypatch):
    from clipforge.core import handlers, queue, worker  # noqa: F401
    from clipforge.core.config import get_settings

    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    queue.enqueue(conn, "selftest", payload={"sleep": 0.4})
    job = queue.claim_next(conn)
    ctx = worker.run_job(conn, settings, job)
    assert ctx.peak_rss_mb > 0
    get_settings.cache_clear()


def test_going_over_the_limit_is_warned_not_fatal(conn, tmp_path, monkeypatch, caplog):
    """Losing the work would be worse than a job that used too much."""
    import logging

    from clipforge.core import handlers, queue, worker  # noqa: F401
    from clipforge.core.config import get_settings

    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEMORY_LIMIT_MB", "0.001")
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    queue.enqueue(conn, "selftest", payload={"sleep": 0.3})
    job = queue.claim_next(conn)
    with caplog.at_level(logging.WARNING, logger="clipforge.worker"):
        ctx = worker.run_job(conn, settings, job)
    assert ctx.peak_rss_mb > 0
    assert "over the" in caplog.text
    get_settings.cache_clear()
