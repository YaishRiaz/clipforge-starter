"""The single worker process.

A job type is a list of named stages. Each stage must be idempotent: if the
process dies in the middle of a stage, that stage runs again from the top
after restart. Completed stages are never re-run.
"""
from __future__ import annotations

import logging
import faulthandler
import os
import signal
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import queue
from .config import Settings

log = logging.getLogger("clipforge.worker")

StageFn = Callable[["Ctx"], None]
HANDLERS: dict[str, list[tuple[str, StageFn]]] = {}


def register(job_type: str, stages: list[tuple[str, StageFn]]) -> None:
    names = [n for n, _ in stages]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate stage names in {job_type}")
    HANDLERS[job_type] = stages


@dataclass
class Ctx:
    conn: sqlite3.Connection
    settings: Settings
    job_id: int
    job_type: str
    ref_id: int | None
    payload: dict[str, Any] = field(default_factory=dict)
    peak_rss_mb: float = 0.0

    def beat(self) -> None:
        """Call from inside long stages so the watchdog knows we are alive.

        Also samples memory: PRD section 9 caps peak use at 4 GB, and Phase 2's
        acceptance is moments found "inside the memory limit". A long stage that
        beats regularly is sampled regularly, which is exactly the stage that
        could grow.
        """
        queue.heartbeat(self.conn, self.job_id)
        _touch_heartbeat(self.settings)
        self.sample_memory()

    def sample_memory(self) -> float:
        rss = rss_mb()
        if rss > self.peak_rss_mb:
            self.peak_rss_mb = rss
        return rss


def rss_mb() -> float:
    """This process's resident memory in MB, or 0 when it cannot be read."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 2**20
    except Exception:
        return 0.0


STUCK_DUMP_AFTER = 600.0        # no heartbeat for this long: write every thread's stack to worker-stuck.txt
_stuck_file = None
_armed_at = 0.0


def _arm_stuck_dump(settings: Settings) -> None:
    """Re-arm a watchdog that dumps all stacks if the worker goes quiet, so a stall can be diagnosed."""
    global _stuck_file, _armed_at
    now = time.monotonic()
    if now - _armed_at < 30:
        return
    try:
        if _stuck_file is None:
            _stuck_file = open(settings.data_dir / "logs" / "worker-stuck.txt", "a", encoding="utf-8")
        faulthandler.dump_traceback_later(STUCK_DUMP_AFTER, repeat=False, file=_stuck_file)
        _armed_at = now
    except (OSError, ValueError, AttributeError):
        pass


def _touch_heartbeat(settings: Settings) -> None:
    _arm_stuck_dump(settings)
    try:
        path = settings.data_dir / "worker.heartbeat"
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the heartbeat file: %s", exc)


def heartbeat_age(settings: Settings) -> float | None:
    path: Path = settings.data_dir / "worker.heartbeat"
    try:
        return time.time() - float(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lower_priority() -> None:
    """Run below normal priority so the laptop stays usable."""
    try:
        if hasattr(os, "nice"):
            os.nice(10)
            return
    except OSError:
        return
    try:  # Windows, if psutil is installed
        import psutil  # type: ignore

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def run_job(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row) -> "Ctx":
    stages = HANDLERS.get(job["type"])
    if stages is None:
        raise RuntimeError(f"no handler registered for job type {job['type']!r}")
    names = [n for n, _ in stages]
    start = names.index(job["stage"]) + 1 if job["stage"] in names else 0
    ctx = Ctx(conn, settings, job["id"], job["type"], job["ref_id"], queue.payload_of(job))
    for name, fn in stages[start:]:
        log.info("job %s (%s): stage %s", job["id"], job["type"], name)
        ctx.beat()
        fn(ctx)
        ctx.sample_memory()
        queue.set_stage(conn, job["id"], name)
    if ctx.peak_rss_mb:
        limit = settings.memory_limit_mb
        if ctx.peak_rss_mb > limit:
            log.warning(
                "job %s peaked at %.0f MB, over the %.0f MB limit: one heavy job at a "
                "time is no longer enough for this work", job["id"], ctx.peak_rss_mb, limit,
            )
        else:
            log.info("job %s peak memory %.0f MB of %.0f MB allowed",
                     job["id"], ctx.peak_rss_mb, limit)
    return ctx


class Worker:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self._stop = False

    def stop(self, *_: Any) -> None:
        log.info("stop requested; finishing the current stage")
        self._stop = True

    def run(self, *, drain: bool = False) -> int:
        """Loop forever, or with drain=True exit once the queue is empty.

        Returns the number of jobs that finished.
        """
        from . import handlers  # noqa: F401  (registers job types)

        settings = self.settings
        settings.ensure_dirs()
        lower_priority()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass

        recovered = queue.recover_interrupted(self.conn)
        if recovered:
            log.info("recovered %d interrupted job(s)", recovered)

        finished = 0
        while not self._stop:
            _touch_heartbeat(settings)
            job = queue.claim_next(self.conn, queue.REPEATING if drain else ())
            if job is None:
                if drain and not self._has_pending(queue.REPEATING):
                    break
                time.sleep(0.2 if drain else settings.poll_seconds)
                continue
            try:
                run_job(self.conn, settings, job)
            except Exception:
                err = traceback.format_exc()
                status = queue.fail(
                    self.conn,
                    job["id"],
                    err,
                    max_attempts=settings.max_attempts,
                    backoff_seconds=settings.retry_backoff_seconds,
                )
                log.error("job %s %s:\n%s", job["id"], status, err)
            else:
                queue.complete(self.conn, job["id"])
                finished += 1
                log.info("job %s done", job["id"])
        return finished

    def _has_pending(self, skip_types: tuple[str, ...] = ()) -> bool:
        skip = "".join(f" AND type != '{t}'" for t in skip_types)
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE status IN ('queued','running')" + skip + " LIMIT 1"
        ).fetchone()
        return row is not None
