"""The taste book: styles come from a niche book, and the campaign brief always wins."""
from __future__ import annotations

import json

from clipforge.core import db
from clipforge.edit import layout, subs
from clipforge.ingest.rules import BriefRules
from clipforge.taste import book as book_mod
from clipforge.taste import style as style_mod
from clipforge.taste import zoom as zoom_mod

GTA = {"niche": "gta", "status": "active",
       "style": {"captions": {"highlight": "#FFE000", "pop": True, "size": 70},
                 "zoom": {"enabled": True, "scale": 1.12},
                 "cuts": {"trim_lines": True}}}


def rules(**kw) -> BriefRules:
    return BriefRules(**kw)


def test_no_book_is_plain_default():
    r = style_mod.resolve(None, rules())
    assert r.niche == "none" and not r.style.zoom.enabled and r.style.captions.highlight == ""


def test_book_applies_when_brief_is_silent():
    r = style_mod.resolve(GTA, rules(banned_edits=["No personal watermarks"]))
    assert r.style.zoom.enabled and r.style.captions.highlight == "#FFE000"
    assert r.style.captions.size == 70 and r.dropped == []


def test_brief_ban_on_zoom_beats_book():
    r = style_mod.resolve(GTA, rules(banned_edits=["No zoom effects"]))
    assert not r.style.zoom.enabled
    assert any(d.startswith("zoom") for d in r.dropped)


def test_generic_effects_ban_drops_zoom_and_animation():
    r = style_mod.resolve(GTA, rules(banned_content=["Heavy edit or filters"]))
    assert not r.style.zoom.enabled and not r.style.captions.pop and r.style.captions.highlight == ""


def test_cutting_ban_drops_line_trimming():
    r = style_mod.resolve(GTA, rules(banned_edits=["Cutting or splicing the footage"]))
    assert not r.style.cuts.trim_lines and r.style.zoom.enabled


def test_caption_requirements_force_plain_captions():
    r = style_mod.resolve(GTA, rules(caption_requirements="Use white subtitle font at the bottom"))
    assert r.style.captions == style_mod.Captions()
    assert any("caption style" in d for d in r.dropped)


def test_post_text_requirements_do_not_block_style():
    r = style_mod.resolve(GTA, rules(caption_requirements="Include #gta and tag @creator"))
    assert r.style.captions.highlight == "#FFE000"


def test_fit_layout_never_zooms():
    r = style_mod.resolve(GTA, rules(), fit=True)
    assert not r.style.zoom.enabled and any("whole frame" in d for d in r.dropped)


def test_bad_book_style_values_are_rejected():
    import pytest
    with pytest.raises(Exception):
        style_mod.from_book({"style": {"zoom": {"scale": 9}}})


def test_book_load_prefers_niche_then_general_and_skips_drafts(tmp_path):
    (tmp_path / "general.json").write_text(json.dumps({"status": "active", "style": {}}))
    (tmp_path / "gta.json").write_text(json.dumps({"status": "draft", "style": {}}))
    assert book_mod.load("GTA", directory=tmp_path)["niche"] == "general"
    assert book_mod.load("GTA", directory=tmp_path, include_draft=True)["niche"] == "gta"
    book_mod.approve("gta", directory=tmp_path)
    assert book_mod.load("gta", directory=tmp_path)["niche"] == "gta"
    assert book_mod.load("minecraft", directory=tmp_path)["niche"] == "general"
    assert book_mod.load("x", directory=tmp_path / "nope") is None


