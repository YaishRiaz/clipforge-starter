"""The mine_source job: transcript, summary, signals, moments.

Every stage is driven for real against a generated audio file; only the two
network calls (transcription and scoring) are faked. The properties that matter
are that each stage is idempotent, that a stage which cannot proceed holds the
source with a reason instead of failing three times, and that the moments reach
the database.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.core import db, handlers, media, worker
from clipforge.core.config import get_settings
from clipforge.ingest import briefs, sources
from clipforge.ingest import captions as caps
from clipforge.ingest.rules import BriefRules
from clipforge.moments import score as score_mod
from clipforge.moments import transcribe as tr
from clipforge.moments.score import Candidate, CandidateList
from clipforge.ingest.tagger import SourceTags


@pytest.fixture()
def home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GROQ_API_KEY", "unused-by-the-fakes")
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    yield settings
    get_settings.cache_clear()


@pytest.fixture()
def conn(home):
    c = db.open_db(home.db_path)
    yield c
    c.close()


def noisy_audio(path: Path, seconds: int = 30) -> Path:
    """Quiet, then a burst, then quiet: one obvious loudness peak."""
    media.run([
        media._tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"sine=frequency=300:sample_rate=16000:duration={seconds}",
        "-af", f"volume='if(between(t,{seconds//2},{seconds//2}+4),1.0,0.02)':eval=frame",
        "-c:a", "pcm_s16le", str(path),
    ], timeout=300)
    return path


def make_source(conn, home, *, seconds: int = 30, kind: str = "audio",
                with_transcript: bool = False) -> int:
    audio = noisy_audio(home.data_dir / "src.wav", seconds)
    campaign_id = sources.own_campaign_id(conn)
    source_id = sources.register(conn, campaign_id, path=audio, title="A stream",
                                 media_kind=kind)
    conn.execute("UPDATE sources SET duration=? WHERE id=?", (float(seconds), source_id))
    if with_transcript:
        segments = [caps.Segment(float(i), float(i + 2), f"word{i} and more words here")
                    for i in range(0, seconds, 2)]
        dest = home.data_dir / "transcripts" / f"{source_id}.json"
        caps.save(segments, dest, source="test")
        conn.execute("UPDATE sources SET transcript_path=? WHERE id=?",
                     (str(dest), source_id))
    return source_id


def ctx_for(conn, home, source_id: int) -> worker.Ctx:
    return worker.Ctx(conn, home, job_id=1, job_type="mine_source", ref_id=source_id)


def row(conn, source_id: int):
    return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


# --- transcribe -------------------------------------------------------------

def test_transcribe_writes_the_transcript(conn, home, monkeypatch):
    source_id = make_source(conn, home)
    monkeypatch.setattr(tr, "transcribe", lambda *a, **k: (
        [caps.Segment(1.0, 4.0, "hello there")], "fake-backend"))
    handlers.stage_transcribe(ctx_for(conn, home, source_id))
    saved = row(conn, source_id)["transcript_path"]
    assert saved and Path(saved).is_file()
    assert [s.text for s in caps.load(saved)] == ["hello there"]


def test_transcribe_is_skipped_when_captions_already_exist(conn, home, monkeypatch):
    source_id = make_source(conn, home, with_transcript=True)
    called = []
    monkeypatch.setattr(tr, "transcribe", lambda *a, **k: called.append(1) or ([], "x"))
    handlers.stage_transcribe(ctx_for(conn, home, source_id))
    assert called == []


def test_a_missing_key_holds_the_source(conn, home, monkeypatch):
    """Not a failure to retry three times: a human has to act."""
    source_id = make_source(conn, home)

    def unavailable(*a, **k):
        raise tr.TranscriptionUnavailable("GROQ_API_KEY is not set in .env")

    monkeypatch.setattr(tr, "transcribe", unavailable)
    handlers.stage_transcribe(ctx_for(conn, home, source_id))
    got = row(conn, source_id)
    assert got["status"] == "held" and "GROQ_API_KEY" in got["held_reason"]


def test_an_empty_transcript_holds_with_a_reason(conn, home, monkeypatch):
    source_id = make_source(conn, home)
    monkeypatch.setattr(tr, "transcribe", lambda *a, **k: ([], "fake"))
    handlers.stage_transcribe(ctx_for(conn, home, source_id))
    got = row(conn, source_id)
    assert got["status"] == "held" and "speech" in got["held_reason"]


def test_a_held_source_is_left_alone(conn, home, monkeypatch):
    source_id = make_source(conn, home)
    sources.hold(conn, source_id, "over the cap")
    called = []
    monkeypatch.setattr(tr, "transcribe", lambda *a, **k: called.append(1) or ([], "x"))
    handlers.stage_transcribe(ctx_for(conn, home, source_id))
    assert called == []


def test_a_missing_file_holds_rather_than_crashing(conn, home):
    source_id = make_source(conn, home)
    Path(row(conn, source_id)["path"]).unlink()
    handlers.stage_transcribe(ctx_for(conn, home, source_id))
    assert row(conn, source_id)["status"] == "held"


# --- summarise --------------------------------------------------------------

def test_summarise_records_the_summary_and_chapters(conn, home, monkeypatch):
    from clipforge.ingest import tagger

    source_id = make_source(conn, home, with_transcript=True)
    monkeypatch.setattr(tagger, "describe", lambda *a, **k: SourceTags(
        summary="A heist goes wrong.", chapters=[], language="en"))
    handlers.stage_summarise(ctx_for(conn, home, source_id))
    got = row(conn, source_id)
    assert got["summary"] == "A heist goes wrong."
    assert got["status"] == "tagged"


def test_summarise_does_not_run_twice(conn, home, monkeypatch):
    from clipforge.ingest import tagger

    source_id = make_source(conn, home, with_transcript=True)
    calls = []

    def once(*a, **k):
        calls.append(1)
        return SourceTags(summary="first", chapters=[])

    monkeypatch.setattr(tagger, "describe", once)
    handlers.stage_summarise(ctx_for(conn, home, source_id))
    handlers.stage_summarise(ctx_for(conn, home, source_id))
    assert len(calls) == 1


def test_summarise_without_a_transcript_does_nothing(conn, home):
    source_id = make_source(conn, home)
    handlers.stage_summarise(ctx_for(conn, home, source_id))
    assert row(conn, source_id)["summary"] is None


# --- signals ----------------------------------------------------------------

def test_signals_finds_the_burst_and_saves_peaks(conn, home):
    source_id = make_source(conn, home, seconds=40, with_transcript=True)
    handlers.stage_signals(ctx_for(conn, home, source_id))
    path = home.data_dir / "signals" / f"{source_id}.json"
    peaks = json.loads(path.read_text(encoding="utf-8"))
    assert peaks, "the loud burst should have been found"
    loud = [p for p in peaks if p["signal"] == "loudness_dbfs"]
    assert loud
    # The burst sits in the middle of the file.
    assert any(14 <= p["start"] <= 26 for p in loud)


def test_signals_is_idempotent(conn, home):
    source_id = make_source(conn, home, seconds=30)
    handlers.stage_signals(ctx_for(conn, home, source_id))
    path = home.data_dir / "signals" / f"{source_id}.json"
    first = path.read_text(encoding="utf-8")
    path.write_text(first.replace("[", "[", 1), encoding="utf-8")
    mtime = path.stat().st_mtime
    handlers.stage_signals(ctx_for(conn, home, source_id))
    assert path.stat().st_mtime == mtime      # not rewritten


def test_an_audio_source_is_not_asked_for_scene_changes(conn, home, monkeypatch):
    from clipforge.moments import signals as sig

    source_id = make_source(conn, home, seconds=20, kind="audio")
    called = []
    monkeypatch.setattr(sig, "scene_change_times",
                        lambda *a, **k: called.append(1) or [])
    handlers.stage_signals(ctx_for(conn, home, source_id))
    assert called == []


# --- score ------------------------------------------------------------------

def prepare_for_score(conn, home, *, seconds: int = 60) -> int:
    source_id = make_source(conn, home, seconds=seconds, with_transcript=True)
    handlers.stage_signals(ctx_for(conn, home, source_id))
    return source_id


def test_score_writes_moments(conn, home, monkeypatch):
    source_id = prepare_for_score(conn, home)
    peaks = json.loads(
        (home.data_dir / "signals" / f"{source_id}.json").read_text(encoding="utf-8"))
    anchor = peaks[0]["start"]

    monkeypatch.setattr(score_mod, "propose", lambda *a, **k: [
        score_mod.Scored(
            candidate=Candidate(start=anchor, end=anchor + 20, description="d",
                                hook="They did not expect that",
                                title="The play that ended the run",
                                categories=["surprise"], penalties=[], confidence=1.0),
            score=61.5, rubric_category="surprise"),
    ])
    handlers.stage_score(ctx_for(conn, home, source_id))
    got = conn.execute("SELECT * FROM moments WHERE source_id=?", (source_id,)).fetchall()
    assert len(got) == 1
    assert got[0]["score"] == 61.5
    assert got[0]["hook"] == "They did not expect that"
    assert got[0]["status"] == "candidate"
    assert row(conn, source_id)["status"] == "mined"


def test_score_does_not_duplicate_on_a_second_run(conn, home, monkeypatch):
    source_id = prepare_for_score(conn, home)
    monkeypatch.setattr(score_mod, "propose", lambda *a, **k: [
        score_mod.Scored(
            candidate=Candidate(start=5.0, end=25.0, description="d", hook="h",
                                title="t", categories=["surprise"], confidence=1.0),
            score=50.0, rubric_category="surprise"),
    ])
    handlers.stage_score(ctx_for(conn, home, source_id))
    handlers.stage_score(ctx_for(conn, home, source_id))
    assert conn.execute("SELECT COUNT(*) AS n FROM moments").fetchone()["n"] == 1


def test_score_passes_the_brief_rules_through(conn, home, monkeypatch):
    """The campaign's banned content has to reach the scorer."""
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at, category) "
        "VALUES ('whop','skii','SkiiMaskTimmy','active',datetime('now'),'gaming')"
    )
    campaign_id = int(cur.lastrowid)
    briefs.save_rules(conn, campaign_id, BriefRules(
        approved_source_urls=["https://www.twitch.tv/videos/1"],
        clip_min_seconds=7.0, banned_content=["other streamers"]))
    source_id = prepare_for_score(conn, home)
    conn.execute("UPDATE sources SET campaign_id=? WHERE id=?", (campaign_id, source_id))

    seen: dict = {}

    def capture(segments, peaks, rubric, *, rules=None, **kw):
        seen["rules"] = rules
        seen["rubric"] = rubric.name
        return []

    monkeypatch.setattr(score_mod, "propose", capture)
    handlers.stage_score(ctx_for(conn, home, source_id))
    assert seen["rules"].banned_content == ["other streamers"]
    assert seen["rubric"] == "general"      # gaming has no rubric file yet


