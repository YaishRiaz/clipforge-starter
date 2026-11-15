"""The ingest gate (hard limits 1, 2 and 7) and the own-footage watcher."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from clipforge.core import db
from clipforge.ingest import briefs, sources, watcher
from clipforge.ingest.rules import BriefRules

APPROVED = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
NOT_APPROVED = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


@pytest.fixture()
def conn(tmp_path: Path):
    c = db.open_db(tmp_path / "data" / "clipforge.db")
    yield c
    c.close()


def make_campaign(conn, *, status="active", joined=True, approved=(APPROVED,)) -> int:
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
        "VALUES ('whop', ?, 'Test campaign', ?, ?)",
        (f"ext{time.time_ns()}", status, "2026-11-12 10:00:00" if joined else None),
    )
    campaign_id = int(cur.lastrowid)
    briefs.save_rules(conn, campaign_id, BriefRules(approved_source_urls=list(approved)))
    return campaign_id


def test_migration_two_applied(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    assert {"held_reason", "bytes"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2


def test_registers_approved_footage(conn):
    campaign_id = make_campaign(conn)
    source_id = sources.register(conn, campaign_id, url="https://youtu.be/dQw4w9WgXcQ?si=x")
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    # Stored normalised, so two spellings cannot become two rows.
    assert row["url"] == "https://youtube.com/watch?v=dQw4w9WgXcQ"
    assert row["status"] == "new"


def test_refuses_footage_the_brief_does_not_name(conn):
    campaign_id = make_campaign(conn)
    with pytest.raises(sources.NotApproved, match="hard limit 1"):
        sources.register(conn, campaign_id, url=NOT_APPROVED)
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_refuses_a_campaign_that_was_never_joined(conn):
    campaign_id = make_campaign(conn, status="found", joined=False)
    with pytest.raises(sources.NotApproved, match="not active"):
        sources.register(conn, campaign_id, url=APPROVED)


def test_refuses_an_active_campaign_with_no_joined_at(conn):
    """Design change 7: nothing is clipped for a campaign before joined_at."""
    campaign_id = make_campaign(conn, status="active", joined=False)
    with pytest.raises(sources.NotApproved, match="joined_at"):
        sources.register(conn, campaign_id, url=APPROVED)


def test_refuses_an_ended_campaign(conn):
    campaign_id = make_campaign(conn, status="ended")
    with pytest.raises(sources.NotApproved):
        sources.register(conn, campaign_id, url=APPROVED)


def test_refuses_when_the_brief_was_never_parsed(conn):
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
        "VALUES ('whop', 'unparsed', 'No brief yet', 'active', datetime('now'))"
    )
    with pytest.raises(sources.NotApproved, match="no parsed brief"):
        sources.register(conn, int(cur.lastrowid), url=APPROVED)


def test_same_footage_twice_reuses_the_row(conn):
    campaign_id = make_campaign(conn)
    first = sources.register(conn, campaign_id, url=APPROVED)
    second = sources.register(conn, campaign_id, url="https://youtu.be/dQw4w9WgXcQ")
    assert first == second


def test_footage_is_not_shared_between_campaigns(conn):
    """Hard limit 2: each campaign gets its own row, licensed to it alone."""
    a = make_campaign(conn)
    b = make_campaign(conn)
    first = sources.register(conn, a, url=APPROVED)
    second = sources.register(conn, b, url=APPROVED)
    assert first != second


def test_own_campaign_needs_no_brief_and_is_created_once(conn):
    first = sources.own_campaign_id(conn)
    assert first == sources.own_campaign_id(conn)
    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (first,)).fetchone()
    assert row["platform"] == "own" and row["status"] == "active" and row["joined_at"]


def test_own_footage_on_disk_skips_the_download_stage(conn, tmp_path: Path):
    clip = tmp_path / "gta6.mp4"
    clip.write_bytes(b"not really a video")
    campaign_id = sources.own_campaign_id(conn)
    source_id = sources.register(conn, campaign_id, path=clip, title="gta6")
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    assert row["status"] == "downloaded"
    assert row["path"] == str(clip.resolve())


def test_hold_records_the_reason(conn, tmp_path: Path):
    campaign_id = sources.own_campaign_id(conn)
    clip = tmp_path / "long.mp4"
    clip.write_bytes(b"x")
    source_id = sources.register(conn, campaign_id, path=clip)
    sources.hold(conn, source_id, "4.2 h is over the 3 h cap")
    row = conn.execute("SELECT status, held_reason FROM sources WHERE id=?",
                       (source_id,)).fetchone()
    assert row["status"] == "held" and "over the 3 h cap" in row["held_reason"]


def test_disk_floor_blocks_downloads(conn, tmp_path, monkeypatch):
    from clipforge.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DISK_FLOOR_GB", "999999")
    settings = get_settings()
    settings.ensure_dirs()
    with pytest.raises(sources.NoDiskSpace, match="floor"):
        sources.check_disk(settings)
    get_settings.cache_clear()


# --- the watched folder ----------------------------------------------------

def test_watcher_registers_settled_recordings(conn, tmp_path: Path):
    inbox = tmp_path / "own"
    inbox.mkdir()
    clip = inbox / "session1.mp4"
    clip.write_bytes(b"video")
    new = watcher.scan(conn, inbox, settle_seconds=0)
    assert len(new) == 1
    row = conn.execute("SELECT title, campaign_id FROM sources WHERE id=?", (new[0],)).fetchone()
    assert row["title"] == "session1"
    assert row["campaign_id"] == sources.own_campaign_id(conn)


def test_watcher_is_idempotent(conn, tmp_path: Path):
    inbox = tmp_path / "own"
    inbox.mkdir()
    (inbox / "a.mp4").write_bytes(b"video")
    assert len(watcher.scan(conn, inbox, settle_seconds=0)) == 1
    assert watcher.scan(conn, inbox, settle_seconds=0) == []
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_watcher_waits_for_a_file_still_being_copied(conn, tmp_path: Path):
    inbox = tmp_path / "own"
    inbox.mkdir()
    (inbox / "copying.mp4").write_bytes(b"partial")
    assert watcher.scan(conn, inbox, settle_seconds=600) == []
    # Once it has stopped changing, the next scan takes it.
    assert len(watcher.scan(conn, inbox, settle_seconds=0)) == 1


def test_watcher_ignores_non_media(conn, tmp_path: Path):
    inbox = tmp_path / "own"
    inbox.mkdir()
    (inbox / "notes.txt").write_text("not footage")
    (inbox / ".gitkeep").write_text("")
    assert watcher.scan(conn, inbox, settle_seconds=0) == []


def test_watcher_finds_recordings_in_subfolders(conn, tmp_path: Path):
    inbox = tmp_path / "own"
    (inbox / "2026-11-19").mkdir(parents=True)
    (inbox / "2026-11-19" / "heist.mkv").write_bytes(b"video")
    assert len(watcher.scan(conn, inbox, settle_seconds=0)) == 1


# --- holding and releasing a long source -----------------------------------
# PRD section 5: over MAX_SOURCE_HOURS a source is held "until Yaish approves
# it". SkiiMaskTimmy's VODs run 4-7 hours, so this is the normal path for
# campaign footage, not an edge case.

def test_migration_three_applied(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    assert "length_approved" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 3


def test_a_new_source_is_not_length_approved(conn, tmp_path):
    clip = tmp_path / "v.mp4"
    clip.write_bytes(b"v")
    source_id = sources.register(conn, sources.own_campaign_id(conn), path=clip)
    row = conn.execute("SELECT length_approved FROM sources WHERE id=?",
                       (source_id,)).fetchone()
    assert row["length_approved"] == 0


def test_release_lifts_the_hold_and_records_the_approval(conn, tmp_path):
    clip = tmp_path / "long.mp4"
    clip.write_bytes(b"v")
    source_id = sources.register(conn, sources.own_campaign_id(conn), path=clip)
    sources.hold(conn, source_id, "5.0 h is over the 3 h cap")
    row = sources.release(conn, source_id)
    assert row["status"] == "downloaded"       # the file is on disk
    assert row["held_reason"] is None
    assert row["length_approved"] == 1


def test_release_of_a_link_goes_back_to_new_for_download(conn):
    campaign_id = make_campaign(conn)
    source_id = sources.register(conn, campaign_id, url=APPROVED)
    sources.hold(conn, source_id, "6.1 h is over the 3 h cap")
    row = sources.release(conn, source_id)
    assert row["status"] == "new"               # nothing downloaded yet
    assert row["length_approved"] == 1


def test_release_of_an_unknown_source_is_an_error(conn):
    with pytest.raises(ValueError, match="no source"):
        sources.release(conn, 9999)


def test_a_released_source_is_not_held_again_by_the_probe(conn, tmp_path, monkeypatch):
    """The point of length_approved: re-running the job must not re-hold it."""
    from clipforge.core import handlers, media, worker
    from clipforge.core.config import get_settings

    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAX_SOURCE_HOURS", "3")
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()

    clip = tmp_path / "fivehours.mp4"
    clip.write_bytes(b"v")
    source_id = sources.register(conn, sources.own_campaign_id(conn), path=clip)

    # Pretend ffprobe saw a five-hour file.
    five_hours = media.Probe(duration=18000.0, width=1920, height=1080, fps=30.0,
                             has_audio=True, subtitle_streams=[], bytes=1,
                             language=None, title=None)
    monkeypatch.setattr(media, "probe", lambda _p: five_hours)

    ctx = worker.Ctx(conn, settings, job_id=1, job_type="ingest_source", ref_id=source_id)
    handlers.stage_probe(ctx)
    row = conn.execute("SELECT status, held_reason FROM sources WHERE id=?",
                       (source_id,)).fetchone()
    assert row["status"] == "held" and "over the 3 h cap" in row["held_reason"]

    sources.release(conn, source_id)
    handlers.stage_probe(ctx)
    row = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
    assert row["status"] != "held"
    get_settings.cache_clear()


# --- audio-first ingest -----------------------------------------------------
# Measured 12 Nov 2026: SkiiMaskTimmy's 5h45 VOD is 18.62 GB at 1080p60 and
# 519 MB as audio. `yt-dlp --download-sections` on Twitch HLS ran 10 minutes
# without writing a usable byte, so fetching only the chosen moments is not an
# option and audio-first is how we decide if a VOD is worth the video.

def test_migration_four_applied(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    assert "media_kind" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 4


def test_sources_default_to_video(conn):
    campaign_id = make_campaign(conn)
    source_id = sources.register(conn, campaign_id, url=APPROVED)
    row = conn.execute("SELECT media_kind FROM sources WHERE id=?", (source_id,)).fetchone()
    assert row["media_kind"] == "video"


def test_a_source_can_be_registered_as_audio(conn):
    campaign_id = make_campaign(conn)
    source_id = sources.register(conn, campaign_id, url=APPROVED, media_kind="audio")
    row = conn.execute("SELECT media_kind FROM sources WHERE id=?", (source_id,)).fetchone()
    assert row["media_kind"] == "audio"


def test_a_nonsense_media_kind_is_refused(conn):
    campaign_id = make_campaign(conn)
    with pytest.raises(ValueError, match="media_kind"):
        sources.register(conn, campaign_id, url=APPROVED, media_kind="interpretive dance")


def test_the_download_stage_asks_for_audio_when_the_source_says_audio(conn, tmp_path,
                                                                     monkeypatch):
    from clipforge.core import handlers, worker
    from clipforge.core.config import get_settings
    from clipforge.ingest import download

    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()

    campaign_id = make_campaign(conn)
    source_id = sources.register(conn, campaign_id, url=APPROVED, media_kind="audio")
    sources.release(conn, source_id)          # approve its length too

    seen: dict = {}

    def fake_fetch(url, dest_dir, settings_, *, timeout=0, allow_long=False,
                   audio_only=False, beat=None):
        seen.update(audio_only=audio_only, allow_long=allow_long)
        got = dest_dir / "a.m4a"
        got.parent.mkdir(parents=True, exist_ok=True)
        got.write_bytes(b"audio")
        return download.Downloaded(path=got, title="t", creator="c", duration=20730.0,
                                   published_at=None, language="en")

    monkeypatch.setattr(download, "fetch", fake_fetch)
    ctx = worker.Ctx(conn, settings, job_id=1, job_type="ingest_source", ref_id=source_id)
    handlers.stage_download(ctx)

    assert seen == {"audio_only": True, "allow_long": True}
    row = conn.execute("SELECT status, duration FROM sources WHERE id=?",
                       (source_id,)).fetchone()
    assert row["status"] == "downloaded" and row["duration"] == 20730.0
    get_settings.cache_clear()


def test_long_download_keeps_beating(monkeypatch):
    import sys
    from clipforge.ingest import download

    beats = []
    cmd = [sys.executable, "-c", "import time; time.sleep(1.3); print('ok')"]
    out = download._run(cmd, timeout=30, beat=lambda: beats.append(1), beat_every=0.4)
    assert out.strip() == "ok" and len(beats) >= 2


def test_beating_download_still_times_out_and_reports_errors():
    import subprocess
    import sys
    from clipforge.ingest import download

    with pytest.raises(subprocess.TimeoutExpired):
        download._run([sys.executable, "-c", "import time; time.sleep(30)"],
                      timeout=0.5, beat=lambda: None, beat_every=0.2)
    with pytest.raises(download.DownloadError):
        download._run([sys.executable, "-c", "import sys; sys.exit('boom')"],
                      timeout=10, beat=lambda: None)


def test_a_rate_limited_caption_fetch_does_not_sink_the_video(tmp_path, monkeypatch):
    from clipforge.core.config import get_settings
    from clipforge.ingest import download

    calls = []

    def fake_run(args, *, timeout, beat=None, beat_every=0):
        calls.append(list(args))
        if "--write-subs" in args:
            raise download.DownloadError("ERROR: Unable to download video subtitles for 'en': HTTP Error 429")
        (tmp_path / "abc.mp4").write_bytes(b"video")
        return ""

    monkeypatch.setattr(download, "metadata", lambda url, **k: {"id": "abc", "duration": 60})
    monkeypatch.setattr(download, "_run", fake_run)
    got = download.fetch("https://youtu.be/abc", tmp_path, get_settings())
    assert got.path.name == "abc.mp4"
    assert len(calls) == 2
    retry = calls[1]
    assert "--write-subs" not in retry and "--sub-langs" not in retry and "--convert-subs" not in retry


def test_other_download_errors_are_not_retried(tmp_path, monkeypatch):
    from clipforge.core.config import get_settings
    from clipforge.ingest import download

    calls = []

    def fake_run(args, *, timeout, beat=None, beat_every=0):
        calls.append(1)
        raise download.DownloadError("ERROR: Video unavailable")

    monkeypatch.setattr(download, "metadata", lambda url, **k: {"id": "abc", "duration": 60})
    monkeypatch.setattr(download, "_run", fake_run)
    with pytest.raises(download.DownloadError):
        download.fetch("https://youtu.be/abc", tmp_path, get_settings())
    assert len(calls) == 1
