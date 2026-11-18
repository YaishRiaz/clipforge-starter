"""Clipping a stream while it is live: chat over IRC, bursts, and the hand-over to full mining."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge.core import autopilot, db, handlers, worker
from clipforge.core.config import get_settings
from clipforge.edit import hls
from clipforge.ingest import briefs, captions as caps, live, tagger
from clipforge.ingest.rules import BriefRules, channel_root
from clipforge.moments import irc
from clipforge.moments.chat import Message
from clipforge.moments.score import Candidate, Scored

CHAN = "https://www.twitch.tv/skiimasktimmy"
VOD = "https://www.twitch.tv/videos/777"
T0 = 1_800_000_000.0


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


def campaign(conn, *, status="active", joined=True, channels=(CHAN,)) -> int:
    n = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    cid = int(conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) VALUES ('whop',?,?,?,?)",
        (f"x{n}", f"Skii {n}", status, "2026-11-01 00:00:00" if joined else None)).lastrowid)
    briefs.save_rules(conn, cid, BriefRules(approved_channels=[channel_root(c) for c in channels]))
    return cid


def quiet(start, end):
    return [Message(float(t), f"u{t % 4}", "hello there") for t in range(int(start), int(end), 7)]


def burst(at, n=14):
    return [Message(at + i * 0.4, f"fan{i}", "LMAOOO") for i in range(n)]


# --- IRC ---------------------------------------------------------------------

def test_parse_keeps_chat_lines_only():
    assert irc.parse(":bob!bob@bob.tmi.twitch.tv PRIVMSG #skii :no way") == ("bob", "no way")
    assert irc.parse("@badges=;color= :amy!amy@amy.tmi.twitch.tv PRIVMSG #skii :a : b") == ("amy", "a : b")
    assert irc.parse(":x!x@x PRIVMSG #skii :\x01ACTION dances\x01") == ("x", "dances")
    assert irc.parse("PING :tmi.twitch.tv") is None
    assert irc.parse(":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!") is None


class FakeSocket:
    def __init__(self, chunks, stop):
        self.chunks, self.stop, self.sent = list(chunks), stop, []

    def settimeout(self, _):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _):
        if not self.chunks:
            self.stop.set()
            return b""
        return self.chunks.pop(0)

    def close(self):
        pass


def test_reader_joins_anonymously_answers_ping_and_never_speaks():
    stop = threading.Event()
    sock = FakeSocket([b":a!a@a PRIVMSG #skii :lol\r\nPING :tmi.twitch.tv\r\n:b!b@b PRIV",
                       b"MSG #skii :clip it\r\n"], stop)
    got = []
    irc.read("Skii", lambda t, who, text: got.append((who, text)), stop,
             connect=lambda: sock, clock=lambda: T0, pause=0)
    assert got == [("a", "lol"), ("b", "clip it")]             # a message split across packets survives
    hello = sock.sent[0].decode()
    assert hello.startswith("NICK justinfan") and "JOIN #skii" in hello and "PASS" not in hello
    assert sock.sent[1] == b"PONG :tmi.twitch.tv\r\n"
    assert not any(b"PRIVMSG" in s for s in sock.sent)


# --- bursts ------------------------------------------------------------------

def test_a_burst_three_times_the_last_five_minutes_is_a_spike():
    msgs = quiet(T0, T0 + 400) + burst(T0 + 391)
    p = irc.spike(msgs, T0 + 401, since=T0)
    assert p is not None and p.signal == "chat" and "laughing" in p.note and p.z > 2
    assert p.start < T0 + 390                                   # placed before the chat reacted
    assert irc.spike(quiet(T0, T0 + 400), T0 + 401, since=T0) is None


def test_no_spike_without_history_or_in_a_dead_chat():
    assert irc.spike(burst(T0 + 31), T0 + 41, since=T0) is None            # just joined: nothing to compare
    small = quiet(T0, T0 + 400) + [Message(T0 + 392, "a", "lol"), Message(T0 + 393, "b", "lol")]
    assert irc.spike(small, T0 + 401, since=T0) is None
    busy = [Message(T0 + t * 0.5, f"u{t % 40}", "LMAO") for t in range(820)]   # always loud is not a burst
    assert irc.spike(busy, T0 + 401, since=T0) is None


# --- live status -------------------------------------------------------------

def reply(stream, videos):
    return lambda body: SimpleNamespace(status_code=200, json=lambda: {"data": {"user": {
        "stream": stream, "videos": {"edges": [{"node": v} for v in videos]}}}})


def test_stream_info_finds_the_vod_being_written():
    assert live.stream_info("skii", post=reply(None, [])) is None
    s = live.stream_info("skii", post=reply(
        {"id": "9", "createdAt": "2026-11-16T02:00:00Z"},
        [{"id": "777", "createdAt": "2026-11-16T02:00:20Z", "status": "RECORDING"},
         {"id": "700", "createdAt": "2026-11-15T02:00:00Z", "status": "RECORDED"}]))
    assert s.vod_url == VOD and s.stream_id == "9" and s.started_at == 1794794400.0
    old_only = live.stream_info("skii", post=reply(
        {"id": "9", "createdAt": "2026-11-16T02:00:00Z"},
        [{"id": "700", "createdAt": "2026-11-15T02:00:00Z", "status": "RECORDED"}]))
    assert old_only is not None and old_only.vod_url is None    # live, but VODs are off


def test_only_joined_campaigns_channels_are_watched_and_the_first_campaign_wins(env):
    _, conn = env
    first = campaign(conn)
    campaign(conn)                                              # same creator, later campaign
    campaign(conn, joined=False, channels=("https://www.twitch.tv/other",))
    campaign(conn, status="skipped", channels=("https://www.twitch.tv/left",))
    assert live.channels(conn) == {"skiimasktimmy": (first, channel_root(CHAN))}


# --- the watcher -------------------------------------------------------------

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_watcher_queues_a_moment_at_the_vod_offset_and_respects_the_cap(env):
    settings, conn = env
    cid = campaign(conn)
    db.kv_set(conn, live.SETTING, "1")
    clock = Clock(T0)
    stream = live.Stream("skiimasktimmy", "9", T0 - 3600, VOD)       # an hour in when we notice
    started = []
    w = live.Watcher(settings, info=lambda login: stream, clock=clock,
                     reader=lambda login, add, stop: started.append(login))
    w.step(conn)
    lv = w.live["skiimasktimmy"]
    assert lv.campaign_id == cid and live.state(conn, now=T0)[0]["login"] == "skiimasktimmy"
    for m in quiet(T0, T0 + 400) + burst(T0 + 391):
        lv.add(m.t, m.who, m.text)
    clock.t = T0 + 401
    w.step(conn)
    job = conn.execute("SELECT * FROM jobs WHERE type='live_moment'").fetchone()
    p = json.loads(job["payload_json"])
    assert p["vod_url"] == VOD and p["campaign_id"] == cid and 3970 < p["offset"] < 3990
    assert job["run_after"] > job["created_at"]                 # waits for the VOD to hold the aftermath
    for m in burst(T0 + 791):
        lv.add(m.t, m.who, m.text)
    clock.t = T0 + 801
    w.step(conn)                                                # cap of one clip per stream
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='live_moment'").fetchone()[0] == 1
    assert started == ["skiimasktimmy"]


def test_watcher_does_nothing_when_live_clips_is_zero(env):
    settings, conn = env
    campaign(conn)
    db.kv_set(conn, live.SETTING, "0")
    asked = []
    w = live.Watcher(settings, info=lambda login: asked.append(login), clock=Clock(T0),
                     reader=lambda *a: None)
    w.step(conn)
    assert asked == [] and w.live == {}


# --- one burst becomes a moment ---------------------------------------------

def fakes(tmp_path, *, score=70.0, start=3950.0, end=3985.0):
    def fetch(url, s, e, dest, *, max_height, beat):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return hls.Section(path=dest, offset=s - 2.0)

    def transcribe(path, settings, *, work_dir, beat):
        return [caps.Segment(10.0, 14.0, "bro what was that"), caps.Segment(40.0, 44.0, "no way")], "fake"

    def propose(segments, peaks, rubric, **kw):
        cand = Candidate(start=start, end=end, description="He falls off the roof", hook="He fell", title="Fell")
        return [Scored(candidate=cand, score=score, rubric_category="funny")]

    return {"fetch": fetch, "transcribe": transcribe, "propose": propose}


def payload(cid, **over):
    return {"campaign_id": cid, "channel": channel_root(CHAN), "vod_url": VOD, "offset": 3980.0,
            "value": 30.0, "z": 4.0, "note": "14 chatters", **over}


def test_a_live_moment_is_transcribed_in_vod_time_scored_and_sent_for_clipping(env, tmp_path):
    settings, conn = env
    cid = campaign(conn)
    ids = live.make_moment(conn, settings, payload(cid), **fakes(tmp_path))
    src = conn.execute("SELECT * FROM sources").fetchone()
    assert src["live"] == 1 and src["status"] == "mined" and src["via_channel"] == 1 and src["campaign_id"] == cid
    segs = caps.load(src["transcript_path"])
    assert segs[0].start == pytest.approx(3980 - live.BEFORE - 2 + 10)       # shifted to VOD time
    m = conn.execute("SELECT * FROM moments").fetchone()
    assert [m["id"]] == ids and m["live"] == 1 and m["status"] == "candidate"
    assert m["score"] == 77.0                                                # the chat's reaction counts
    clip_job = conn.execute("SELECT * FROM jobs WHERE type='make_clip'").fetchone()
    assert clip_job["ref_id"] == m["id"]
    assert not (settings.data_dir / "work" / f"live-{src['id']}-3980").exists()
    # the same stretch a second time adds nothing
    assert live.make_moment(conn, settings, payload(cid), **fakes(tmp_path)) == []
    assert conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0] == 1


def test_live_moments_obey_the_gates(env, tmp_path):
    settings, conn = env
    left = campaign(conn, status="skipped")
    assert live.make_moment(conn, settings, payload(left), **fakes(tmp_path)) == []
    unapproved = campaign(conn, channels=("https://www.twitch.tv/other",))
    assert live.make_moment(conn, settings, payload(unapproved), **fakes(tmp_path)) == []
    owner = campaign(conn)
    assert live.make_moment(conn, settings, payload(owner), **fakes(tmp_path))
    rival = campaign(conn)                                      # footage never crosses campaigns
    assert live.make_moment(conn, settings, payload(rival, offset=5000.0), **fakes(tmp_path)) == []
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_a_vod_that_trails_the_broadcast_is_asked_again_then_dropped(env, tmp_path):
    settings, conn = env
    cid = campaign(conn)

    def behind(*a, **k):
        raise hls.SectionError("3920s is past the end of the video (3900s)")

    f = {**fakes(tmp_path), "fetch": behind}
    assert live.make_moment(conn, settings, payload(cid), **f) == []
    again = conn.execute("SELECT payload_json FROM jobs WHERE type='live_moment'").fetchall()
    assert len(again) == 1 and json.loads(again[0][0])["tries"] == 1
    live.make_moment(conn, settings, payload(cid, tries=2), **f)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='live_moment'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0] == 0


# --- the stream ends ---------------------------------------------------------

def test_when_the_stream_ends_the_vod_is_mined_whole_without_repeating_live_moments(env, tmp_path, monkeypatch):
    settings, conn = env
    cid = campaign(conn)
    live.make_moment(conn, settings, payload(cid), **fakes(tmp_path))
    sid = conn.execute("SELECT id FROM sources").fetchone()[0]
    stream = live.Stream("skiimasktimmy", "9", T0, VOD)
    log_path = settings.data_dir / "live" / "skiimasktimmy-9.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps([T0 + 50.0, "a", "lol"]) + "\n", encoding="utf-8")

    assert live.finish(conn, settings, stream, log_path) == sid
    src = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    assert src["status"] == "new" and src["live"] == 0 and src["transcript_path"] is None
    assert conn.execute("SELECT ref_id FROM jobs WHERE type='ingest_source'").fetchone()[0] == sid
    assert json.loads(live.chat_log(settings, VOD).read_text(encoding="utf-8")) == [[50.0, "a", "lol"]]
    # a burst that was still waiting its turn is dropped: the whole VOD covers it now
    assert live.make_moment(conn, settings, payload(cid, offset=6000.0), **fakes(tmp_path)) == []

    # downloaded: autopilot still sends it to mining although it has (live) moments
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    conn.execute("UPDATE sources SET status='downloaded', path=?, duration=7200 WHERE id=?", (str(audio), sid))
    autopilot.run(conn, settings, lister=lambda ch: [])
    assert conn.execute("SELECT ref_id FROM jobs WHERE type='mine_source'").fetchone()[0] == sid

    # scoring the whole VOD keeps the live moment and adds only what is new
    dest = tagger.transcript_path(settings, sid)
    caps.save([caps.Segment(3950.0, 3960.0, "bro")], dest, source="fake")
    conn.execute("UPDATE sources SET transcript_path=? WHERE id=?", (str(dest), sid))
    peaks = settings.data_dir / "signals" / f"{sid}.json"
    peaks.parent.mkdir(parents=True, exist_ok=True)
    peaks.write_text("[]", encoding="utf-8")
    from clipforge.moments import score as score_mod

    def both(*a, **k):
        same = Candidate(start=3952.0, end=3984.0, description="again", hook="h", title="t")
        new = Candidate(start=100.0, end=130.0, description="new", hook="h", title="t")
        return [Scored(same, 80.0, "funny"), Scored(new, 60.0, "funny")]

    monkeypatch.setattr(score_mod, "propose", both)
    handlers.stage_score(worker.Ctx(conn, settings, job_id=1, job_type="mine_source", ref_id=sid))
    rows = conn.execute('SELECT start, live FROM moments ORDER BY id').fetchall()
    assert [(r["start"], r["live"]) for r in rows] == [(3950.0, 1), (100.0, 0)]


def test_live_chat_stands_in_when_the_replay_cannot_be_read(env, monkeypatch):
    settings, conn = env
    cid = campaign(conn)
    sid = int(conn.execute(
        "INSERT INTO sources (campaign_id, url, status, duration) VALUES (?,?, 'downloaded', 600)",
        (cid, VOD)).lastrowid)
    from clipforge.moments import chat

    def down(*a, **k):
        raise RuntimeError("chat is not available for this video")

    monkeypatch.setattr(chat, "fetch", down)
    ctx = worker.Ctx(conn, settings, job_id=1, job_type="mine_source", ref_id=sid)
    row = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    assert handlers._chat_peaks(ctx, row) == []
    kept = live.chat_log(settings, VOD)
    kept.parent.mkdir(parents=True, exist_ok=True)
    msgs = [[float(t), f"u{t % 3}", "hello"] for t in range(0, 600, 9)] + \
           [[300 + i * 0.3, f"fan{i}", "LMAO"] for i in range(12)]
    kept.write_text(json.dumps(msgs), encoding="utf-8")
    found = handlers._chat_peaks(ctx, row)
    assert len(found) == 1 and found[0].signal == "chat"