def test_score_without_a_signals_file_does_nothing(conn, home):
    source_id = make_source(conn, home, with_transcript=True)
    handlers.stage_score(ctx_for(conn, home, source_id))
    assert conn.execute("SELECT COUNT(*) AS n FROM moments").fetchone()["n"] == 0


def test_score_holds_when_the_model_is_unavailable(conn, home, monkeypatch):
    from clipforge.core import llm

    source_id = prepare_for_score(conn, home)

    def unavailable(*a, **k):
        raise llm.LLMUnavailable("GROQ_API_KEY is not set in .env")

    monkeypatch.setattr(score_mod, "propose", unavailable)
    handlers.stage_score(ctx_for(conn, home, source_id))
    got = row(conn, source_id)
    assert got["status"] == "held" and "GROQ_API_KEY" in got["held_reason"]


# --- the whole job ----------------------------------------------------------

def test_the_job_runs_end_to_end(conn, home, monkeypatch):
    from clipforge.core import queue
    from clipforge.ingest import tagger

    source_id = make_source(conn, home, seconds=60)
    monkeypatch.setattr(tr, "transcribe", lambda *a, **k: (
        [caps.Segment(float(i), float(i + 3), "they really did that just now")
         for i in range(0, 60, 3)], "fake-backend"))
    monkeypatch.setattr(tagger, "describe", lambda *a, **k: SourceTags(
        summary="A stream happens.", chapters=[], language="en"))
    monkeypatch.setattr(score_mod, "propose", lambda *a, **k: [
        score_mod.Scored(
            candidate=Candidate(start=10.0, end=35.0, description="d", hook="h",
                                title="t", categories=["surprise"], confidence=1.0),
            score=42.0, rubric_category="surprise"),
    ])

    job_id = queue.enqueue(conn, "mine_source", ref_id=source_id)
    job = queue.claim_next(conn)
    assert job["id"] == job_id
    worker.run_job(conn, home, job)

    got = row(conn, source_id)
    assert got["status"] == "mined"
    assert got["summary"] == "A stream happens."
    assert got["transcript_path"]
    assert conn.execute("SELECT COUNT(*) AS n FROM moments").fetchone()["n"] == 1
    assert (home.data_dir / "signals" / f"{source_id}.json").is_file()


