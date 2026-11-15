"""Re-weighting a rubric from our own clips' views, and the campaign hold switch."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.__main__ import main
from clipforge.app.web import create_app
from clipforge.core import db
from clipforge.core.config import get_settings
from clipforge.moments import reweight, rubric as rubric_mod

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    rdir = tmp_path / "rubrics"
    rdir.mkdir()
    for n in ("gta.json", "general.json"):
        shutil.copyfile(REPO / "rubrics" / n, rdir / n)
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RUBRICS_DIR", str(rdir))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    get_settings.cache_clear()
    settings = get_settings()
    conn = db.open_db(settings.db_path)
    conn.execute("INSERT INTO campaigns (id, platform, name, status, joined_at) "
                 "VALUES (1,'whop','C','active',datetime('now'))")
    conn.execute("INSERT INTO sources (id, campaign_id, url) VALUES (1,1,'u')")
    rubric_mod.for_campaign.cache_clear()
    yield settings, conn
    conn.close()
    rubric_mod.for_campaign.cache_clear()
    get_settings.cache_clear()


def post(conn, cat, views, *, age_hours=100):
    mid = conn.execute('INSERT INTO moments (source_id, start, "end", score, rubric_category) '
                       "VALUES (1,0,30,50,?)", (cat,)).lastrowid
    cid = conn.execute("INSERT INTO clips (moment_id, status) VALUES (?, 'posted')", (mid,)).lastrowid
    pid = conn.execute("INSERT INTO posts (clip_id, posted_at) VALUES (?, datetime('now', ?))",
                       (cid, f"-{age_hours} hours")).lastrowid
    conn.execute("INSERT INTO stats (post_id, views) VALUES (?, ?)", (pid, views))


def gta(settings):
    return rubric_mod.load(settings.rubrics_dir / "gta.json")


def test_no_posts_changes_nothing(env):
    settings, conn = env
    assert all(c.new == c.old for c in reweight.propose(gta(settings), reweight.own_views(conn)))


def test_unsettled_posts_are_ignored(env):
    settings, conn = env
    for _ in range(4):
        post(conn, "police_chase", 100, age_hours=2)
    assert reweight.own_views(conn) == {}


def test_one_category_alone_has_nothing_to_compare_against(env):
    settings, conn = env
    for _ in range(5):
        post(conn, "police_chase", 100)
    assert all(c.new == c.old for c in reweight.propose(gta(settings), reweight.own_views(conn)))


def test_better_performer_goes_up_worse_goes_down_within_bounds(env):
    settings, conn = env
    for _ in range(6):
        post(conn, "police_chase", 10_000)
        post(conn, "big_reaction", 100)
    rub = gta(settings)
    by = {c.id: c for c in reweight.propose(rub, reweight.own_views(conn))}
    pc, br = by["police_chase"], by["big_reaction"]
    assert pc.new > pc.old and br.new < br.old
    assert pc.new <= round(pc.old * reweight.CEILING, 2)
    assert br.new >= round(br.old * reweight.FLOOR, 2)
    assert by["heist_fail"].new == by["heist_fail"].old          # no posts, untouched


def test_few_posts_move_less_than_many(env):
    settings, conn = env
    rub = gta(settings)
    few = {"police_chase": [10_000] * 3, "big_reaction": [100] * 3}
    many = {"police_chase": [10_000] * 30, "big_reaction": [100] * 30}
    f = {c.id: c for c in reweight.propose(rub, few)}["police_chase"]
    m = {c.id: c for c in reweight.propose(rub, many)}["police_chase"]
    assert f.old < f.new <= m.new


def test_apply_writes_new_version_and_keeps_the_old_file(env):
    settings, conn = env
    path = settings.rubrics_dir / "gta.json"
    before = json.loads(path.read_text(encoding="utf-8"))
    for _ in range(5):
        post(conn, "police_chase", 10_000)
        post(conn, "big_reaction", 100)
    out = reweight.apply(path, reweight.propose(gta(settings), reweight.own_views(conn)))
    assert out["version"] == before["version"] + 1
    assert (settings.rubrics_dir / f"gta.v{before['version']}.json").is_file()
    after = rubric_mod.load(path)
    assert after.weight_of("police_chase") > rubric_mod.load(
        settings.rubrics_dir / f"gta.v{before['version']}.json").weight_of("police_chase")
    assert json.loads(path.read_text(encoding="utf-8"))["own_evidence"]["police_chase"]["n"] == 5


def test_cli_dry_run_writes_nothing(env, capsys):
    settings, conn = env
    for _ in range(5):
        post(conn, "police_chase", 10_000)
        post(conn, "big_reaction", 100)
    before = (settings.rubrics_dir / "gta.json").read_text(encoding="utf-8")
    assert main(["rubric-reweight", "--dry-run"]) == 0
    assert (settings.rubrics_dir / "gta.json").read_text(encoding="utf-8") == before
    assert "dry run" in capsys.readouterr().out


def test_learn_page_and_apply(env):
    settings, conn = env
    c = TestClient(create_app(settings), follow_redirects=False)
    assert c.post("/login", data={"password": "pw"}).status_code == 303
    page = c.get("/learn")
    assert page.status_code == 200 and "Nothing to propose" in page.text
    for _ in range(5):
        post(conn, "police_chase", 10_000)
        post(conn, "big_reaction", 100)
    assert "Apply proposed weights" in c.get("/learn").text
    r = c.post("/learn/apply", headers={"origin": "http://testserver"})
    assert r.status_code == 303
    assert gta(settings).version == 3


def test_campaign_hold_and_resume(env, capsys):
    settings, conn = env
    assert main(["campaign-hold", "1"]) == 0
    assert conn.execute("SELECT status FROM campaigns WHERE id=1").fetchone()[0] == "skipped"
    from clipforge.core import autopilot
    assert autopilot.run(conn, settings, lister=lambda _c: [], fetch=lambda u: "")["clipping"] == 0
    assert main(["campaign-hold", "1", "--resume"]) == 0
    assert conn.execute("SELECT status FROM campaigns WHERE id=1").fetchone()[0] == "active"
    assert main(["campaign-hold", "99"]) == 2
