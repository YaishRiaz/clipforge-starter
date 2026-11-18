"""Phase 5: pacing, what may be posted, the YouTube upload, submission and stats."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from clipforge.app import telegram as tg
from clipforge.core import db, queue
from clipforge.core.config import get_settings, set_env_value
from clipforge.publish import flow, pacing, youtube
from clipforge.stats import collect

T = pacing.parse_times("10:00,14:00,18:00")


def at(h: int, m: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 11, day, h, m)


# ---- pacing -------------------------------------------------------------------

def test_parse_times_sorts_and_ignores_blanks():
    assert pacing.parse_times("18:00, 10:30,,10:30") == [time(10, 30), time(18, 0)]
    assert pacing.parse_times("") == []


def test_cap_ramps_one_per_week_and_respects_limits():
    first = at(9)
    assert pacing.effective_cap(8, 8, None, at(9)) == 7
    assert pacing.effective_cap(8, 8, first, first + timedelta(days=6)) == 7
    assert pacing.effective_cap(8, 8, first, first + timedelta(days=15)) == 8
    assert pacing.effective_cap(4, 8, first, first + timedelta(days=60)) == 4
    assert pacing.effective_cap(8, 6, first, first + timedelta(days=60)) == 6
    assert pacing.effective_cap(0, 8, None, at(9)) == 1


def test_not_due_before_first_slot_and_due_after():
    assert not pacing.is_due(at(9, 59), T, 3, [], 3)
    assert pacing.is_due(at(10, 0), T, 3, [], 3)


def test_a_slot_is_used_once():
    assert not pacing.is_due(at(10, 5), T, 3, [at(10, 1)], 3)
    assert pacing.is_due(at(14, 5), T, 3, [at(10, 1)], 3)


def test_min_gap_holds_back_a_due_slot():
    assert not pacing.is_due(at(14, 0), T, 3, [at(12, 30)], 3)
    assert pacing.is_due(at(15, 31), T, 3, [at(12, 30)], 3)


def test_cap_limits_slots_per_day():
    assert not pacing.is_due(at(19, 0), T, 1, [at(10, 0)], 3)


def test_yesterdays_posts_do_not_use_todays_slots():
    assert pacing.is_due(at(10, 0), T, 3, [at(18, 0, day=9)], 3)


def test_next_slot_today_tomorrow_and_none():
    assert pacing.next_slot(at(9), T, 3, [], 3) == at(10)
    assert pacing.next_slot(at(11), T, 3, [at(10, 5)], 3) == at(14)
    assert pacing.next_slot(at(19), T, 3, [at(10), at(14), at(18)], 3) == at(10, day=11)
    assert pacing.next_slot(at(9), [], 3, [], 3) is None


def test_next_slot_shows_a_missed_slot_as_soon_as_the_gap_allows():
    times = pacing.parse_times("07:00,09:00,11:00,17:00,19:00,21:30")
    posted = [at(7, 30), at(9, 52), at(11, 52), at(17, 19)]
    now = at(19, 14)
    assert pacing.next_slot(now, times, 7, posted, 2) == at(19, 19)
    assert not pacing.is_due(now, times, 7, posted, 2) and pacing.is_due(at(19, 19), times, 7, posted, 2)


# ---- fixtures ---------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPFORGE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "rtoken")
    monkeypatch.setenv("YOUTUBE_API_KEY", "apikey")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    settings = get_settings()
    conn = db.open_db(settings.db_path)
    conn.execute("INSERT INTO campaigns (id, platform, name, creator, rate_per_1k, status, joined_at, "
                 "brief_url) VALUES (1,'whop','Camp','c',1.0,'active','2026-01-01 00:00:00',"
                 "'https://example.com/camp')")
    conn.execute("INSERT INTO sources (id, campaign_id, creator, url) VALUES (1,1,'c','u')")
    return settings, conn


def add_clip(conn, tmp_path, clip_id, *, moment=None, score=50, status="approved", ok=1):
    moment = moment or clip_id
    if not conn.execute("SELECT 1 FROM moments WHERE id=?", (moment,)).fetchone():
        conn.execute("INSERT INTO moments (id, source_id, start, \"end\", score) VALUES (?,1,0,30,?)",
                     (moment, score))
    video = tmp_path / f"c{clip_id}.mp4"
    video.write_bytes(b"x" * 64)
    meta = {"title": f"Title {clip_id}", "description": "Desc #tag", "hashtags": ["#tag"]}
    conn.execute("INSERT INTO clips (id, moment_id, path, meta_json, compliance_ok, status) "
                 "VALUES (?,?,?,?,?,?)", (clip_id, moment, str(video), json.dumps(meta), ok, status))


def ctx(settings, conn, clip_id):
    return SimpleNamespace(conn=conn, settings=settings, ref_id=clip_id, beat=lambda: None)


# ---- what may be posted -----------------------------------------------------

def test_candidates_are_approved_compliant_joined_active_and_best_first(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1, score=40)
    add_clip(conn, tmp_path, 2, score=70)
    add_clip(conn, tmp_path, 3, status="ready")
    add_clip(conn, tmp_path, 4, ok=0)
    assert [r["id"] for r in flow.candidates(conn)] == [2, 1]
    conn.execute("UPDATE campaigns SET joined_at=NULL")
    assert flow.candidates(conn) == []
    conn.execute("UPDATE campaigns SET joined_at='2026-01-01 00:00:00', status='ended'")
    assert flow.candidates(conn) == []


def test_never_two_clips_of_the_same_moment(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1, moment=1)
    add_clip(conn, tmp_path, 2, moment=1)
    conn.execute("INSERT INTO posts (clip_id, video_id) VALUES (1,'v')")
    assert flow.candidates(conn) == []


def test_a_failed_publish_job_takes_the_clip_out_of_the_queue(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1)
    jid = queue.enqueue(conn, flow.JOB, 1)
    conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (jid,))
    assert flow.candidates(conn) == []


def test_tick_queues_one_upload_when_a_slot_is_open(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1)
    add_clip(conn, tmp_path, 2)
    assert flow.tick(conn, settings, at(9)) is None
    assert flow.tick(conn, settings, at(10, 1)) in (1, 2)
    assert flow.tick(conn, settings, at(10, 2)) is None          # one already queued


def test_tick_does_nothing_when_youtube_is_not_set_up(env, tmp_path, monkeypatch):
    settings, conn = env
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN")
    get_settings.cache_clear()
    add_clip(conn, tmp_path, 1)
    assert flow.tick(conn, get_settings(), at(10, 1)) is None


def test_auto_mode_approves_high_scores_only(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1, score=90, status="ready")
    add_clip(conn, tmp_path, 2, score=30, status="ready")
    db.kv_set(conn, "setting_review_mode", "auto")
    db.kv_set(conn, "setting_auto_threshold", "60")
    assert flow.tick(conn, settings, at(10, 1)) == 1
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM clips")}
    assert status == {1: "approved", 2: "ready"}


def test_review_mode_never_approves_by_itself(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1, score=99, status="ready")
    assert flow.tick(conn, settings, at(10, 1)) is None
    assert conn.execute("SELECT status FROM clips").fetchone()[0] == "ready"


def test_enqueue_now_refuses_an_unapproved_clip(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1, status="ready")
    with pytest.raises(ValueError):
        flow.enqueue_now(conn, 1)
    conn.execute("UPDATE clips SET status='approved'")
    flow.enqueue_now(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type=?", (flow.JOB,)).fetchone()[0] == 1


def test_the_job_refuses_a_clip_that_stopped_being_approved(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1, status="rejected")
    with pytest.raises(RuntimeError, match="no longer approved"):
        flow.stage_upload(ctx(settings, conn, 1))


# ---- upload -----------------------------------------------------------------

class Google:
    """A stand-in for the resumable upload endpoint."""

    def __init__(self, fail_first_chunk: bool = False):
        self.puts: list[str] = []
        self.starts = 0
        self.body: dict | None = None
        self.received = 0
        self.fail = fail_first_chunk

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.starts += 1
            self.body = json.loads(request.content)
            return httpx.Response(200, headers={"Location": "https://up.example/session"})
        rng = request.headers["Content-Range"]
        self.puts.append(rng)
        size = int(rng.rsplit("/", 1)[1])
        if rng.startswith("bytes */"):
            if self.received >= size:
                return httpx.Response(200, json={"id": "vid1"})
            return httpx.Response(308, headers={"Range": f"bytes=0-{self.received - 1}"}
                                  if self.received else {})
        if self.fail:
            self.fail = False
            return httpx.Response(503)
        end = int(rng.split(" ")[1].split("-")[1].split("/")[0])
        self.received = end + 1
        if self.received >= size:
            return httpx.Response(200, json={"id": "vid1"})
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})


@pytest.fixture
def google(monkeypatch):
    g = Google()
    real = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(g)
        return real(*a, **kw)

    monkeypatch.setattr(youtube.httpx, "Client", factory)
    monkeypatch.setattr(youtube, "access_token", lambda s: "access")
    monkeypatch.setattr(youtube, "CHUNK", 8)
    return g


def test_upload_sends_public_snippet_and_chunks(env, tmp_path, google):
    settings, _ = env
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 20)
    saved = []
    video = youtube.upload(settings, f, title="T", description="D", tags=["#a", "b"],
                           on_session=saved.append)
    assert video["id"] == "vid1"
    assert saved == ["https://up.example/session"]
    assert google.puts == ["bytes 0-7/20", "bytes 8-15/20", "bytes 16-19/20"]
    assert google.body["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    assert google.body["snippet"]["tags"] == ["a", "b"]


def test_upload_resumes_a_saved_session_without_restarting(env, tmp_path, google):
    settings, _ = env
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 20)
    google.received = 8
    video = youtube.upload(settings, f, title="T", description="D", tags=[],
                           session="https://up.example/session")
    assert video["id"] == "vid1"
    assert google.starts == 0
    assert google.puts[0] == "bytes */20"
    assert google.puts[1] == "bytes 8-15/20"


def test_upload_survives_a_server_error_mid_transfer(env, tmp_path, google):
    settings, _ = env
    google.fail = True
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 20)
    assert youtube.upload(settings, f, title="T", description="D", tags=[])["id"] == "vid1"
    assert google.starts == 1


def test_upload_error_carries_no_secrets(env, tmp_path, monkeypatch):
    settings, _ = env
    monkeypatch.setattr(youtube, "access_token", lambda s: "ACCESS-SECRET")
    real = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(
            403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}))
        return real(*a, **kw)

    monkeypatch.setattr(youtube.httpx, "Client", factory)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with pytest.raises(youtube.YouTubeError) as exc:
        youtube.upload(settings, f, title="T", description="D", tags=[])
    assert "quotaExceeded" in str(exc.value)
    assert "ACCESS-SECRET" not in str(exc.value)


def test_access_token_reports_invalid_grant_without_leaking(env, monkeypatch):
    settings, _ = env
    monkeypatch.setattr(youtube.httpx, "post", lambda *a, **k: httpx.Response(
        400, json={"error": "invalid_grant"}, headers={"content-type": "application/json"}))
    with pytest.raises(youtube.YouTubeError) as exc:
        youtube.access_token(settings)
    assert "youtube-auth" in str(exc.value)
    assert "rtoken" not in str(exc.value) and "csecret" not in str(exc.value)


def test_snippet_limits():
    body = youtube.snippet_body("t" * 200, "d" * 6000, [f"#t{i}" for i in range(30)])
    assert len(body["snippet"]["title"]) == 100
    assert len(body["snippet"]["description"]) <= 4900
    assert len(body["snippet"]["tags"]) == 15


def test_pkce_and_auth_url():
    verifier, challenge = youtube.pkce_pair()
    import base64
    import hashlib
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect
    url = youtube.build_auth_url("cid", "http://127.0.0.1:5", challenge, "st")
    assert "code_challenge_method=S256" in url and "youtube.upload" in url
    assert "access_type=offline" in url and "client_secret" not in url


# ---- the job stages ---------------------------------------------------------

def test_upload_stage_stores_the_video_id_and_is_idempotent(env, tmp_path, google):
    settings, conn = env
    add_clip(conn, tmp_path, 1)
    flow.stage_upload(ctx(settings, conn, 1))
    assert db.kv_get(conn, "yt_video_1") == "vid1"
    starts = google.starts
    flow.stage_upload(ctx(settings, conn, 1))
    assert google.starts == starts


def test_record_stage_creates_one_post_and_marks_the_clip(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1)
    db.kv_set(conn, "yt_video_1", "abc123")
    flow.stage_record(ctx(settings, conn, 1))
    flow.stage_record(ctx(settings, conn, 1))
    posts = conn.execute("SELECT * FROM posts").fetchall()
    assert len(posts) == 1
    assert posts[0]["url"] == "https://www.youtube.com/shorts/abc123"
    assert posts[0]["submit_deadline"] > posts[0]["posted_at"]
    assert conn.execute("SELECT status FROM clips WHERE id=1").fetchone()[0] == "posted"
    assert conn.execute("SELECT last_clip_posted_at FROM sources").fetchone()[0]


def test_record_stage_needs_a_video_id(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1)
    with pytest.raises(RuntimeError):
        flow.stage_record(ctx(settings, conn, 1))


def item(channel="UC1", privacy="public"):
    return {"id": "abc", "snippet": {"channelId": channel}, "status": {"privacyStatus": privacy},
            "statistics": {"viewCount": "12", "likeCount": "3", "commentCount": "1"}}


def test_verify_stage_checks_channel_and_visibility(env, tmp_path, monkeypatch):
    settings, conn = env
    monkeypatch.setenv("YOUTUBE_CHANNEL_ID", "UC1")
    get_settings.cache_clear()
    settings = get_settings()
    add_clip(conn, tmp_path, 1)
    db.kv_set(conn, "yt_video_1", "abc")
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {"abc": item()})
    flow.stage_verify(ctx(settings, conn, 1))
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {"abc": item(channel="OTHER")})
    with pytest.raises(RuntimeError, match="channel"):
        flow.stage_verify(ctx(settings, conn, 1))
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {"abc": item(privacy="private")})
    with pytest.raises(RuntimeError, match="not public"):
        flow.stage_verify(ctx(settings, conn, 1))
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {})
    flow.stage_verify(ctx(settings, conn, 1))                    # not listed yet: tolerated


# ---- submission and the reminders -------------------------------------------

def make_post(conn, tmp_path, minutes_ago=0):
    add_clip(conn, tmp_path, 1, status="posted")
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(flow.FMT)
    conn.execute("INSERT INTO posts (id, clip_id, video_id, url, posted_at) VALUES "
                 "(1,1,'abc','https://www.youtube.com/shorts/abc',?)", (when,))


def test_mark_submitted_is_idempotent_and_moves_the_clip(env, tmp_path):
    settings, conn = env
    make_post(conn, tmp_path)
    first = flow.mark_submitted(conn, 1)["submitted_at"]
    assert first
    assert flow.mark_submitted(conn, 1)["submitted_at"] == first
    assert conn.execute("SELECT status FROM clips WHERE id=1").fetchone()[0] == "submitted"
    with pytest.raises(ValueError):
        flow.mark_submitted(conn, 99)


def test_submit_page_comes_from_the_campaign(env, tmp_path):
    settings, conn = env
    make_post(conn, tmp_path)
    assert flow.submit_page(conn, 1) == "https://example.com/camp"


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(tg, "send", lambda s, text, buttons=None: out.append((text, buttons)))
    return out


def test_new_post_message_has_url_buttons_and_the_submitted_button(env, tmp_path, sent):
    settings, conn = env
    make_post(conn, tmp_path)
    assert tg._sweep_posts(conn, settings) == 1
    text, buttons = sent[0]
    assert "shorts/abc" in text and "25 minutes" in text
    flat = [b for row in buttons for b in row]
    assert ("Open video", "https://www.youtube.com/shorts/abc") in flat
    assert ("Open submit page", "https://example.com/camp") in flat
    assert ("I submitted it", "s:1") in flat
    assert tg._sweep_posts(conn, settings) == 0                 # once only


def test_reminders_fire_once_each_and_stop_after_submission(env, tmp_path, sent):
    settings, conn = env
    make_post(conn, tmp_path, minutes_ago=16)
    assert tg._sweep_posts(conn, settings) == 2                 # announcement + 15 min reminder
    assert tg._sweep_posts(conn, settings) == 0
    conn.execute("UPDATE posts SET posted_at=?", (
        (datetime.now(timezone.utc) - timedelta(minutes=26)).strftime(flow.FMT),))
    assert tg._sweep_posts(conn, settings) == 1                 # window passed
    assert tg._sweep_posts(conn, settings) == 0


def test_no_reminders_once_submitted(env, tmp_path, sent):
    settings, conn = env
    make_post(conn, tmp_path, minutes_ago=30)
    flow.mark_submitted(conn, 1)
    assert tg._sweep_posts(conn, settings) == 1                 # only the announcement


def test_submitted_button_marks_the_post(env, tmp_path, monkeypatch, sent):
    settings, conn = env
    make_post(conn, tmp_path)
    calls = []
    monkeypatch.setattr(tg, "call", lambda s, method, **kw: calls.append(method) or {})
    tg.handle(conn, settings, {"callback_query": {
        "id": "1", "data": "s:1", "message": {"chat": {"id": 42}, "message_id": 7}}})
    assert conn.execute("SELECT submitted_at FROM posts").fetchone()[0]
    assert "answerCallbackQuery" in calls


def test_approve_answer_names_the_next_slot(env, tmp_path, monkeypatch, sent):
    settings, conn = env
    add_clip(conn, tmp_path, 1, status="ready")
    monkeypatch.setattr(tg, "call", lambda s, method, **kw: {})
    tg.handle(conn, settings, {"callback_query": {
        "id": "1", "data": "a:1", "message": {"chat": {"id": 42}, "message_id": 7}}})
    assert "approved; posts" in sent[-1][0]


def test_failed_publish_job_sends_the_manual_fallback(env, tmp_path, monkeypatch):
    settings, conn = env
    add_clip(conn, tmp_path, 1)
    jid = queue.enqueue(conn, flow.JOB, 1)
    conn.execute("UPDATE jobs SET status='failed', last_error='boom' WHERE id=?", (jid,))
    texts, videos = [], []
    monkeypatch.setattr(tg, "send", lambda s, text, buttons=None: texts.append(text))
    monkeypatch.setattr(tg, "call", lambda s, method, **kw: videos.append(method) or {})
    tg.sweep(conn, settings)
    assert any("boom" in t for t in texts)
    assert any(t.startswith("Title:") and "Title 1" in t for t in texts)
    assert "sendVideo" in videos


# ---- stats ------------------------------------------------------------------

def test_collect_writes_a_row_per_video(env, tmp_path, monkeypatch):
    settings, conn = env
    make_post(conn, tmp_path)
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {"abc": item()})
    assert collect.collect(conn, settings) == (1, [])
    row = conn.execute("SELECT views, likes, comments FROM stats").fetchone()
    assert tuple(row) == (12, 3, 1)
    assert not collect.due(conn)
    assert collect.due(conn, now=9e12)


def test_collect_alerts_once_when_a_video_is_gone_or_not_public(env, tmp_path, monkeypatch):
    settings, conn = env
    make_post(conn, tmp_path, minutes_ago=120)
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {})
    written, alerts = collect.collect(conn, settings)
    assert written == 0 and len(alerts) == 1 and "no longer be found" in alerts[0]
    assert collect.collect(conn, settings)[1] == []
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {"abc": item(privacy="private")})
    _, alerts = collect.collect(conn, settings)
    assert len(alerts) == 1 and "private" in alerts[0]
    assert collect.collect(conn, settings)[1] == []


def test_a_new_video_missing_from_the_api_is_not_an_alarm_yet(env, tmp_path, monkeypatch):
    settings, conn = env
    make_post(conn, tmp_path, minutes_ago=5)
    monkeypatch.setattr(youtube, "lookup", lambda s, ids: {})
    assert collect.collect(conn, settings) == (0, [])


def test_lookup_sends_the_key_in_a_header_not_the_url(env, monkeypatch):
    settings, _ = env
    seen = {}

    def get(url, **kw):
        seen.update(url=url, headers=kw["headers"], params=kw["params"])
        return httpx.Response(200, json={"items": [item()]})

    monkeypatch.setattr(youtube.httpx, "get", get)
    assert "abc" in youtube.lookup(settings, ["abc"])
    assert seen["headers"]["X-Goog-Api-Key"] == "apikey"
    assert "apikey" not in str(seen["params"]) and "apikey" not in seen["url"]


# ---- config -----------------------------------------------------------------

def test_set_env_value_replaces_and_appends(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nYOUTUBE_REFRESH_TOKEN=old\nB=2\n", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "x")
    monkeypatch.setenv("C", "x")
    set_env_value(env_file, "YOUTUBE_REFRESH_TOKEN", "new")
    set_env_value(env_file, "C", "3")
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        "A=1", "B=2", "YOUTUBE_REFRESH_TOKEN=new", "C=3"]


def test_phase_5_needs_the_oauth_settings():
    missing = get_settings().missing_for(5)
    for name in ("YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        assert name in missing


def test_a_failed_upload_is_tried_again_after_an_hour_but_not_a_third_time(env, tmp_path):
    settings, conn = env
    add_clip(conn, tmp_path, 1)
    jid = queue.enqueue(conn, flow.JOB, 1)
    conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (jid,))
    conn.execute("UPDATE jobs SET updated_at = updated_at - ?", (flow.RETRY_AFTER + 60,))
    assert [r["id"] for r in flow.candidates(conn)] == [1]
    second = queue.enqueue(conn, flow.JOB, 1)
    conn.execute("UPDATE jobs SET status='failed', updated_at = updated_at - ?", (flow.RETRY_AFTER + 60,))
    assert flow.candidates(conn) == []


# ---- viewer geography ---------------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


def test_views_by_country_asks_for_one_video_and_reads_the_rows():
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return _Resp(200, {"rows": [["US", 60], ["lk", 40]]})

    got = youtube.views_by_country("tok", "abc", "2026-11-13", "2026-11-16", get=get)
    assert got == {"US": 60, "LK": 40}
    assert seen["url"] == youtube.REPORTS_URL and seen["params"]["filters"] == "video==abc"
    assert seen["params"]["dimensions"] == "country" and seen["params"]["ids"] == "channel==MINE"
    youtube.views_by_country("tok", None, "a", "b", get=get)
    assert "filters" not in seen["params"]
    assert youtube.views_by_country("t", "abc", "a", "b", get=lambda u, **k: _Resp(200, {})) == {}


def test_a_sign_in_without_the_analytics_scope_is_reported_not_raised(env, tmp_path):
    settings, conn = env
    make_post(conn, tmp_path)

    def get(url, **kw):
        return _Resp(403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}})

    fetch = lambda tok, vid, a, b: youtube.views_by_country(tok, vid, a, b, get=get)
    assert collect.collect_geo(conn, settings, fetch=fetch, token="t") == 0
    assert db.kv_get(conn, "geo_status") == "needs_consent"
    assert not collect.geo_due(conn) and collect.geo_due(conn, now=9e12)
    assert "yt-analytics.readonly" in youtube.SCOPE and "youtube.upload" in youtube.SCOPE


def test_geography_is_stored_and_summarised_with_the_tier1_share(env, tmp_path):
    from clipforge.app import stats_view

    settings, conn = env
    make_post(conn, tmp_path)
    data = {"US": 30, "LK": 60, "GB": 10}
    assert collect.collect_geo(conn, settings, fetch=lambda *a: data, token="t") == 1
    assert json.loads(db.kv_get(conn, "geo_channel")) == data
    assert collect.collect_geo(conn, settings, fetch=lambda *a: {}, token="t") == 0      # lagging: keep old
    g = stats_view.geo(conn)
    assert g["status"] == "ok" and g["all"]["total"] == 100
    assert abs(g["all"]["tier1"] - 0.4) < 1e-9 and g["all"]["top"][0]["country"] == "LK"
    assert list(g["posts"].values())[0]["tier1"] == g["campaigns"][1]["tier1"]
    assert stats_view.geo_summary([]) is None


def test_prime_slots_post_the_best_clip_and_other_slots_the_weakest(env, tmp_path):
    settings, conn = env
    for cid, score in ((1, 40), (2, 90), (3, 60)):
        add_clip(conn, tmp_path, cid, score=score)
    db.kv_set(conn, "setting_post_times", "07:00,13:00")
    db.kv_set(conn, "setting_prime_times", "07:00")
    assert flow.tick(conn, settings, at(7, 1)) == 2                # US evening: the best
    conn.execute("DELETE FROM jobs")
    assert flow.tick(conn, settings, at(13, 1)) == 1               # off-peak: the weakest
    conn.execute("DELETE FROM jobs")
    db.kv_set(conn, "setting_prime_times", "")
    assert flow.tick(conn, settings, at(13, 1)) == 2               # no prime times: best first
    assert pacing.current_slot(at(6), pacing.parse_times("07:00")) is None


def test_the_plan_shows_the_order_and_slots_and_yaish_can_reorder_it(env, tmp_path):
    settings, conn = env
    for cid, score in ((1, 40), (2, 90), (3, 60)):
        add_clip(conn, tmp_path, cid, score=score)
    db.kv_set(conn, "setting_post_times", "07:00,13:00")
    db.kv_set(conn, "setting_prime_times", "07:00")
    db.kv_set(conn, "setting_daily_cap", "2")
    got = flow.plan(conn, settings, at(6))
    assert [p["clip_id"] for p in got] == [2, 1, 3]            # prime: best, off-peak: weakest, then next day
    assert [p["at"] for p in got] == [at(7), at(13), at(7, day=11)]
    assert got[0]["prime"] and not got[1]["prime"] and not any(p["manual"] for p in got)

    flow.reorder(conn, settings, 3, "top", at(6))
    assert [p["clip_id"] for p in flow.plan(conn, settings, at(6))] == [3, 2, 1]
    flow.reorder(conn, settings, 1, "up", at(6))
    assert [p["clip_id"] for p in flow.plan(conn, settings, at(6))] == [3, 1, 2]
    assert flow.tick(conn, settings, at(13, 1)) == 3           # the manual order beats the off-peak rule
    with pytest.raises(ValueError):
        flow.reorder(conn, settings, 99, "up", at(6))
    flow.reset_order(conn)
    assert [p["clip_id"] for p in flow.plan(conn, settings, at(6))] == [2, 1, 3]