# --- keeping the top N ------------------------------------------------------
# PRD section 6 step 5: 3-8 moments per source. A five-hour stream can yield
# dozens of passable ones and posting is capped at 3-8 a day.

def many_moments(count: int):
    return [
        score_mod.Scored(
            candidate=Candidate(start=float(i * 30), end=float(i * 30 + 20),
                                description="d", hook=f"hook {i}", title=f"title {i}",
                                categories=["surprise"], confidence=1.0),
            score=float(100 - i), rubric_category="surprise")
        for i in range(count)
    ]


def test_only_the_best_are_kept_as_candidates(conn, home, monkeypatch):
    source_id = prepare_for_score(conn, home)
    monkeypatch.setattr(score_mod, "propose", lambda *a, **k: many_moments(20))
    handlers.stage_score(ctx_for(conn, home, source_id))
    kept = conn.execute(
        "SELECT COUNT(*) AS n FROM moments WHERE status='candidate'").fetchone()["n"]
    dropped = conn.execute(
        "SELECT COUNT(*) AS n FROM moments WHERE status='dropped'").fetchone()["n"]
    assert kept == home.moments_per_source == 8
    assert dropped == 12


def test_the_dropped_ones_are_recorded_not_deleted(conn, home, monkeypatch):
    """The Learn page needs to know what was passed over."""
    source_id = prepare_for_score(conn, home)
    monkeypatch.setattr(score_mod, "propose", lambda *a, **k: many_moments(12))
    handlers.stage_score(ctx_for(conn, home, source_id))
    assert conn.execute("SELECT COUNT(*) AS n FROM moments").fetchone()["n"] == 12


