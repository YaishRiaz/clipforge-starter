"""Reference discovery, analysis plumbing and the draft taste book."""
from __future__ import annotations

import dataclasses
import json

import pytest

from clipforge.core import db
from clipforge.core.config import get_settings
from clipforge.taste import analyze, build, refs
from clipforge.taste import book as book_mod
from clipforge.taste.style import Style


def settings(tmp_path):
    return get_settings()


def look(**over):
    base = {"layout": "full_gameplay",
            "captions": {"present": True, "position": "lower_third", "colour": "white",
                         "highlight_word": True, "uppercase": True, "words_on_screen": 2},
            "hook_title": True, "hook_position": "top",
            "graphics": [{"kind": "emoji", "at_seconds": 12.0}], "note": "x"}
    base.update(over)
    return base


def an(cuts_per_10s=3.0, looked=True, **over):
    d = {"seconds": 30.0, "cuts": [2.0, 5.0], "cuts_per_10s": cuts_per_10s, "median_shot_s": 4.0,
         "loud_peaks": [3.0], "words_per_s": 2.5, "frame_times": [0.4, 7.5, 15.0, 22.5],
         "looked": look() if looked else None}
    d.update(over)
    return d


VIDEOS = [{"id": f"v{i}", "title": "t", "channel": f"c{i}", "views": 1000 - i, "seconds": 30,
           "url": f"https://www.youtube.com/shorts/v{i}"} for i in range(4)]


def test_aggregate_derives_style_and_cites_evidence():
    data, md = build.aggregate("gta", VIDEOS, {v["id"]: an() for v in VIDEOS})
    assert data["status"] == "draft" and data["sample"] == {"videos": 4, "inspected_in_frames": 4}
    st = Style.model_validate(data["style"])
    assert st.captions.highlight and st.captions.pop and st.captions.words_per_event == 2
    assert st.captions.margin_v == 330 and st.zoom.enabled and st.hook.enabled
    assert "https://www.youtube.com/shorts/v0?t=2" in md and "## Unknown" in md
    assert "Interpretation" in md and "emoji graphics" in md


def test_aggregate_slow_edit_switches_zoom_off_and_unseen_frames_stay_default():
    data, md = build.aggregate("x", VIDEOS, {v["id"]: an(cuts_per_10s=0.8, looked=False) for v in VIDEOS})
    assert not data["style"]["zoom"]["enabled"] and data["style"]["captions"] == {}
    assert data["sample"]["inspected_in_frames"] == 0
    assert any("vision model unavailable" in u for u in data["unsupported"])


def test_aggregate_ignores_captions_when_most_references_have_none():
    got = {v["id"]: an() for v in VIDEOS}
    for k in ("v1", "v2", "v3"):
        got[k]["looked"] = look(captions={"present": False})
    data, _ = build.aggregate("x", VIDEOS, got)
    assert "highlight" not in data["style"]["captions"]
    assert any("fewer than half" in u for u in data["unsupported"])


def test_find_keeps_one_per_channel_and_the_length_window():
    def mk(i, secs, views, ch):
        return {"id": i, "snippet": {"title": i, "channelId": ch},
                "statistics": {"viewCount": str(views)}, "contentDetails": {"duration": f"PT{secs}S"}}

    def get(path, params, _settings):
        if path == "search":
            return {"items": [{"id": {"videoId": x}} for x in ("a", "b", "c", "d")]}
        return {"items": [mk("a", 30, 900, "X"), mk("b", 40, 800, "X"),
                          mk("c", 8, 9999, "Y"), mk("d", 25, 100, "Z")]}

    got = refs.find(get_settings(), "gta", n=5, queries=("q",), get=get)
    assert [v["id"] for v in got] == ["a", "d"]


def test_look_falls_back_across_models_and_validates():
    calls = []

    class R:
        def __init__(self, code, body=None):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    def post(body):
        calls.append(body["model"])
        if len(calls) == 1:
            return R(404)
        return R(200, {"choices": [{"message": {"content": json.dumps(look())}}]})

    out = analyze.look([], dataclasses.replace(get_settings(), gemini_api_key="k"), post=post)
    assert out.captions.highlight_word and len(calls) == 2 and calls[0] != calls[1]
    with pytest.raises(RuntimeError):
        analyze.look([], dataclasses.replace(get_settings(), gemini_api_key=""), post=post)


