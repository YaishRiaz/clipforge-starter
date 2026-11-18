"""Per-streamer and per-game taste books and rubrics: detection, blending, layering, queueing."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge.core import db
from clipforge.core.config import get_settings
from clipforge.moments import rubric as rb
from clipforge.taste import book, build, subjects

VOD = "https://www.twitch.tv/videos/2918610608"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    c = db.open_db(get_settings().db_path)
    c.execute("INSERT INTO campaigns (id, platform, name, category, status, joined_at) "
              "VALUES (1,'whop','Skii','streamer','active','2026-11-01 00:00:00')")
    c.execute("INSERT INTO sources (id, campaign_id, creator, url, duration) VALUES (1,1,'skii',?,14190)", (VOD,))
    yield c
    c.close()
    get_settings.cache_clear()


def gql(video):
    return lambda body: SimpleNamespace(status_code=200, json=lambda: {"data": {"video": video}})


CHAPTERED = {"lengthSeconds": 14190, "owner": {"displayName": "SkiiMaskTimmy"},
             "game": {"displayName": "Rocket League"}, "moments": {"edges": [
                 {"node": {"positionMilliseconds": 0, "durationMilliseconds": 12020000, "type": "GAME_CHANGE",
                           "details": {"game": {"displayName": "Rocket League"}}}},
                 {"node": {"positionMilliseconds": 12020000, "durationMilliseconds": 2170000,
                           "type": "GAME_CHANGE", "details": {"game": {"displayName": "Run Rob"}}}}]}}


def test_twitch_chapters_give_the_game_at_each_moment():
    got = subjects.fetch_twitch("1", post=gql(CHAPTERED))
    assert got["streamer"] == "SkiiMaskTimmy"
    assert got["games"] == [[0.0, 12020.0, "Rocket League"], [12020.0, 14190.0, "Run Rob"]]
    assert subjects.game_at(got["games"], 100, 130) == "Rocket League"
    assert subjects.game_at(got["games"], 12010, 12060) == "Run Rob"           # mostly after the switch
    single = subjects.fetch_twitch("1", post=gql({**CHAPTERED, "moments": {"edges": []}}))
    assert single["games"] == [[0.0, 14190.0, "Rocket League"]]                # no switches: the VOD's game


def test_keys_and_searches():
    assert subjects.streamer_key("SkiiMaskTimmy") == "streamer-skiimasktimmy"
    assert subjects.game_key("Run Rob") == "game-run-rob"
    assert subjects.game_key("Just Chatting") is None and subjects.streamer_key("") is None
    assert "SkiiMaskTimmy shorts" in subjects.queries("streamer-skiimasktimmy", "SkiiMaskTimmy")
    assert book.slug("streamer-skiimasktimmy") == "streamer-skiimasktimmy"     # hyphens survive


def test_a_sources_games_are_read_once_and_kept(conn):
    calls = []

    def fetch(vid):
        calls.append(vid)
        return subjects.fetch_twitch(vid, post=gql(CHAPTERED))

    row = conn.execute("SELECT * FROM sources WHERE id=1").fetchone()
    info = subjects.of_source(conn, row, fetch=fetch)
    again = subjects.of_source(conn, conn.execute("SELECT * FROM sources WHERE id=1").fetchone(), fetch=fetch)
    assert info == again and calls == ["2918610608"] and info["streamer"] == "SkiiMaskTimmy"

    def down(vid):
        raise RuntimeError("offline")

    conn.execute("INSERT INTO sources (id, campaign_id, creator, url) VALUES (2,1,'skii',?)",
                 ("https://www.twitch.tv/videos/5",))
    row2 = conn.execute("SELECT * FROM sources WHERE id=2").fetchone()
    assert subjects.of_source(conn, row2, fetch=down) == {"streamer": "skii", "games": []}
    assert conn.execute("SELECT games_json FROM sources WHERE id=2").fetchone()[0] is None   # asked again later


def write_rubric(d: Path, name: str, weights: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"name": name, "categories": [
        {"id": k, "weight": v, "description": k} for k, v in weights.items()],
        "penalties": [{"id": "dead_air", "weight": -1.0, "description": "x"}]}), encoding="utf-8")


def test_rubrics_blend_the_niche_with_the_streamer_and_game(tmp_path):
    d = tmp_path / "rubrics"
    write_rubric(d, "general", {"surprise": 1.0, "conflict": 0.8})
    write_rubric(d, "streamer-skiimasktimmy", {"surprise": 0.6, "rage": 1.2})
    write_rubric(d, "game-run-rob", {"surprise": 1.4})
    (d / "game-fortnite.draft.json").write_text("{}", encoding="utf-8")
    rb.for_campaign.cache_clear()
    got = rb.for_subjects(d, None, "streamer-skiimasktimmy", "game-run-rob")
    assert got.weight_of("surprise") == pytest.approx(1.0)                  # (1.0 + 0.6 + 1.4) / 3
    assert got.weight_of("rage") == 1.2 and got.weight_of("conflict") == 0.8
    only = rb.for_subjects(d, None, "streamer-nobody", "game-fortnite")     # a draft is not used
    assert only.name == "general"
    rb.for_campaign.cache_clear()


def test_the_edit_style_layers_niche_then_game_then_streamer(conn, tmp_path):
    d = tmp_path / "books"
    d.mkdir()

    def put(name, style, status="active"):
        (d / f"{name}.json").write_text(json.dumps({"niche": name, "status": status, "style": style}),
                                        encoding="utf-8")

    put("streamer", {"zoom": {"enabled": True, "scale": 1.1}, "captions": {"size": 60, "color": "#FFFFFF"}})
    put("game-run-rob", {"captions": {"size": 72}})
    put("streamer-skiimasktimmy", {"captions": {"color": "#FFE000"}})
    put("streamer-skiimasktimmy", {"captions": {"color": "#FFE000"}})
    conn.execute("UPDATE sources SET games_json=? WHERE id=1", (json.dumps(
        {"streamer": "SkiiMaskTimmy", "games": [[0, 12020, "Rocket League"], [12020, 14190, "Run Rob"]]}),))
    conn.execute('INSERT INTO moments (id, source_id, start, "end", game) VALUES (1,1,12100,12130,"Run Rob")')
    camp = conn.execute("SELECT * FROM campaigns WHERE id=1").fetchone()
    m = conn.execute("SELECT * FROM moments WHERE id=1").fetchone()
    got = book.for_clip(conn, camp, m, directory=d)
    assert got["niche"] == "streamer + game-run-rob + streamer-skiimasktimmy"
    assert got["style"]["captions"] == {"size": 72, "color": "#FFE000"}      # each layer changes only its own
    assert got["style"]["zoom"]["enabled"] is True
    put("game-run-rob", {"captions": {"size": 72}}, status="draft")          # a draft is skipped
    assert book.for_clip(conn, camp, m, directory=d)["style"]["captions"]["size"] == 60


def test_builds_are_queued_once_per_streamer_and_game_a_few_at_a_time(conn, tmp_path):
    conn.execute("UPDATE sources SET games_json=? WHERE id=1", (json.dumps(
        {"streamer": "SkiiMaskTimmy", "games": [[0, 1, "Rocket League"], [1, 2, "Run Rob"]]}),))
    base = tmp_path / "books"
    base.mkdir()
    (base / "game-rocket-league.json").write_text("{}", encoding="utf-8")   # already has a book
    conn.execute("UPDATE sources SET games_json=? WHERE id=1", (json.dumps(
        {"streamer": "SkiiMaskTimmy", "games": [[0, 10, "Rocket League"], [10, 100, "Run Rob"],
                                                [100, 120, "Bodycam"]]}),))
    first = build.queue_subjects(conn, base)
    assert first == ["streamer-skiimasktimmy", "game-run-rob"]             # streamer, then most played
    job = json.loads(conn.execute("SELECT payload_json FROM jobs WHERE type='taste_build' ORDER BY id").fetchone()[0])
    assert job["subject"] == "SkiiMaskTimmy" and "SkiiMaskTimmy shorts" in job["queries"]
    assert build.queue_subjects(conn, base) == []                            # two a day, never twice
    conn.execute("UPDATE jobs SET created_at=created_at-90000")              # a day later
    assert build.queue_subjects(conn, base) == ["game-bodycam"]
