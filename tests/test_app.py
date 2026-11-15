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


def test_sweep_announces_each_ready_clip_once(env, monkeypatch):
    settings, conn = env
    rec = Recorder()
    monkeypatch.setattr(tg, "call", rec)
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
