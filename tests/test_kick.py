"""Kick streams, launch-day footage limits, and GTA VI priority."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge.core import db, priority
from clipforge.core.config import get_settings
from clipforge.edit import hls
from clipforge.ingest import briefs, captions as caps, channel, kick, live
from clipforge.ingest.rules import BriefRules, _epoch, channel_root
from clipforge.moments.score import Candidate, Scored
from clipforge.moments.signals import Peak
from clipforge.publish import flow, youtube

CHAN = "https://kick.com/dasgasd3m"
UUID = "baa12604-007b-4bfa-b0d3-7a61078f4670"
VOD = f"https://kick.com/dasgasd3m/videos/{UUID}"
LAUNCH = "2026-11-19T05:00:00Z"
T_LAUNCH = _epoch(LAUNCH)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DISK_FLOOR_GB", "0")
    get_settings.cache_clear()
    settings = get_settings()
    conn = db.open_db(settings.db_path)
    yield settings, conn
    conn.close()
    get_settings.cache_clear()


def gta_campaign(conn, *, name="Dasgasd3m x RUCKUS | GTA 6", **rules) -> int:
    n = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    cid = int(conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
        "VALUES ('whop',?,?,'active','2026-11-18 00:00:00')", (f"k{n}", name)).lastrowid)
    base = dict(approved_channels=[channel_root(CHAN)], footage_after=LAUNCH,
                footage_games=["GTA 6"], post_from="2026-11-19T17:00:00Z", paid_promotion=True)
    briefs.save_rules(conn, cid, BriefRules(**{**base, **rules}))
    return cid


def resp(data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: data, text=data if isinstance(data, str) else "")


# --- Kick's endpoints ------------------------------------------------------------------------

def test_kick_links_are_recognised():
    assert kick.slug_of(VOD) == "dasgasd3m" and kick.slug_of("https://kick.com/Dasgasd3m/") == "dasgasd3m"
    assert kick.vod_uuid(VOD) == UUID and kick.vod_uuid(CHAN) is None
    assert channel_root("https://www.kick.com/Dasgasd3m/videos") == "https://kick.com/dasgasd3m"
    assert live.login_of(CHAN) == "kick:dasgasd3m"


def test_stream_info_and_vod_listing_read_kicks_api():
    pages = {
        "https://kick.com/api/v2/channels/dasgasd3m": {"livestream": {
            "id": 5, "is_live": True, "start_time": "2026-11-19 04:00:00", "session_title": "GTA 6 DAY",
            "categories": [{"name": "Grand Theft Auto VI"}]}},
        "https://kick.com/api/v2/channels/dasgasd3m/videos": [
            {"id": 5, "is_live": True, "duration": 0, "start_time": "2026-11-19 04:00:00",
             "session_title": "GTA 6 DAY", "categories": [{"name": "Grand Theft Auto VI"}],
             "video": {"uuid": UUID, "live_stream_id": 5}},
            {"id": 4, "is_live": False, "duration": 3_600_000, "start_time": "2026-11-17 02:00:00",
             "session_title": "gta 6 in 3 days", "categories": [{"name": "Just Chatting"}],
             "video": {"uuid": "11111111-2222-3333-4444-555555555555"}}],
    }
    get = lambda u: resp(pages[u])
    s = kick.stream_info("dasgasd3m", get=get)
    assert s.vod_url == VOD and s.games == ("Grand Theft Auto VI",) and s.started_at == _epoch("2026-11-19T04:00:00Z")
    vods = kick.list_vods(CHAN, get=get)
    assert vods[0].live and vods[1].duration == 3600.0 and vods[1].games == ("Just Chatting",)


def test_the_tallest_rendition_that_fits_is_picked():
    master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8,RESOLUTION=1920x1080\n1080p60/playlist.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=2,RESOLUTION=852x480\n480p30/playlist.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=284x160\n160p30/playlist.m3u8\n")
    base = "https://stream.kick.com/x/hls/"
    assert kick.pick_variant(master, base, 720) == base + "480p30/playlist.m3u8"
    assert kick.pick_variant(master, base, 100) == base + "160p30/playlist.m3u8"


# --- the brief's footage limits ------------------------------------------------------------

def test_footage_must_be_on_launch_day_and_show_the_game():
    r = BriefRules(footage_after=LAUNCH, footage_games=["GTA 6"], post_from="2026-11-19T17:00:00Z")
    assert not r.footage_ok(T_LAUNCH - 60, ("Grand Theft Auto VI",))[0]          # the countdown
    assert not r.footage_ok(T_LAUNCH + 60, ("Just Chatting",))[0]
    assert r.footage_ok(T_LAUNCH + 60, ("Grand Theft Auto VI",))[0]
    assert r.footage_ok(T_LAUNCH + 60, ("GTA VI",))[0]
    assert not r.may_post(_epoch("2026-11-19T16:59:00Z")) and r.may_post(_epoch("2026-11-19T17:00:00Z"))
    assert BriefRules().footage_ok(0, ("Anything",))[0] and BriefRules().may_post(0)


def test_channel_scan_takes_only_eligible_kick_vods_and_cuts_the_countdown_off(env):
    settings, conn = env
    cid = gta_campaign(conn)

    def lister(_):
        return [
            channel.Vod(url=VOD.replace(UUID, "a" * 8 + "-0000-0000-0000-000000000000"), title="live now",
                        duration=None, started_at=T_LAUNCH + 3600, games=("Grand Theft Auto VI",), live=True),
            channel.Vod(url=VOD, title="countdown into launch", duration=30 * 3600.0,
                        started_at=T_LAUNCH - 25 * 3600, games=("Just Chatting", "Grand Theft Auto VI")),
            channel.Vod(url=VOD.replace(UUID, "b" * 8 + "-0000-0000-0000-000000000000"), title="old",
                        duration=3600.0, started_at=T_LAUNCH - 3 * 86400, games=("Just Chatting",)),
        ]
    db.kv_set(conn, channel.GAME_START + VOD, str(25 * 3600 + 900))   # the game came on 15 min after launch
    new = channel.scan(conn, settings, cid, lister=lister)
    assert len(new) == 1
    rules = briefs.load_rules(conn, cid)
    assert rules.window_for(VOD) == (25 * 3600 + 900, None)
    assert not rules.inside_window(VOD, 100, 130)


# --- live Kick streams ---------------------------------------------------------------------

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_a_live_kick_stream_is_taken_in_stretches_only_once_the_game_is_on(env):
    settings, conn = env
    cid = gta_campaign(conn)
    stream = live.Stream("kick:dasgasd3m", "5", T_LAUNCH - 3600, VOD, games=("Just Chatting",))
    clock = Clock(T_LAUNCH + 600)
    w = live.Watcher(settings, info=lambda key: stream, reader=lambda *a: None, clock=clock)
    w.step(conn)
    clock.t += 3 * live.ROLL
    w.step(conn)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='live_moment'").fetchone()[0] == 0   # chatting
    stream = live.Stream("kick:dasgasd3m", "5", T_LAUNCH - 3600, VOD, games=("Grand Theft Auto VI",))
    w.info = lambda key: stream
    w.polled = 0
    w.step(conn)                                   # the game comes on: remember where
    on_at = clock.t - stream.started_at
    assert float(db.kv_get(conn, channel.GAME_START + VOD)) == pytest.approx(on_at)
    clock.t += live.ROLL + live.SETTLE + 1
    w.step(conn)
    job = conn.execute("SELECT * FROM jobs WHERE type='live_moment'").fetchone()
    p = json.loads(job["payload_json"])
    assert p["offset"] == pytest.approx(on_at, abs=0.1) and p["span"] == live.ROLL
    clock.t += 60
    w.step(conn)                                   # the next stretch is not whole yet
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='live_moment'").fetchone()[0] == 1


def test_a_live_stretch_becomes_a_moment_and_a_countdown_stretch_is_refused(env, tmp_path, monkeypatch):
    settings, conn = env
    cid = gta_campaign(conn)
    monkeypatch.setattr(live, "_stretch_peaks",
                        lambda got, heard: [Peak(start=got.offset + 50, end=got.offset + 60, value=3, z=3,
                                                 signal="loudness", note="")])

    def fetch(url, s, e, dest, *, max_height, beat):
        assert max_height == live.ROLL_HEIGHT and e - s == live.ROLL
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return hls.Section(path=dest, offset=s)

    def transcribe(path, settings, *, work_dir, beat):
        return [caps.Segment(40.0, 70.0, "the car just flew into the sky")], "fake"

    def propose(segments, peaks, rubric, **kw):
        s = peaks[0].start - 10
        return [Scored(candidate=Candidate(start=s, end=s + 30, description="Car flies", hook="It flew",
                                           title="GTA 6 car glitch"), score=70.0, rubric_category="funny")]

    start = T_LAUNCH - 3600
    ok = {"campaign_id": cid, "channel": channel_root(CHAN), "vod_url": VOD, "offset": 4000.0,
          "span": live.ROLL, "started_at": start}
    ids = live.make_moment(conn, settings, ok, fetch=fetch, transcribe=transcribe, propose=propose)
    assert ids and conn.execute("SELECT priority FROM jobs WHERE type='make_clip'").fetchone()[0] >= 10
    early = {**ok, "offset": 100.0}                                     # before launch
    assert live.make_moment(conn, settings, early, fetch=fetch, transcribe=transcribe, propose=propose) == []


# --- GTA VI priority -----------------------------------------------------------------------

def _approved_clip(conn, cid, score, n):
    sid = int(conn.execute("INSERT INTO sources (campaign_id, url, status) VALUES (?,?,'mined')",
                           (cid, f"https://kick.com/x/videos/{n:08d}-0000-0000-0000-000000000000")).lastrowid)
    mid = int(conn.execute('INSERT INTO moments (source_id, start, "end", score, status) VALUES (?,0,30,?,?)',
                           (sid, score, "candidate")).lastrowid)
    return int(conn.execute("INSERT INTO clips (moment_id, status, compliance_ok, path) VALUES (?,?,1,'x.mp4')",
                            (mid, "approved")).lastrowid)


def test_gta6_campaigns_get_priority_from_release(env):
    _, conn = env
    gta = gta_campaign(conn)
    other = gta_campaign(conn, name="SypherPK $7,500", footage_games=[], footage_after=None)
    briefs.save_rules(conn, other, BriefRules())
    assert not priority.is_priority(conn, gta, now=_epoch("2026-11-18T12:00:00Z"))
    assert priority.is_priority(conn, gta, now=_epoch("2026-11-19T01:00:00Z"))
    assert not priority.is_priority(conn, other, now=_epoch("2026-11-19T01:00:00Z"))
    db.kv_set(conn, priority.KV_FORCE.format(other), "1")
    assert priority.is_priority(conn, other, now=0)
    db.kv_set(conn, priority.KV_FORCE.format(gta), "0")
    assert not priority.is_priority(conn, gta, now=_epoch("2026-11-20T00:00:00Z"))


def test_priority_clips_post_first_but_never_before_the_campaign_opens(env, monkeypatch):
    _, conn = env
    db.kv_set(conn, priority.KV_FROM, "2026-01-01T00:00:00Z")
    gta = gta_campaign(conn)
    other = gta_campaign(conn, name="SypherPK $7,500")
    briefs.save_rules(conn, other, BriefRules())
    best_other = _approved_clip(conn, other, 90.0, 1)
    gta_clip = _approved_clip(conn, gta, 40.0, 2)
    before_open = _epoch("2026-11-19T10:00:00Z")
    assert [c["id"] for c in flow.candidates(conn, now=before_open)] == [best_other]
    after = flow.candidates(conn, now=_epoch("2026-11-19T18:00:00Z"))
    assert [c["id"] for c in after] == [gta_clip, best_other] and after[0]["priority"]
    assert flow.choose(after, [], None, [])["id"] == gta_clip


def test_waiting_jobs_of_a_priority_campaign_are_raised(env):
    _, conn = env
    db.kv_set(conn, priority.KV_FROM, "2026-01-01T00:00:00Z")
    gta = gta_campaign(conn)
    other = gta_campaign(conn, name="Other")
    briefs.save_rules(conn, other, BriefRules())
    from clipforge.core import queue

    a = queue.enqueue(conn, "live_moment", payload={"campaign_id": gta}, dedupe=False)
    b = queue.enqueue(conn, "live_moment", payload={"campaign_id": other}, dedupe=False)
    assert priority.boost(conn) == 1
    pr = dict(conn.execute("SELECT id, priority FROM jobs").fetchall())
    assert pr[a] == priority.JOB_PRIORITY and pr[b] == 0


def test_paid_promotion_is_declared_on_upload_when_the_brief_asks():
    assert youtube.snippet_body("t", "d", [], paid=True)["paidProductPlacementDetails"] == {
        "hasPaidProductPlacement": True}
    assert "paidProductPlacementDetails" not in youtube.snippet_body("t", "d", [])
    assert youtube.upload_parts(True).endswith("paidProductPlacementDetails")
