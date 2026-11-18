"""Campaign discovery: public-page parsers, the stored list, joining, and the dashboard pages."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.app.web import create_app
from clipforge.core import autopilot, db
from clipforge.core.config import get_settings
from clipforge.discover import sources as src, store


def page(text: str) -> str:
    return f"<html><script>self.__next_f.push([1,{json.dumps(text)}])</script></html>"


CR_ITEMS = [
    {"id": "aaa", "title": "GTA RP clipping$$", "brand": "Skii", "category": "Gaming",
     "platforms": ["YouTube", "TikTok"], "ratePer1kLabel": "$$1.50 - $$2.00",
     "budgetTotalRaw": 5000, "availableBudgetRaw": 4000, "requiresApplication": False},
    {"id": "bbb", "title": "Perfume promo", "brand": "Scent", "category": "Lifestyle",
     "platforms": ["TikTok"], "payoutSortRaw": 3, "budgetTotalRaw": 900, "availableBudgetRaw": 900},
    {"id": "ccc", "title": "Old Fortnite thing", "brand": "X", "category": "Gaming",
     "platforms": ["YouTube"], "payoutSortRaw": 1, "budgetTotalRaw": 900, "availableBudgetRaw": 0},
]
CR_HTML = page('1:{"campaigns":' + json.dumps(CR_ITEMS) + "}")
VYRO = [
    {"id": "v1", "slug": "zone-wars", "name": "Zonewars highlights", "type": "clipping",
     "workspace": {"name": "Zone"}, "targeting": {"platforms": ["youtube"]},
     "payoutModel": {"model": "CPM", "payRate": 0.8}, "headlineRate": 1.2,
     "budget": {"value": 2000, "remaining": 1500}, "isLocked": False,
     "status": {"id": "active"}, "requirements": {"minViewsPerClip": 1000}},
    {"id": "v2", "slug": "locked-one", "name": "Locked", "isLocked": True,
     "payoutModel": {"model": "CPM", "payRate": 2}, "budget": {"value": 10, "remaining": 10},
     "status": {"id": "active"}, "targeting": {"platforms": ["youtube"]}},
]
VYRO_HTML = page('"initialPage":' + json.dumps({"data": VYRO}))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    get_settings.cache_clear()
    settings = get_settings()
    conn = db.open_db(settings.db_path)
    yield settings, conn
    conn.close()
    get_settings.cache_clear()


def fetch(url: str) -> str:
    return VYRO_HTML if "vyro" in url else CR_HTML


def test_parse_contentrewards():
    found = {f.external_id: f for f in src.parse_contentrewards(CR_HTML)}
    a = found["aaa"]
    assert a.title == "GTA RP clipping$" and a.gaming and a.platforms == ["youtube", "tiktok"]
    assert (a.rate_min, a.rate_max, a.budget_left) == (1.5, 2.0, 4000)
    assert a.url == "https://contentrewards.com/c/campaigns/aaa"
    assert found["bbb"].rate_max == 3 and not found["bbb"].gaming
    assert found["ccc"].ended


def test_parse_vyro():
    found = {f.external_id: f for f in src.parse_vyro(VYRO_HTML)}
    assert found["v1"].rate_max == 1.2 and found["v1"].gaming and found["v1"].min_views == 1000
    assert found["v1"].url == "https://vyro.com/campaigns/zone-wars" and not found["v1"].ended
    assert found["v2"].locked


def test_parsers_survive_junk():
    assert src.parse_contentrewards("<html>nothing</html>") == []
    assert src.parse_vyro(page("no data here")) == []


def test_refresh_is_idempotent_and_keeps_choices(env):
    _, conn = env
    first = store.refresh(conn, fetch=fetch)
    assert len(first["new"]) == 5
    assert store.refresh(conn, fetch=fetch)["new"] == []
    gta = conn.execute("SELECT id FROM discovered WHERE external_id='aaa'").fetchone()["id"]
    store.set_state(conn, gta, "watching")
    store.refresh(conn, fetch=fetch)
    assert conn.execute("SELECT state FROM discovered WHERE id=?", (gta,)).fetchone()[0] == "watching"
    assert not store.due(conn)


def test_refresh_reports_a_site_that_is_down(env):
    _, conn = env

    def flaky(url):
        if "vyro" in url:
            raise RuntimeError("down")
        return CR_HTML

    report = store.refresh(conn, fetch=flaky)
    assert report["errors"] and report["new"]


def test_query_filters(env):
    _, conn = env
    store.refresh(conn, fetch=fetch)
    titles = lambda **kw: {r["title"] for r in store.query(conn, **kw)}   # noqa: E731
    assert "Perfume promo" not in titles()                    # YouTube only by default
    assert "Perfume promo" in titles(youtube=False)
    assert titles(gaming=True) == {"GTA RP clipping$", "Zonewars highlights"}
    assert "Locked" not in titles(youtube=True) and "Locked" in titles(hide_locked=False)
    assert titles(min_rate=1.5, gaming=True) == {"GTA RP clipping$"}
    assert titles(source="vyro", hide_locked=True) == {"Zonewars highlights"}
    assert "Old Fortnite thing" not in titles(youtube=False)  # ended
    assert titles(text="zonewars") == {"Zonewars highlights"}
    assert store.query(conn, gaming=True, sort="rate")[0]["rate_max"] == 2.0


def test_hidden_rows_leave_the_list(env):
    _, conn = env
    store.refresh(conn, fetch=fetch)
    row = store.query(conn, gaming=True)[0]
    store.set_state(conn, row["id"], "hidden")
    assert row["id"] not in {r["id"] for r in store.query(conn, gaming=True)}
    assert row["id"] in {r["id"] for r in store.query(conn, state="hidden")}
    with pytest.raises(ValueError):
        store.set_state(conn, row["id"], "joined")


def test_mark_joined_creates_active_campaign_and_queues_brief(env):
    _, conn = env
    store.refresh(conn, fetch=fetch)
    did = conn.execute("SELECT id FROM discovered WHERE external_id='aaa'").fetchone()["id"]
    cid = store.mark_joined(conn, did, brief="Footage from Skii's Twitch streams only.")
    k = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    assert k["status"] == "active" and k["joined_at"] and k["rate_per_1k"] == 2.0
    assert json.loads(k["allowed_platforms"]) == ["youtube", "tiktok"]
    assert conn.execute("SELECT type FROM jobs WHERE ref_id=?", (cid,)).fetchone()[0] == "parse_brief"
    assert store.mark_joined(conn, did) == cid                # second click changes nothing
    row = conn.execute("SELECT state, campaign_id FROM discovered WHERE id=?", (did,)).fetchone()
    assert (row["state"], row["campaign_id"]) == ("joined", cid)


def test_join_without_brief_queues_nothing(env):
    _, conn = env
    store.refresh(conn, fetch=fetch)
    did = conn.execute("SELECT id FROM discovered WHERE external_id='v1'").fetchone()["id"]
    cid = store.mark_joined(conn, did)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='parse_brief'").fetchone()[0] == 0
    assert conn.execute("SELECT brief_text FROM campaigns WHERE id=?", (cid,)).fetchone()[0] is None


def test_brief_link_reads_a_public_doc_only(env):
    import httpx

    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="The brief")))
    login = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>Sign in</html>")))
    link = "https://docs.google.com/document/d/abc123/edit"
    assert store.brief_from_link(link, ok) == "The brief"
    assert store.brief_from_link(link, login) is None
    assert store.brief_from_link("https://example.com/x", ok) is None


def test_new_campaign_alert_picks_only_good_gaming_ones(env):
    _, conn = env
    store.refresh(conn, fetch=fetch)
    ids = [r["id"] for r in conn.execute("SELECT id FROM discovered")]
    sent = []
    assert autopilot.alert_new_campaigns(conn, ids, sent.append) == 2
    assert "GTA RP" in sent[0] and "Perfume" not in sent[0] and "Locked" not in sent[0]
    assert autopilot.alert_new_campaigns(conn, [], sent.append) == 0


def test_discover_pass_runs_once_per_window(env):
    settings, conn = env
    sent = []
    assert autopilot.discover_pass(conn, settings, fetch=fetch, notify=sent.append)["new"]
    assert sent
    assert autopilot.discover_pass(conn, settings, fetch=fetch, notify=sent.append) == {}


@pytest.fixture()
def client(env):
    settings, conn = env
    store.refresh(conn, fetch=fetch)
    c = TestClient(create_app(settings), follow_redirects=False)
    assert c.post("/login", data={"password": "pw"}).status_code == 303
    return c, conn


def test_discover_page_filters_and_requires_login(env, client):
    settings, _ = env
    c, _ = client
    html = c.get("/discover").text
    assert "GTA RP clipping" in html and "Perfume" not in html
    assert "Zonewars" in c.get("/discover?source=vyro&gaming=1&youtube=1").text
    assert "Zonewars" not in c.get("/discover?min_rate=1.4&gaming=1&youtube=1").text
    anon = TestClient(create_app(settings), follow_redirects=False)
    assert anon.get("/discover").status_code == 303


def test_join_flow_through_the_dashboard(client):
    c, conn = client
    did = conn.execute("SELECT id FROM discovered WHERE external_id='aaa'").fetchone()["id"]
    page_html = c.get(f"/discover/{did}").text
    assert "https://contentrewards.com/c/campaigns/aaa" in page_html
    r = c.post(f"/discover/{did}/joined", data={"brief": "Twitch streams of Skii."})
    assert r.status_code == 303
    cid = conn.execute("SELECT campaign_id FROM discovered WHERE id=?", (did,)).fetchone()[0]
    assert r.headers["location"].startswith(f"/campaigns/{cid}")
    assert "GTA RP clipping" in c.get(f"/campaigns/{cid}").text
    c.post(f"/discover/{did}/state", data={"state": "hidden"})   # refused for joined, no crash
    assert conn.execute("SELECT state FROM discovered WHERE id=?", (did,)).fetchone()[0] == "joined"


def test_campaign_page_channel_approval(client):
    from clipforge.ingest import briefs
    from clipforge.ingest.rules import BriefRules

    c, conn = client
    cid = int(conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at, brief_text) "
        "VALUES ('whop','s','Skii','active',datetime('now'),'brief')").lastrowid)
    briefs.save_rules(conn, cid, BriefRules())
    c.post(f"/campaigns/{cid}/channel", data={"channel": "https://twitch.tv/skiimasktimmy/videos"})
    assert briefs.load_rules(conn, cid).approved_channels == ["https://twitch.tv/skiimasktimmy"]
    c.post(f"/campaigns/{cid}/channel", data={"channel": "not a link"})
    assert len(briefs.load_rules(conn, cid).approved_channels) == 1
    c.post(f"/campaigns/{cid}/scan")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='autopilot'").fetchone()[0] == 1


def test_overview_autopilot_and_warmup_controls(client):
    c, conn = client
    home = c.get("/").text
    assert "Needs you" in home and "Autopilot" in home and "Next post" in home
    c.post("/autopilot", data={"mode": "off"})
    assert not autopilot.enabled(conn)
    c.post("/autopilot", data={"mode": "on"})
    assert autopilot.enabled(conn)
    c.post("/warmup", data={"warmup_date": "2026-11-19"})
    assert autopilot.runway(conn)["date"] == "2026-11-19"
    c.post("/warmup", data={"warmup_date": "soon"})
    assert autopilot.runway(conn)["date"] == "2026-11-19"


def test_refresh_links_campaigns_joined_by_hand(env):
    _, conn = env
    cid = int(conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
        "VALUES ('whop','aaa','Joined earlier','active',datetime('now'))").lastrowid)
    store.refresh(conn, fetch=fetch)
    row = conn.execute("SELECT state, campaign_id FROM discovered WHERE external_id='aaa'").fetchone()
    assert (row["state"], row["campaign_id"]) == ("joined", cid)


def test_refresh_logs_a_joined_campaigns_budget_only_when_it_changes(env):
    _, conn = env
    cid = int(conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
        "VALUES ('whop','aaa','Joined earlier','active',datetime('now'))").lastrowid)
    store.refresh(conn, fetch=fetch)
    store.refresh(conn, fetch=fetch)                                   # links it, then logs once
    store.refresh(conn, fetch=fetch)
    left = conn.execute("SELECT budget_left FROM discovered WHERE external_id='aaa'").fetchone()[0]
    logged = [r[0] for r in conn.execute("SELECT budget_left FROM budget_log")]
    assert (logged == [left]) if left is not None else (logged == [])
    if left is not None:
        assert conn.execute("SELECT budget_left FROM campaigns WHERE id=?", (cid,)).fetchone()[0] == left
