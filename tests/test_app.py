"""Phase 4: dashboard auth and pages, the review flow, and the Telegram bot's rules."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from clipforge.app import review, telegram as tg
from clipforge.app.web import create_app, make_token, valid_token
from clipforge.core import db
from clipforge.core.config import get_settings

PW = "correct horse"
RULES = {"required_hashtags": ["#skiimasktimmy"]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PW)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("CLIPFORGE_DB", str(tmp_path / "t.db"))
    get_settings.cache_clear()
    settings = get_settings()
    conn = db.open_db(settings.db_path)
    conn.execute("INSERT INTO campaigns (id, platform, name, creator, rate_per_1k, rules_json, status)"
                 " VALUES (1,'whop','Camp','skiimasktimmy',1.0,?, 'active')", (json.dumps(RULES),))
    conn.execute("INSERT INTO sources (id, campaign_id, creator, url) VALUES (1,1,'skiimasktimmy','u')")
    conn.execute("INSERT INTO moments (id, source_id, start, \"end\", score, hook) "
                 "VALUES (1,1,0,30,55,'hook')")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0" * 1000)
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"jpg")
    zipf = tmp_path / "package.zip"
    zipf.write_bytes(b"zip")
    meta = {"title": "A title", "description": "Hi\nwatch twitch.tv/skiimasktimmy\n#skiimasktimmy",
            "handle": "twitch.tv/skiimasktimmy", "duration": 30, "hook": "hook"}
    conn.execute("INSERT INTO clips (id, moment_id, path, thumb_path, package_path, meta_json, "
                 "compliance_ok, compliance_notes, status) VALUES (1,1,?,?,?,?,1,'WARN x: y','ready')",
                 (str(video), str(thumb), str(zipf), json.dumps(meta)))
    conn.execute("INSERT INTO clips (id, moment_id, meta_json, compliance_ok, status) "
                 "VALUES (2,1,'{}',0,'held')")
    return settings, conn


@pytest.fixture
def client(env):
    c = TestClient(create_app(env[0]), follow_redirects=False)
    return c


def login(c):
    r = c.post("/login", data={"password": PW})
    assert r.status_code == 303
    return c


# ---- auth ---------------------------------------------------------------

def test_dashboard_will_not_start_without_a_password(monkeypatch, env):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        create_app(get_settings())


def test_tokens_verify_expire_and_do_not_cross_passwords():
    t = make_token("a", now=1000)
    assert valid_token("a", t, now=1001)
    assert not valid_token("b", t, now=1001)
    assert not valid_token("a", t, now=1000 + 10 * 24 * 3600)
    assert not valid_token("a", "garbage") and not valid_token("a", None)


def test_every_page_needs_the_password(client):
    for path in ("/", "/ready", "/queue", "/posted", "/campaigns", "/learn", "/settings",
                 "/clip/1/video", "/clip/1/package"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert client.post("/clip/1/approve").status_code == 303


def test_wrong_password_is_refused_and_repeats_are_throttled(client):
    for _ in range(5):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert client.post("/login", data={"password": PW}).status_code == 429


def test_logged_in_user_sees_every_page(client):
    login(client)
    for path in ("/", "/ready", "/queue", "/posted", "/campaigns", "/learn", "/settings",
                 "/clip/1/edit"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "A title" in client.get("/ready").text


def test_queue_page_names_the_clip_and_hides_the_autopilot_noise(client, env):
    from clipforge.core import queue as queue_mod

    conn = env[1]
    queue_mod.enqueue(conn, "autopilot", dedupe=False)
    queue_mod.enqueue(conn, "make_clip", ref_id=1, dedupe=False)
    login(client)
    r = client.get("/queue")
    assert "Camp: hook" in r.text and "Making clip" in r.text
    assert "autopilot" not in r.text.lower()


def test_the_campaigns_list_also_offers_leave_and_rejoin(client, env):
    login(client)
    conn = env[1]
    assert "action=\"/campaigns/1/hold\"" in client.get("/campaigns").text
    conn.execute("UPDATE campaigns SET status='skipped', joined_at=datetime('now') WHERE id=1")
    assert "action=\"/campaigns/1/resume\"" in client.get("/campaigns").text


def test_leaving_a_campaign_holds_it_and_rejoining_resumes_it(client, env):
    login(client)
    conn = env[1]
    conn.execute("UPDATE campaigns SET joined_at=datetime('now') WHERE id=1")
    r = client.post("/campaigns/1/hold")
    assert r.status_code == 303
    assert conn.execute("SELECT status FROM campaigns WHERE id=1").fetchone()[0] == "skipped"
    r = client.post("/campaigns/1/resume")
    assert r.status_code == 303
    assert conn.execute("SELECT status FROM campaigns WHERE id=1").fetchone()[0] == "active"


def test_cross_site_posts_are_refused(client):
    login(client)
    r = client.post("/clip/1/approve", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


# ---- review ---------------------------------------------------------------

def test_approve_reject_and_undo(env):
    _, conn = env
    assert review.approve(conn, 1)["status"] == "approved"
    assert review.restore(conn, 1)["status"] == "ready"
    assert review.reject(conn, 1, "boring")["status"] == "rejected"
    assert review.meta_of(review.get(conn, 1))["review_note"] == "boring"


def test_a_clip_that_failed_the_brief_cannot_be_approved(env):
    _, conn = env
    with pytest.raises(review.ReviewError):
        review.approve(conn, 2)


def test_posted_clips_are_untouchable(env):
    _, conn = env
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    for fn in (review.approve, review.reject, review.restore):
        with pytest.raises(review.ReviewError):
            fn(conn, 1)


def test_edit_keeps_required_text_and_refuses_removing_it(env):
    _, conn = env
    ok = review.edit(conn, 1, title="New title", description="Fresh\ntwitch.tv/skiimasktimmy #skiimasktimmy")
    assert review.meta_of(ok)["title"] == "New title"
    with pytest.raises(review.ReviewError):
        review.edit(conn, 1, title="T", description="no tags here")
    with pytest.raises(review.ReviewError):
        review.edit(conn, 1, title="x" * 200, description="#skiimasktimmy twitch.tv/skiimasktimmy")


def test_approving_through_the_dashboard(client, env):
    login(client)
    r = client.post("/clip/1/approve")
    assert r.status_code == 303
    assert review.get(env[1], 1)["status"] == "approved"


def test_files_come_only_from_recorded_paths(client):
    login(client)
    assert client.get("/clip/1/video").content == b"0" * 1000
    assert client.get("/clip/1/package").content == b"zip"
    assert client.get("/clip/2/video").status_code == 404
    assert client.get("/clip/999/video").status_code == 404


def test_settings_validate_and_persist(client, env):
    login(client)
    r = client.post("/settings", data={"review_mode": "review", "auto_threshold": "70",
                                       "daily_cap": "20", "post_times": "10:00",
                                       "lkr_per_usd": "300"})
    assert "Out%20of%20range" in r.headers["location"]
    r = client.post("/settings", data={"review_mode": "auto", "auto_threshold": "70",
                                       "daily_cap": "4", "post_times": "10:00, 15:30",
                                       "lkr_per_usd": "310"})
    assert "Saved" in r.headers["location"]
    assert db.kv_get(env[1], "setting_post_times") == "10:00,15:30"


# ---- telegram ---------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, settings, method, **kw):
        self.calls.append((method, kw))
        return {}


def test_bot_ignores_everyone_but_the_configured_chat(env, monkeypatch):
    settings, conn = env
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    tg.handle(conn, settings, {"callback_query": {"id": "1", "data": "a:1",
              "message": {"chat": {"id": 999}, "message_id": 5}}})
    assert rec.calls == [] and review.get(conn, 1)["status"] == "ready"


def test_approve_button_approves_and_acknowledges(env, monkeypatch):
    settings, conn = env
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    tg.handle(conn, settings, {"callback_query": {"id": "1", "data": "a:1",
              "message": {"chat": {"id": 42}, "message_id": 5}}})
    assert review.get(conn, 1)["status"] == "approved"
    assert [m for m, _ in rec.calls][0] == "answerCallbackQuery"


def test_buttons_on_a_held_clip_report_instead_of_approving(env, monkeypatch):
    settings, conn = env
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    tg.handle(conn, settings, {"callback_query": {"id": "1", "data": "a:2",
              "message": {"chat": {"id": 42}, "message_id": 5}}})
    assert review.get(conn, 2)["status"] == "held"
    assert "cannot become approved" in rec.calls[0][1]["text"]


def test_sweep_announces_each_ready_clip_once(env, monkeypatch, tmp_path):
    from clipforge.taste import book as book_mod

    settings, conn = env
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    monkeypatch.setattr(book_mod, "books_dir", lambda: tmp_path)          # no real drafts in this test
    assert tg.sweep(conn, settings) == 1
    assert tg.sweep(conn, settings) == 0
    assert [m for m, _ in rec.calls] == ["sendVideo"]


def test_errors_never_carry_the_token(env, monkeypatch):
    import httpx

    settings, _ = env

    def boom(*a, **k):
        raise httpx.ConnectError("failed for https://api.telegram.org/bottok/getMe")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(tg.TelegramError) as exc:
        tg.call(settings, "getMe")
    assert "tok" not in str(exc.value)


def test_posted_clip_sends_a_cross_post_kit_once(env, monkeypatch):
    settings, conn = env
    conn.execute("UPDATE campaigns SET allowed_platforms=? WHERE id=1",
                 ('["youtube_shorts", "tiktok", "instagram_reels"]',))
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    conn.execute("INSERT INTO posts (clip_id, url, posted_at) VALUES (1,'https://y/1',"
                 "datetime('now'))")
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    tg.sweep(conn, settings)
    texts = [k.get("text", "") for m, k in rec.calls if m == "sendMessage"]
    assert any(t.startswith("TikTok caption") and "watch twitch.tv/skiimasktimmy" in t for t in texts)
    assert any(t.startswith("Instagram Reels caption") for t in texts)
    assert [m for m, _ in rec.calls].count("sendVideo") == 1
    n = len(rec.calls)
    tg.sweep(conn, settings)
    assert len(rec.calls) == n


def test_no_kit_when_the_campaign_is_youtube_only_or_switched_off(env, monkeypatch):
    settings, conn = env
    conn.execute("UPDATE campaigns SET allowed_platforms=? WHERE id=1", ('["youtube_shorts"]',))
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    conn.execute("INSERT INTO posts (clip_id, url, posted_at) VALUES (1,'u',datetime('now'))")
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    tg.sweep(conn, settings)
    assert not any("caption (clip" in k.get("text", "") for _, k in rec.calls)
    conn.execute("UPDATE campaigns SET allowed_platforms=? WHERE id=1", ('["tiktok"]',))
    conn.execute("DELETE FROM kv WHERE key='tg_kit_1'")
    db.kv_set(conn, "setting_crosspost", "0")
    tg.sweep(conn, settings)
    assert not any("caption (clip" in k.get("text", "") for _, k in rec.calls)


def _note(text, caption="Clip 1  score 55\nA title"):
    return {"message": {"chat": {"id": 42}, "text": text,
                        "reply_to_message": {"caption": caption}}}


def _redo_env(env, monkeypatch, edit):
    from clipforge.app import redo

    settings, conn = env
    meta = review.meta_of(review.get(conn, 1))
    meta.update(source_start=100.0, source_end=130.0, duration=30.0)
    conn.execute("UPDATE clips SET meta_json=? WHERE id=1", (json.dumps(meta),))
    conn.execute("UPDATE moments SET status='clipped' WHERE id=1")
    monkeypatch.setattr(redo.llm, "parse", lambda *a, **k: edit)
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    return settings, conn, rec


def test_replying_to_a_clip_remakes_it_with_the_trim(env, monkeypatch):
    from clipforge.app import redo

    settings, conn, rec = _redo_env(env, monkeypatch, redo.Edit(trim_end=3.0, hook="Behind you"))
    tg.handle(conn, settings, _note("last few seconds are unclear"))
    clip = conn.execute("SELECT status, package_path FROM clips WHERE id=1").fetchone()
    assert clip["status"] == "rejected" and clip["package_path"] is None
    m = conn.execute('SELECT start, "end", hook, status FROM moments WHERE id=1').fetchone()
    assert (m["start"], m["end"], m["hook"], m["status"]) == (100.0, 127.0, "Behind you", "candidate")
    over = json.loads((settings.data_dir / "work" / "clip-1" / "override.json").read_text())
    assert over == {"start": 100.0, "end": 127.0}
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='make_clip' AND ref_id=1").fetchone()[0] == 1
    assert "Remaking clip 1" in rec.calls[-1][1]["text"]
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 2     # nothing deleted


def test_a_note_that_would_break_the_length_changes_nothing(env, monkeypatch):
    from clipforge.app import redo

    settings, conn, rec = _redo_env(env, monkeypatch, redo.Edit(trim_end=28.0))
    tg.handle(conn, settings, _note("cut it down"))
    assert review.get(conn, 1)["status"] == "ready"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert "must be" in rec.calls[-1][1]["text"]


def test_a_posted_clip_cannot_be_remade_and_a_stray_note_gets_help(env, monkeypatch):
    from clipforge.app import redo

    settings, conn, rec = _redo_env(env, monkeypatch, redo.Edit(trim_end=2.0))
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    tg.handle(conn, settings, _note("cut the end"))
    assert "only clips not yet posted" in rec.calls[-1][1]["text"]
    tg.handle(conn, settings, {"message": {"chat": {"id": 42}, "text": "hello"}})
    assert "Reply to a clip" in rec.calls[-1][1]["text"]


def test_plain_notes_work_without_the_model():
    from clipforge.app import redo

    assert redo._fallback("cut the last 3 seconds").trim_end == 3.0
    assert redo._fallback("trim the first 1.5 sec").trim_start == 1.5
    assert redo._fallback("make it purple").unsupported


def test_a_posted_clip_can_be_replaced_and_the_remake_may_post(env, monkeypatch):
    from clipforge.app import redo
    from clipforge.publish import flow

    settings, conn, rec = _redo_env(env, monkeypatch, redo.Edit(hook="h"))
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    conn.execute("INSERT INTO posts (clip_id, url, posted_at) VALUES (1,'u',datetime('now'))")
    with pytest.raises(redo.RedoError):
        redo.apply(conn, settings, 1, redo.Edit(hook="h"))
    redo.apply(conn, settings, 1, redo.Edit(hook="h"), replace_posted=True)
    assert review.get(conn, 1)["status"] == "rejected"
    conn.execute("INSERT INTO clips (id, moment_id, path, package_path, meta_json, compliance_ok, "
                 "status) VALUES (3,1,'p','z','{}',1,'approved')")
    conn.execute("UPDATE campaigns SET joined_at=datetime('now') WHERE id=1")
    assert [r["id"] for r in flow.candidates(conn)] == [3]


def test_a_facecam_reply_remakes_the_clip_and_asks_for_a_wide_search(env, monkeypatch):
    from clipforge.app import redo

    settings, conn, rec = _redo_env(env, monkeypatch, redo.Edit())
    tg.handle(conn, settings, _note("facecam is not available"))
    assert review.get(conn, 1)["status"] == "rejected"
    over = json.loads((settings.data_dir / "work" / "clip-1" / "override.json").read_text())
    assert over == {"start": 100.0, "end": 130.0, "recam": True}
    assert "facecam" in rec.calls[-1][1]["text"]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='make_clip' AND ref_id=1").fetchone()[0] == 1


def test_review_page_splits_blockers_from_things_to_check_by_eye(client, env):
    conn = env[1]
    conn.execute("UPDATE clips SET compliance_notes=? WHERE id=1",
                 ("WARN banned_edits: check by eye, the machine cannot: cover his voice with music\n"
                  "FAIL facecam: no facecam is visible",))
    login(client)
    text = client.get("/ready").text
    assert "Check by eye: The brief bans these edits" in text and "cover his voice with music" in text
    assert "Blocks posting: Facecam" in text and "the machine cannot" not in text


def test_posted_page_records_the_campaigns_review_and_counts_it(client, env):
    conn = env[1]
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    pid = conn.execute("INSERT INTO posts (clip_id, url, posted_at) VALUES (1,'https://y/1',datetime('now'))").lastrowid
    for v in (5, 40, 90):
        conn.execute("INSERT INTO stats (post_id, captured_at, views) VALUES (?, datetime('now', ?), ?)",
                     (pid, f"-{100 - v} minutes", v))
    login(client)
    page = client.get("/posted").text
    assert "<polyline" in page and "A title" in page and "/clip/1/thumb" in page
    assert client.post(f"/post/{pid}/review", data={"status": "approved"}).status_code == 303
    assert conn.execute("SELECT campaign_review_status FROM posts WHERE id=?", (pid,)).fetchone()[0] == "approved"
    assert client.post(f"/post/{pid}/review", data={"status": "nonsense"}).status_code == 303
    assert conn.execute("SELECT campaign_review_status FROM posts WHERE id=?", (pid,)).fetchone()[0] == "approved"
    assert "1 approved" in client.get("/campaigns").text
    home = client.get("/").text
    assert "Needs you" in home and "Views gained per day" in home


def test_spark_and_daily_gains_are_safe_on_thin_data(env):
    from clipforge.app import stats_view

    assert stats_view.spark([]) == "" and stats_view.spark([7]) == ""
    assert len(stats_view.spark([0, 0]).split()) == 2
    days = stats_view.daily_gains(env[1])
    assert len(days) == 14 and all(d["views"] == 0 and d["day"] for d in days)


def test_overview_explains_the_missing_permission_then_shows_countries(client, env):
    conn = env[1]
    login(client)
    assert "No country data yet" in client.get("/").text
    db.kv_set(conn, "geo_status", "needs_consent")
    assert "youtube-auth" in client.get("/").text
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    pid = conn.execute("INSERT INTO posts (clip_id, url, posted_at) VALUES (1,'https://y/1',datetime('now'))").lastrowid
    conn.executemany("INSERT INTO geo (post_id, country, views) VALUES (?,?,?)", [(pid, "US", 3), (pid, "LK", 1)])
    assert "75%</span> of 4 counted views" in client.get("/").text
    assert "Tier 1: 75%" in client.get("/posted").text and "Tier 1: 75%" in client.get("/campaigns").text


def test_budget_drain_and_tier1_quota_reach_the_dashboard(client, env):
    from clipforge.app import stats_view
    from clipforge.ingest import briefs, rules as rules_mod

    conn = env[1]
    did = conn.execute(
        "INSERT INTO discovered (source, external_id, title, platforms_json, budget_total, budget_left, "
        "campaign_id, state, url) VALUES ('whop','x','Camp','[]',1000,90,1,'joined','u')").lastrowid
    conn.execute("INSERT INTO budget_log (discovered_id, captured_at, budget_left) "
                 "VALUES (?, datetime('now','-24 hours'), 190)", (did,))
    conn.execute("INSERT INTO budget_log (discovered_id, budget_left) VALUES (?, 90)", (did,))
    b = stats_view.budgets(conn)[1]
    assert 95 < b["per_day"] < 105 and "runs out in about 1 day" in b["warn"]
    conn.execute("UPDATE campaigns SET joined_at=datetime('now') WHERE id=1")
    rules = rules_mod.BriefRules(required_hashtags=["#skiimasktimmy"], min_tier1_share=0.5,
                                 audience_requirements="US viewers only")
    briefs.save_rules(conn, 1, rules)
    conn.execute("UPDATE clips SET status='posted' WHERE id=1")
    pid = conn.execute("INSERT INTO posts (clip_id, posted_at) VALUES (1, datetime('now'))").lastrowid
    conn.executemany("INSERT INTO geo (post_id, country, views) VALUES (?,?,?)", [(pid, "US", 1), (pid, "LK", 3)])
    login(client)
    home = client.get("/").text
    assert "runs out in about 1 day" in home and "25% of viewers are Tier 1, the brief wants 50%" in home
    assert "brief wants 50% Tier 1" in client.get("/campaigns").text
    page = client.get("/campaigns/1").text
    assert "At least 50% from Tier 1 countries" in page and "US viewers only" in page and "may not be paid" in page


def test_overview_shows_a_live_stream_and_settings_keep_the_live_clip_count(client, env):
    import json
    import time

    from clipforge.ingest import live

    conn = env[1]
    db.kv_set(conn, live.KV_STATE, json.dumps({"at": time.time(), "live": [
        {"login": "skiimasktimmy", "campaign_id": 1, "since": time.time() - 600, "messages": 1234,
         "moments": 2, "vod": True}]}))
    login(client)
    home = client.get("/").text
    assert "Live now" in home and "skiimasktimmy" in home and "1,234 messages" in home and "2 reactions" in home
    db.kv_set(conn, live.KV_STATE, json.dumps({"at": time.time() - 3600, "live": [{"login": "x"}]}))
    assert "Live now" not in client.get("/").text               # a watcher that stopped reporting
    client.post("/settings", data={"daily_cap": "3", "post_times": "10:00", "live_clips": "0"})
    assert live.clips_per_stream(conn) == 0


def test_queue_page_lists_what_posts_next_with_scores_and_moves_it(client, env):
    conn = env[1]
    conn.execute("UPDATE clips SET status='approved', compliance_ok=1 WHERE id=1")
    conn.execute("UPDATE campaigns SET joined_at=datetime('now'), status='active' WHERE id=1")
    login(client)
    page = client.get("/queue").text
    assert "Up next to post" in page and "/plan/1/move" in page
    r = client.post("/plan/1/move", data={"how": "top"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert conn.execute("SELECT post_rank FROM clips WHERE id=1").fetchone()[0] == 1
    assert "go back to automatic" in client.get("/queue").text
    client.post("/plan/reset")
    assert conn.execute("SELECT post_rank FROM clips WHERE id=1").fetchone()[0] is None


@pytest.fixture()
def books(env, tmp_path, monkeypatch):
    """A throwaway tastebook/ and rubrics/ with one draft that has a rubric draft beside it."""
    from clipforge.taste import book as book_mod

    bdir, rdir = tmp_path / "tastebook", tmp_path / "rubrics"
    bdir.mkdir()
    rdir.mkdir()
    monkeypatch.setattr(book_mod, "books_dir", lambda: bdir)
    monkeypatch.setenv("RUBRICS_DIR", str(rdir))
    get_settings.cache_clear()
    data = {"niche": "streamer-skii", "status": "draft", "sample": {"videos": 12},
            "pace": {"cuts_per_10s": 2.1},
            "style": {"zoom": {"enabled": True}, "captions": {"highlight": "#FFE000", "words_per_event": 2}}}
    (bdir / "streamer-skii.draft.json").write_text(json.dumps(data), encoding="utf-8")
    (bdir / "streamer-skii.draft.md").write_text("# Taste book: streamer-skii (DRAFT)\n", encoding="utf-8")
    (bdir / "gta.json").write_text(json.dumps({"niche": "gta", "status": "active"}), encoding="utf-8")
    (rdir / "streamer-skii.draft.json").write_text(json.dumps({
        "name": "streamer-skii", "evidence": {"rage": {"n": 9, "median_views": 1200000}},
        "categories": [{"id": "rage", "weight": 1.2, "description": "x"},
                       {"id": "surprise", "weight": 0.9, "description": "y"}], "penalties": []}),
        encoding="utf-8")
    return get_settings(), env[1], bdir, rdir


def press(conn, settings, data):
    tg.handle(conn, settings, {"callback_query": {"id": "1", "data": data,
              "message": {"chat": {"id": 42}, "message_id": 5}}})


def test_a_draft_book_is_announced_once_with_read_approve_reject(books, monkeypatch):
    settings, conn, bdir, rdir = books
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    assert tg._sweep_books(conn, settings) == 1 and tg._sweep_books(conn, settings) == 0
    method, kw = rec.calls[0]
    assert method == "sendMessage" and "streamer-skii" in kw["text"] and "rubric draft" in kw["text"]
    assert all(f"{a}:streamer-skii" in kw["reply_markup"] for a in ("tv", "ta", "tr"))


def test_read_sends_the_book_file_and_the_rubric_in_words(books, monkeypatch):
    settings, conn, bdir, rdir = books
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    press(conn, settings, "tv:streamer-skii")
    methods = [m for m, _ in rec.calls]
    assert methods[:3] == ["answerCallbackQuery", "sendDocument", "sendMessage"]   # acknowledged first
    words = rec.calls[2][1]["text"]
    assert "1.20  rage (9 top Shorts, median 1,200,000 views)" in words
    assert (bdir / "streamer-skii.draft.json").is_file()                  # reading changes nothing


def test_approve_makes_the_book_and_its_rubric_live(books, monkeypatch):
    settings, conn, bdir, rdir = books
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    press(conn, settings, "ta:streamer-skii")
    assert json.loads((bdir / "streamer-skii.json").read_text(encoding="utf-8"))["status"] == "active"
    assert (rdir / "streamer-skii.json").is_file() and not (rdir / "streamer-skii.draft.json").exists()
    assert "approved" in rec.calls[-1][1]["text"]


def test_reject_sets_the_draft_aside_without_deleting_it(books, monkeypatch):
    settings, conn, bdir, rdir = books
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    press(conn, settings, "tr:streamer-skii")
    assert (bdir / "streamer-skii.rejected.json").is_file() and (rdir / "streamer-skii.rejected.json").is_file()
    assert not (bdir / "streamer-skii.json").exists()
    press(conn, settings, "tr:streamer-skii")                              # a second press just reports
    assert "no draft" in rec.calls[-1][1]["text"]


def test_books_command_lists_live_and_draft_books(books, monkeypatch):
    settings, conn, bdir, rdir = books
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
    tg.handle(conn, settings, {"message": {"chat": {"id": 42}, "text": "/books"}})
    text = rec.calls[0][1]["text"]
    assert "gta: live" in text and "streamer-skii: draft" in text
    assert "ta:streamer-skii" in rec.calls[0][1]["reply_markup"] and "tv:gta" in rec.calls[0][1]["reply_markup"]


class LateRecorder(Recorder):
    """Telegram refuses the button acknowledgement, as it does for a press read after a restart."""

    def __call__(self, settings, method, **kw):
        if method == "answerCallbackQuery":
            raise tg.TelegramError("answerCallbackQuery: Bad Request: query is too old")
        return super().__call__(settings, method, **kw)


def test_a_press_read_too_late_to_acknowledge_still_does_its_job(books, monkeypatch):
    settings, conn, bdir, rdir = books
    rec = LateRecorder()
    monkeypatch.setattr(tg, "call", rec)
    press(conn, settings, "tv:streamer-skii")
    assert [m for m, _ in rec.calls][:2] == ["sendDocument", "sendMessage"]
    press(conn, settings, "a:1")
    assert review.get(conn, 1)["status"] == "approved"
    assert "approved" in rec.calls[-1][1]["text"]