def test_niche_for_trailer_override_category_and_fallback(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    camp = {"id": 7, "category": "Minecraft"}
    assert book_mod.niche_for(conn, camp, {"rubric_category": "x"}) == "minecraft"
    assert book_mod.niche_for(conn, camp, {"rubric_category": "official_trailer"}) == "trailer"
    db.kv_set(conn, "taste_niche_7", "GTA RP")
    assert book_mod.niche_for(conn, camp, {"rubric_category": "x"}) == "gtarp"
    assert book_mod.niche_for(conn, {"id": 8, "category": None}, None) == "general"


def test_shipped_books_are_valid_and_trailer_is_plain():
    for name in ("general", "gaming", "gta", "minecraft", "trailer"):
        b = book_mod.load(name)
        assert b and b["niche"] == name
        style_mod.from_book(b)
    assert not style_mod.from_book(book_mod.load("trailer")).zoom.enabled


def test_ass_default_output_unchanged_without_style():
    ev = [(1.0, 1.6, "hello there")]
    plain = subs.build_ass(hook="HI", handle=None, events=ev, duration=5, cam_height=0)
    assert "Style: Caption,Arial,66,&H00FFFFFF" in plain
    assert "Dialogue: 0,0:00:01.00,0:00:01.60,Caption,,0,0,0,,HELLO THERE" in plain


def test_ass_colour_converts_rgb_to_bgr():
    assert subs.ass_colour("#FFE000") == "&H0000E0FF"
    assert subs.ass_colour("nonsense") == "&H00FFFFFF"


def test_ass_highlight_marks_the_spoken_word_and_pops_the_first():
    st = style_mod.Style.model_validate({"captions": {"highlight": "#FFE000", "pop": True}})
    words = [(1.0, 1.3, "big"), (1.3, 1.7, "shit"), (2.0, 2.3, "wow")]
    ass = subs.build_ass(hook="", handle=None, events=subs.caption_events(words), duration=5,
                         cam_height=0, style=st, words=words)
    d = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0")]
    assert len(d) == 3
    assert "\\fscx118" in d[0] and "\\fscx118" not in d[1]
    assert d[0].count("&H0000E0FF") == 1 and "S**T" in d[1]
    assert "{\\c&H00FFFFFF}BIG" in d[1]


def test_ass_captions_and_hook_can_be_switched_off():
    st = style_mod.Style.model_validate({"captions": {"enabled": False}, "hook": {"enabled": False}})
    ass = subs.build_ass(hook="HI", handle=None, events=[(1, 2, "x")], duration=5, cam_height=0, style=st)
    assert "Dialogue" not in ass


def test_filter_graph_without_zoom_is_unchanged_and_zoom_only_touches_game():
    lay = layout.Layout("x", 1920, 1080, layout.Box(0, 700, 480, 380), True, None)
    base = layout.filter_graph(lay)
    assert "eval=frame" not in base and layout.filter_graph(lay, (), 1.2) == base
    zoomed = layout.filter_graph(lay, [(2.0, 0.7)], 1.15)
    cam_part, game_part = zoomed.split("[game]")[0].split("[cam];")
    assert "eval=frame" not in cam_part and "eval=frame" in game_part
    fit = layout.Layout("x", 1920, 1080, None, False, None, fit=True)
    assert layout.filter_graph(fit, [(2.0, 0.7)], 1.15) == layout.filter_graph(fit)


def test_zoom_plan_respects_edges_gaps_and_off(monkeypatch):
    from clipforge.moments.signals import Peak
    cfg = style_mod.Zoom(enabled=True, max_events=2, min_gap=4.0, seconds=0.7)

    def peak(t, z):
        return Peak(start=t, end=t + 0.25, value=0.0, z=z, signal="loudness")

    monkeypatch.setattr(zoom_mod.signals, "loudness_series", lambda *a, **k: object())
    monkeypatch.setattr(zoom_mod.signals, "peaks", lambda *a, **k: [
        peak(0.3, 9), peak(3.0, 5), peak(4.5, 4), peak(9.0, 3), peak(19.6, 8)])
    got = zoom_mod.plan("a.flac", 20.0, cfg)
    assert got == [(2.9, 0.7), (8.9, 0.7)]          # head/tail kept free, 4.5 too close to 3.0
    assert zoom_mod.plan("a.flac", 20.0, style_mod.Zoom(enabled=False)) == []
    assert zoom_mod.plan("a.flac", 2.0, cfg) == []