def test_the_kept_ones_are_the_highest_scoring(conn, home, monkeypatch):
    source_id = prepare_for_score(conn, home)
    monkeypatch.setattr(score_mod, "propose", lambda *a, **k: many_moments(20))
    handlers.stage_score(ctx_for(conn, home, source_id))
    kept = [r["score"] for r in conn.execute(
        "SELECT score FROM moments WHERE status='candidate' ORDER BY score DESC")]
    dropped_best = conn.execute(
        "SELECT MAX(score) AS s FROM moments WHERE status='dropped'").fetchone()["s"]
    assert min(kept) > dropped_best


def test_fewer_moments_than_the_cap_are_all_kept(conn, home, monkeypatch):
    source_id = prepare_for_score(conn, home)
    monkeypatch.setattr(score_mod, "propose", lambda *a, **k: many_moments(3))
    handlers.stage_score(ctx_for(conn, home, source_id))
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM moments WHERE status='candidate'").fetchone()["n"] == 3
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM moments WHERE status='dropped'").fetchone()["n"] == 0


def test_mine_resumes_a_source_held_by_the_provider(conn, home, capsys):
    from clipforge.__main__ import main

    sid = make_source(conn, home)
    sources.hold(conn, sid, "cannot summarise: the provider's daily token allowance is spent")
    assert main(["mine", str(sid)]) == 0
    row = conn.execute("SELECT status, held_reason FROM sources WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "downloaded" and row["held_reason"] is None
    assert "resuming" in capsys.readouterr().out


def test_mine_does_not_lift_a_hard_limit_hold(conn, home, capsys):
    """Only source-release may lift a gate; mining must not be a side door."""
    from clipforge.__main__ import main

    sid = make_source(conn, home)
    sources.hold(conn, sid, "6.0 h is over the 3 h cap; approve it to continue")
    assert main(["mine", str(sid)]) == 2
    status = conn.execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()["status"]
    assert status == "held"
