"""Channel approval, the VOD scan, and the autopilot pass that chains the stages."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.__main__ import main
from clipforge.core import autopilot, db
from clipforge.core.config import get_settings
from clipforge.ingest import briefs, channel, sources
from clipforge.ingest.rules import BriefRules, channel_root

CHAN = "https://www.twitch.tv/skiimasktimmy"


def vods(n: int, start: int = 1):
    return lambda _channel: [
        channel.Vod(url=f"https://www.twitch.tv/videos/{1000 + i}", title=f"VOD {i}", duration=3600)
        for i in range(start, start + n)]


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOURCES_PER_DAY", "2")
    monkeypatch.setenv("CLIP_BUFFER", "3")
    get_settings.cache_clear()
    settings = get_settings()
    conn = db.open_db(settings.db_path)
    yield settings, conn
    conn.close()
    get_settings.cache_clear()


def make_campaign(conn, *, channels=(CHAN,), joined=True, status="active", platform="whop") -> int:
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
        "VALUES (?, ?, 'Skii', ?, ?)",
        (platform, f"x{conn.execute('SELECT COUNT(*) FROM campaigns').fetchone()[0]}", status,
         "2026-11-01 00:00:00" if joined else None))
    cid = int(cur.lastrowid)
    briefs.save_rules(conn, cid, BriefRules(approved_channels=[channel_root(c) for c in channels]))
    return cid


def test_channel_root_strips_tail():
    assert channel_root("https://www.twitch.tv/SkiiMaskTimmy/videos") == channel_root(CHAN + "/")


def test_channel_approval_is_the_only_way_in(env):
    _, conn = env
    cid = make_campaign(conn, channels=())
    rules = briefs.load_rules(conn, cid)
    with pytest.raises(sources.NotApproved):
        sources.register(conn, cid, url="https://www.twitch.tv/videos/1", rules=rules,
                         from_channel=CHAN)
    cid2 = make_campaign(conn)
    rules2 = briefs.load_rules(conn, cid2)
    with pytest.raises(sources.NotApproved):                  # another creator's channel
        sources.register(conn, cid2, url="https://www.twitch.tv/videos/1", rules=rules2,
                         from_channel="https://www.twitch.tv/someoneelse")
    assert sources.register(conn, cid2, url="https://www.twitch.tv/videos/1", rules=rules2,
                            from_channel=CHAN, media_kind="audio", length_approved=True)


def test_reparse_keeps_yaishs_channels(env, monkeypatch):
    _, conn = env
    cid = make_campaign(conn)
    before = briefs.load_rules(conn, cid)
    assert before.approved_channels
    new = BriefRules(approved_channels=["https://twitch.tv/model-invented"])
    # parse_brief overwrites whatever the model returned with the stored list
    kept = list(before.approved_channels)
    new.approved_channels = kept
    briefs.save_rules(conn, cid, new)
    assert briefs.load_rules(conn, cid).approved_channels == kept


def test_scan_respects_daily_cap_and_dedupes(env):
    settings, conn = env
    cid = make_campaign(conn)
    first = channel.scan(conn, settings, cid, lister=vods(5))
    assert len(first) == 2                                    # SOURCES_PER_DAY
    assert channel.scan(conn, settings, cid, lister=vods(5)) == []   # cap used up today
    conn.execute("UPDATE sources SET created_at=datetime('now','-2 days')")
    again = channel.scan(conn, settings, cid, lister=vods(5))
    assert len(again) == 2 and not set(again) & set(first)    # next two, none repeated


def test_scan_refuses_unjoined_campaign(env):
    settings, conn = env
    cid = make_campaign(conn, joined=False)
    assert channel.scan(conn, settings, cid, lister=vods(3)) == []


def test_scan_survives_a_listing_failure(env):
    settings, conn = env
    cid = make_campaign(conn)

    def boom(_c):
        raise RuntimeError("twitch down")

    assert channel.scan(conn, settings, cid, lister=boom) == []


def test_cli_add_and_remove_channel(env, capsys):
    settings, conn = env
    cur = conn.execute("INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
                       "VALUES ('whop','s','S','active',datetime('now'))")
    cid = int(cur.lastrowid)
    briefs.save_rules(conn, cid, BriefRules())
    assert main(["campaign-rules", str(cid), "--add-channel", CHAN + "/videos"]) == 0
    assert briefs.load_rules(conn, cid).approved_channels == [channel_root(CHAN)]
    assert main(["campaign-rules", str(cid), "--remove-channel", CHAN]) == 0
    assert briefs.load_rules(conn, cid).approved_channels == []


def add_source(conn, cid, status="tagged", n=1):
    cur = conn.execute("INSERT INTO sources (campaign_id, url, status, path) VALUES (?,?,?,?)",
                       (cid, f"https://twitch.tv/videos/{n}", status, f"/media/{n}.mp4"))
    return int(cur.lastrowid)


def add_moment(conn, sid, score=50, status="candidate"):
    cur = conn.execute('INSERT INTO moments (source_id, start, "end", score, hook, status) '
                       "VALUES (?,0,30,?, 'h', ?)", (sid, score, status))
    return int(cur.lastrowid)


def jobs(conn, kind):
    return [r["ref_id"] for r in conn.execute(
        "SELECT ref_id FROM jobs WHERE type=? ORDER BY id", (kind,))]


def test_pass_chains_ingest_mine_and_clip(env):
    settings, conn = env
    cid = make_campaign(conn)
    tagged = add_source(conn, cid, "tagged", n=50)
    out = autopilot.run(conn, settings, lister=vods(1), fetch=lambda u: "")
    assert out["new_sources"] == 1 and out["mining"] == 1
    assert jobs(conn, "mine_source") == [tagged]
    assert len(jobs(conn, "ingest_source")) == 1
    mined = add_source(conn, cid, "mined", n=51)
    ids = [add_moment(conn, mined, s) for s in (10, 90, 50, 70, 30)]
    conn.execute("DELETE FROM jobs")
    out = autopilot.run(conn, settings, lister=vods(0), fetch=lambda u: "")
    assert out["clipping"] == 3                               # CLIP_BUFFER
    assert jobs(conn, "make_clip") == [ids[1], ids[3], ids[2]]    # best first
    again = autopilot.run(conn, settings, lister=vods(0), fetch=lambda u: "")
    assert again["clipping"] == 0                             # buffer already full


def test_mining_is_capped_per_day(env):
    settings, conn = env
    cid = make_campaign(conn)
    for n in range(4):
        add_source(conn, cid, "tagged", n=n)
    out = autopilot.run(conn, settings, lister=vods(0), fetch=lambda u: "")
    assert out["mining"] == 2


def test_caption_less_downloaded_sources_get_mined(env):
    settings, conn = env
    cid = make_campaign(conn)
    sid = add_source(conn, cid, "downloaded", n=70)
    out = autopilot.run(conn, settings, lister=vods(0), fetch=lambda u: "")
    assert out["mining"] == 1 and jobs(conn, "mine_source") == [sid]


def test_budget_counts_sources_not_retries_and_failed_mines_are_not_requeued(env):
    settings, conn = env
    cid = make_campaign(conn)
    old = add_source(conn, cid, "mined", n=80)
    for _ in range(3):                                        # one source, three attempts
        conn.execute("INSERT INTO jobs (type, ref_id, status, created_at, updated_at) "
                     "VALUES ('mine_source', ?, 'done', strftime('%s','now'), strftime('%s','now'))", (old,))
    broken = add_source(conn, cid, "downloaded", n=81)
    conn.execute("INSERT INTO jobs (type, ref_id, status, created_at, updated_at) "
                 "VALUES ('mine_source', ?, 'failed', strftime('%s','now'), strftime('%s','now'))", (broken,))
    fresh = [add_source(conn, cid, "downloaded", n=82), add_source(conn, cid, "downloaded", n=83)]
    out = autopilot.run(conn, settings, lister=vods(0), fetch=lambda u: "")
    assert out["mining"] == 0                                 # old + broken already use the 2 slots
    conn.execute("DELETE FROM jobs WHERE ref_id=?", (old,))
    out = autopilot.run(conn, settings, lister=vods(0), fetch=lambda u: "")
    assert out["mining"] == 1 and jobs(conn, "mine_source")[-1] == fresh[0]


def test_off_switch_and_unjoined_campaigns(env):
    settings, conn = env
    cid = make_campaign(conn, joined=False)
    sid = add_source(conn, cid, "mined")
    add_moment(conn, sid)
    assert autopilot.run(conn, settings, lister=vods(2), fetch=lambda u: "")["clipping"] == 0
    cid2 = make_campaign(conn)
    sid2 = add_source(conn, cid2, "mined", n=9)
    add_moment(conn, sid2)
    db.kv_set(conn, "setting_autopilot", "off")
    assert not any(autopilot.run(conn, settings, lister=vods(2), fetch=lambda u: "").values())
    assert jobs(conn, "make_clip") == []


def test_ensure_scheduled_adds_one(env):
    _, conn = env
    assert autopilot.ensure_scheduled(conn) is True
    assert autopilot.ensure_scheduled(conn) is False
    row = conn.execute("SELECT payload_json FROM jobs WHERE type='autopilot'").fetchone()
    assert json.loads(row["payload_json"])["repeat_seconds"] == autopilot.EVERY


def test_runway_counts_slots(env):
    _, conn = env
    assert autopilot.runway(conn)["date"] is None
    db.kv_set(conn, "setting_warmup_date", "2999-01-01")
    r = autopilot.runway(conn)
    assert r["slots"] == r["days"] * r["cap"] and r["short"] == r["slots"]
    db.kv_set(conn, "setting_warmup_date", "junk")
    assert autopilot.runway(conn)["date"] is None


def test_scan_never_takes_a_vod_another_campaign_has(env):
    settings, conn = env
    a = make_campaign(conn)
    b = make_campaign(conn)
    first = channel.scan(conn, settings, a, lister=vods(2))
    assert len(first) == 2
    second = channel.scan(conn, settings, b, lister=vods(4))
    urls_a = {r["url"] for r in conn.execute("SELECT url FROM sources WHERE campaign_id=?", (a,))}
    urls_b = {r["url"] for r in conn.execute("SELECT url FROM sources WHERE campaign_id=?", (b,))}
    assert len(second) == 2 and not urls_a & urls_b


def _fetch_ctx(env, *, via_channel: int, approved_channels):
    from clipforge.core.worker import Ctx

    settings, conn = env
    cid = make_campaign(conn, channels=approved_channels)
    sid = add_source(conn, cid, "mined", n=90)
    conn.execute("UPDATE sources SET path=NULL, via_channel=? WHERE id=?", (via_channel, sid))
    mid = add_moment(conn, sid)
    return conn, mid, Ctx(conn, settings, 1, "make_clip", mid)


def test_clip_fetch_accepts_a_vod_that_came_through_an_approved_channel(env, monkeypatch):
    from clipforge.core import handlers
    from clipforge.edit import hls

    conn, mid, ctx = _fetch_ctx(env, via_channel=1, approved_channels=(CHAN,))
    got = {}

    def fake_section(url, start, end, dest, **kw):
        got["url"] = url
        return hls.Section(path=dest, offset=0.0)

    monkeypatch.setattr(hls, "fetch_section", fake_section)
    handlers.stage_clip_fetch(ctx)
    assert got["url"].endswith("/videos/90")
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0


@pytest.mark.parametrize("via_channel, channels", [(0, (CHAN,)), (1, ())])
def test_clip_fetch_still_refuses_what_nothing_approves(env, monkeypatch, via_channel, channels):
    from clipforge.core import handlers
    from clipforge.edit import hls

    conn, mid, ctx = _fetch_ctx(env, via_channel=via_channel, approved_channels=channels)
    monkeypatch.setattr(hls, "fetch_section", lambda *a, **k: pytest.fail("must not fetch"))
    handlers.stage_clip_fetch(ctx)
    row = conn.execute("SELECT status, compliance_notes FROM clips").fetchone()
    assert row["status"] == "held" and "does not approve" in row["compliance_notes"]


def test_registering_through_a_channel_marks_the_source(env):
    _, conn = env
    cid = make_campaign(conn)
    rules = briefs.load_rules(conn, cid)
    sid = sources.register(conn, cid, url="https://www.twitch.tv/videos/5", rules=rules,
                           media_kind="audio", from_channel=CHAN, length_approved=True)
    assert conn.execute("SELECT via_channel FROM sources WHERE id=?", (sid,)).fetchone()[0] == 1


def test_a_short_official_trailer_is_cut_by_its_chapters():
    import json
    from clipforge.core import handlers
    from clipforge.ingest.rules import BriefRules
    from clipforge.moments import score as score_mod

    chapters = [{"start": 5.39, "title": "Intro"}, {"start": 20.69, "title": "Power"},
                {"start": 33.1, "title": "Dark"}, {"start": 43.09, "title": "Combat"}]
    row = {"duration": 59.2, "chapters_json": json.dumps(chapters), "title": "Heismay Trailer"}
    cuts = handlers._short_official_cuts(row, BriefRules(clip_min_seconds=10.0), score_mod)
    spans = [(round(c.start, 1), round(c.end, 1)) for c in cuts]
    assert (5.4, 20.7) in spans and (0.0, 59.2) in spans
    assert all(c.length >= 10.0 for c in cuts)
    assert all(c.candidate.hook == "" for c in cuts)
    assert cuts[0].rubric_category == "official_trailer"
    assert (33.1, 43.1) in spans or all(s != (33.1, 43.1) for s in spans)


def test_a_long_source_gets_no_trailer_cuts():
    from clipforge.core import handlers
    from clipforge.moments import score as score_mod

    row = {"duration": 3600.0, "chapters_json": "[]", "title": "VOD"}
    assert handlers._short_official_cuts(row, None, score_mod) == []


def test_clip_fetch_uses_a_hand_trim_without_snapping(env, monkeypatch):
    from clipforge.core import handlers
    from clipforge.edit import hls

    conn, mid, ctx = _fetch_ctx(env, via_channel=1, approved_channels=(CHAN,))
    (ctx.settings.data_dir / "work" / f"clip-{mid}").mkdir(parents=True)
    (ctx.settings.data_dir / "work" / f"clip-{mid}" / "override.json").write_text(
        json.dumps({"start": 12.5, "end": 24.0}))
    got = {}

    def fake_section(url, start, end, dest, **kw):
        got["span"] = (start, end)
        return hls.Section(path=dest, offset=0.0)

    monkeypatch.setattr(hls, "fetch_section", fake_section)
    handlers.stage_clip_fetch(ctx)
    plan = json.loads((ctx.settings.data_dir / "work" / f"clip-{mid}" / "plan.json").read_text())
    assert (plan["start"], plan["end"]) == (12.5, 24.0) and got["span"] == (11.5, 25.0)
