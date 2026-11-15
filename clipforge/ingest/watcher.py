"""The `inbox/own/` watched folder (PRD section 5, "Own-gameplay track").

Anything dropped in is ingested with campaign = own and no brief rules. The
scan is a job like any other, so it is idempotent: a file already registered is
skipped, and a file still being copied in is left for the next scan.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from ..core import media

log = logging.getLogger("clipforge.ingest.watcher")

# A recording copied in over Wi-Fi grows for a while. Registering it mid-copy
# would probe a truncated file, so wait until the size holds still.
SETTLE_SECONDS = 20.0


def candidates(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*") if media.is_media(p))


def is_settled(path: Path, *, now: float | None = None,
               settle_seconds: float = SETTLE_SECONDS) -> bool:
    """Has the file stopped changing?"""
    try:
        stat = path.stat()
    except OSError:
        return False
    now = time.time() if now is None else now
    return (now - stat.st_mtime) >= settle_seconds and stat.st_size > 0


def scan(conn: sqlite3.Connection, folder: Path, *, settle_seconds: float = SETTLE_SECONDS,
         now: float | None = None) -> list[int]:
    """Register every settled recording. Returns the new source ids."""
    from . import sources

    campaign_id = sources.own_campaign_id(conn)
    new: list[int] = []
    for path in candidates(folder):
        if not is_settled(path, now=now, settle_seconds=settle_seconds):
            log.info("%s is still being written; leaving it for the next scan", path.name)
            continue
        resolved = str(path.resolve())
        existing = conn.execute(
            "SELECT id FROM sources WHERE campaign_id=? AND path=?", (campaign_id, resolved)
        ).fetchone()
        if existing:
            continue
        source_id = sources.register(
            conn, campaign_id, path=path, title=path.stem, creator="Yaish",
        )
        new.append(source_id)
        log.info("own footage %s registered as source %s", path.name, source_id)
    return new