def test_build_resumes_from_cache_deletes_reference_and_writes_a_draft(tmp_path, monkeypatch):
    s = settings(tmp_path)
    out = tmp_path / "books"
    out.mkdir()
    (out / "gta.json").write_text(json.dumps({"niche": "gta", "status": "active", "style": {}}))
    monkeypatch.setattr(refs, "find", lambda *a, **k: VIDEOS)
    fetched = []

    def fake_fetch(url, dest):
        fetched.append(url)
        p = dest / (url.rsplit("/", 1)[-1] + ".mp4")
        p.write_bytes(b"x")
        return p

    def fake_analyze(path, settings_, *, cache, vision):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(an()))
        return {}

    monkeypatch.setattr(refs, "fetch", fake_fetch)
    monkeypatch.setattr(analyze, "analyze", fake_analyze)
    pre = s.data_dir / "taste" / "gta" / "analysis"
    pre.mkdir(parents=True)
    (pre / "v0.json").write_text(json.dumps(an()))
    data = build.build(s, "gta", directory=out)
    assert len(fetched) == 3 and not any(u.endswith("/v0") for u in fetched)
    assert not list(refs.refs_dir(s, "gta").glob("*.mp4"))
    assert data["status"] == "draft" and (out / "gta.draft.md").is_file()
    assert book_mod.load("gta", directory=out)["status"] == "active"      # live book untouched


def test_build_refuses_a_thin_sample(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(refs, "find", lambda *a, **k: VIDEOS)

    def blocked(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(refs, "fetch", blocked)
    with pytest.raises(RuntimeError, match="at least 3"):
        build.build(s, "gta", directory=tmp_path)


def test_approve_promotes_the_draft_and_keeps_the_prior_book(tmp_path):
    (tmp_path / "gta.json").write_text(json.dumps({"niche": "gta", "status": "active", "source": "prior", "style": {}}))
    (tmp_path / "gta.draft.json").write_text(json.dumps({"niche": "gta", "status": "draft", "source": "measured", "style": {}}))
    (tmp_path / "gta.draft.md").write_text("evidence")
    book_mod.approve("gta", directory=tmp_path)
    live = json.loads((tmp_path / "gta.json").read_text())
    assert live["source"] == "measured" and live["status"] == "active"
    assert json.loads((tmp_path / "gta.prior.json").read_text())["source"] == "prior"
    assert (tmp_path / "gta.md").read_text() == "evidence" and not (tmp_path / "gta.draft.json").exists()


def test_queue_missing_builds_once_per_niche_and_skips_existing_books(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(campaigns)").fetchall()]
    assert {"category", "joined_at", "platform", "status"} <= set(cols)
    for name, cat in (("a", "Minecraft"), ("b", "Minecraft"), ("c", "gta"), ("d", "Streamer"), ("e", None)):
        conn.execute("INSERT INTO campaigns (name, platform, category, status, joined_at) "
                     "VALUES (?, 'youtube', ?, 'active', 1)", (name, cat))
    books = tmp_path / "books"
    books.mkdir()
    (books / "gta.json").write_text("{}")
    got = build.queue_missing(conn, get_settings(), directory=books)
    assert sorted(got) == ["minecraft", "streamer"]
    assert build.queue_missing(conn, get_settings(), directory=books) == []
    jobs = conn.execute("SELECT payload_json FROM jobs WHERE type='taste_build'").fetchall()
    assert sorted(json.loads(j[0])["niche"] for j in jobs) == ["minecraft", "streamer"]


def test_summary_tells_how_to_approve():
    data, _ = build.aggregate("gta", VIDEOS, {v["id"]: an() for v in VIDEOS})
    text = build.summary("gta", data)
    assert "taste-approve gta" in text and "brief" in text


def test_font_size_banner_and_six_word_captions_are_measured_into_the_book():
    looked = look(captions={"present": True, "position": "middle", "colour": "white", "highlight_word": False,
                            "uppercase": True, "words_on_screen": 6, "font_style": "heavy_block", "size": "large"},
                  title_stays=True, title_box=True,
                  graphics=[{"kind": "text_banner", "at_seconds": 1.0}, {"kind": "emoji", "at_seconds": 3.0}])
    data, md = build.aggregate("streamer-x", VIDEOS, {v["id"]: {**an(), "looked": looked} for v in VIDEOS})
    st = Style.model_validate(data["style"])
    assert st.captions.font == "Arial Black" and st.captions.size == 84 and st.captions.words_per_event == 6
    assert st.hook.banner and st.hook.box and st.hook.font == "Arial Black"
    assert any("banner" in r["rule"] for r in data["rules"])
    assert not any(u.startswith("text_banner") for u in data["unsupported"])      # rendered as the banner
    assert st.emoji.enabled and not any(u.startswith("emoji") for u in data["unsupported"])   # drawn now


def test_an_unknown_font_or_size_changes_nothing():
    data, _ = build.aggregate("x", VIDEOS, {v["id"]: an() for v in VIDEOS})
    st = Style.model_validate(data["style"])
    assert st.captions.font == "Arial" and st.captions.size == 66 and not st.hook.banner


def test_emoji_in_enough_references_switch_emoji_on():
    data, _ = build.aggregate("x", VIDEOS, {v["id"]: an() for v in VIDEOS})        # every reference has one
    assert Style.model_validate(data["style"]).emoji.enabled
    assert not any(u.startswith("emoji") for u in data["unsupported"])
    none = look(graphics=[])
    data2, _ = build.aggregate("x", VIDEOS, {v["id"]: {**an(), "looked": none} for v in VIDEOS})
    assert not Style.model_validate(data2["style"]).emoji.enabled
