"""Phase 3 modules that need no network and no real ffmpeg."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from clipforge.edit import compliance, cut, hls, layout, meta, package, render, subs
from clipforge.ingest.captions import Segment
from clipforge.ingest.rules import BriefRules

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
0.mp4
#EXTINF:10.000,
1.mp4
#EXTINF:10.000,
2.mp4
#EXTINF:5.500,
3.mp4
#EXT-X-ENDLIST
"""


# ---- hls ----------------------------------------------------------------

def test_playlist_parse_and_duration():
    p = hls.parse_playlist(PLAYLIST, "https://x/")
    assert p.init == "init-0.mp4"
    assert [u for u, _ in p.segments] == ["0.mp4", "1.mp4", "2.mp4", "3.mp4"]
    assert p.duration == pytest.approx(35.5)


def test_playlist_without_segments_is_an_error():
    with pytest.raises(hls.SectionError):
        hls.parse_playlist("#EXTM3U\n", "https://x/")


def test_cover_returns_whole_segments_and_the_offset():
    p = hls.parse_playlist(PLAYLIST, "https://x/")
    assert hls.cover(p, 12.0, 24.0) == (1, 2, 10.0)
    assert hls.cover(p, 0.0, 5.0) == (0, 0, 0.0)
    assert hls.cover(p, 29.0, 34.0) == (2, 3, 20.0)


def test_cover_past_the_end_or_backwards_is_refused():
    p = hls.parse_playlist(PLAYLIST, "https://x/")
    with pytest.raises(hls.SectionError):
        hls.cover(p, 99.0, 120.0)
    with pytest.raises(hls.SectionError):
        hls.cover(p, 10.0, 5.0)


# ---- layout -------------------------------------------------------------

def test_layout_loads_the_shipped_creator_file():
    lay = layout.load("SkiiMaskTimmy")
    assert lay.facecam is not None and lay.facecam_required
    assert lay.handle == "twitch.tv/skiimasktimmy"
    assert lay.cam_height % 2 == 0 and 0.2 < lay.cam_share < 0.45


def test_unknown_creator_gets_a_plain_centre_crop(tmp_path):
    lay = layout.load("nobody", directory=tmp_path)
    assert lay.facecam is None and lay.cam_height == 0
    assert "vstack" not in layout.filter_graph(lay)


def test_stacked_graph_fills_the_frame_exactly():
    graph = layout.filter_graph(layout.load("skiimasktimmy"))
    lay = layout.load("skiimasktimmy")
    assert f"scale=1080:{lay.cam_height}" in graph
    assert f"crop=1080:{1920 - lay.cam_height}" in graph
    assert "vstack" in graph


# ---- captions ------------------------------------------------------------

def test_mask_hides_profanity_but_keeps_ordinary_words():
    assert subs.mask("What the fuck, shit happens") == "What the f**k, s**t happens"
    assert subs.mask("Scunthorpe classic") == "Scunthorpe classic"
    assert subs.has_profanity("oh SHIT") and not subs.has_profanity("hello")


def test_words_are_spread_across_their_phrase_and_made_relative():
    segs = [Segment(10.0, 14.0, "one two three four")]
    words = subs.words_in_window(segs, 10.0, 20.0)
    assert [w[2] for w in words] == ["one", "two", "three", "four"]
    assert words[0][0] == pytest.approx(0.0)
    assert words[2][0] == pytest.approx(2.0)


def test_words_outside_the_window_are_left_out():
    segs = [Segment(0.0, 4.0, "a b c d"), Segment(50.0, 52.0, "x y")]
    assert subs.words_in_window(segs, 10.0, 20.0) == []


def test_ass_has_hook_handle_and_masked_captions():
    ass = subs.build_ass(hook="What the fuck?!", handle="twitch.tv/x",
                         events=[(0.0, 1.0, "oh shit")], duration=10.0, cam_height=594)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "WHAT THE F**K?!" in ass
    assert "S**T" in ass and "twitch.tv/x" in ass
    assert "fuck" not in ass.lower() and "shit" not in ass.lower()


def test_ass_without_a_hook_has_no_hook_line():
    ass = subs.build_ass(hook="", handle=None, events=[], duration=5.0, cam_height=0)
    assert "Dialogue: 1" not in ass


# ---- cut points ------------------------------------------------------------

SEGS = [Segment(98.0, 102.0, "a"), Segment(102.0, 109.0, "b"), Segment(109.0, 130.0, "c")]


def test_snap_moves_the_edges_onto_phrase_boundaries():
    start, end = cut.snap(SEGS, 100.5, 109.8)
    assert start == pytest.approx(102.0 - cut.PRE_ROLL, abs=0.01)
    assert end == pytest.approx(109.0 + cut.TAIL, abs=0.01)


def test_snap_never_breaks_the_minimum_length():
    start, end = cut.snap(SEGS, 100.5, 109.8, min_seconds=9.0)
    assert (start, end) == (100.5, 109.8)


def test_snap_caps_the_length():
    assert cut.snap([], 0.0, 100.0)[1] == 59.0


# ---- meta ------------------------------------------------------------------

def test_title_is_capped_masked_and_cut_on_a_word():
    t = meta.clip_title("What the fuck " + "word " * 30)
    assert len(t) <= meta.TITLE_MAX and t.endswith("…") and "fuck" not in t


def test_required_fields_are_added_in_code():
    rules = BriefRules(required_hashtags=["#SkiiMaskTimmy"], required_mentions=["clippedin"],
                       disclosure_text="#ad")
    got = meta.finish(meta.Draft(title="A title", description="Something happened.",
                                 hashtags=["#gta", "gta", "chase", "x", "y"]),
                      rules=rules, handle="twitch.tv/skiimasktimmy")
    assert got["hashtags"][0] == "#skiimasktimmy"
    assert len(got["hashtags"]) == 1 + meta.EXTRA_TAGS
    assert "twitch.tv/skiimasktimmy" in got["description"]
    assert "@clippedin" in got["description"] and "#ad" in got["description"]


def test_handle_comes_from_the_layout_then_the_brief():
    rules = BriefRules(caption_requirements="Caption must include “twitch.tv/skiimasktimmy”")
    assert meta.handle_from(rules, "twitch.tv/other") == "twitch.tv/other"
    assert meta.handle_from(rules, None) == "twitch.tv/skiimasktimmy"
    assert meta.handle_from(None, None) is None


def test_draft_falls_back_when_the_model_is_unavailable():
    moment = {"title": "T", "hook": "H", "description": "D"}
    d = meta.draft(moment, "words", settings=__import__("clipforge.core.config",
                   fromlist=["get_settings"]).get_settings())
    assert d.title == "T" and d.description == "D"


# ---- compliance -------------------------------------------------------------

def _check(rules, **over):
    args = dict(rules=rules, layout=layout.load("skiimasktimmy"),
                meta={"title": "t", "description": "Watch live: twitch.tv/skiimasktimmy",
                      "handle": "twitch.tv/skiimasktimmy"},
                duration=28.0, width=1080, height=1920, has_audio=True, overlay_burned=True,
                hook="a hook", spoken="plain speech")
    args.update(over)
    return compliance.check(**args)


def codes(findings, level):
    return {f.code for f in findings if f.level == level}


def test_a_good_clip_has_no_failures():
    rules = BriefRules(clip_min_seconds=7, required_overlay="the handle",
                       caption_requirements="include twitch.tv/skiimasktimmy")
    ok, _ = compliance.summary(_check(rules))
    assert ok


def test_too_short_wrong_size_and_silent_all_fail():
    f = _check(BriefRules(clip_min_seconds=7), duration=5.0, width=720, height=1280,
               has_audio=False)
    assert {"length", "size", "audio"} <= codes(f, "FAIL")


def test_missing_required_text_fails():
    rules = BriefRules(required_hashtags=["#abc"], required_mentions=["@who"],
                       disclosure_text="#ad")
    assert {"hashtag", "mention", "disclosure"} <= codes(_check(rules), "FAIL")


def test_overlay_and_facecam_requirements_fail_when_unmet():
    rules = BriefRules(required_overlay="handle")
    assert "overlay" in codes(_check(rules, overlay_burned=False), "FAIL")
    bare = layout.Layout("x", 1920, 1080, None, True, None)
    assert "facecam" in codes(_check(None, layout=bare), "FAIL")


def test_profanity_warns_but_does_not_hold():
    f = _check(None, hook="what the fuck", spoken="shit")
    assert {"hook_profanity", "spoken_profanity"} <= codes(f, "WARN")
    assert compliance.summary(f)[0]


def test_over_a_minute_fails_unless_the_brief_allows_it():
    assert "length" in codes(_check(BriefRules(), duration=75.0), "FAIL")
    assert "length" not in codes(_check(BriefRules(clip_max_seconds=90), duration=75.0), "FAIL")


# ---- render and package -------------------------------------------------------

def test_render_command_burns_subtitles_and_maps_both_streams(tmp_path):
    args = render.build_command(tmp_path / "in.mp4", ss=3.0, duration=20.0,
                                layout=layout.load("skiimasktimmy"), ass_name="subs.ass",
                                dest=tmp_path / "out.mp4")
    graph = args[args.index("-filter_complex") + 1]
    assert "ass=subs.ass[vout]" in graph and "vstack" in graph
    assert "[vout]" in args and "0:a:0?" in args
    assert args[args.index("-r") + 1] == "30" and "libx264" in args


def test_package_writes_meta_and_a_zip_of_the_three_files(tmp_path):
    d = package.folder(tmp_path, "My Campaign ($2,500)", 7)
    assert d == tmp_path / "my-campaign-2-500" / "7"
    (d / "clip.mp4").write_bytes(b"video")
    (d / "thumb.jpg").write_bytes(b"jpg")
    z = package.write(d, clip=d / "clip.mp4", thumb=d / "thumb.jpg", meta={"title": "t’s"})
    assert sorted(zipfile.ZipFile(z).namelist()) == ["clip.mp4", "meta.json", "thumb.jpg"]
    assert json.loads((d / "meta.json").read_text(encoding="utf-8"))["title"] == "t’s"


# ---- word timing -------------------------------------------------------------

def test_parse_words_makes_tuples_shows_a_touch_early_and_drops_junk():
    from clipforge.edit import words
    data = {"words": [{"word": " Hello", "start": 1.0, "end": 1.4},
                      {"word": "", "start": 2.0, "end": 2.2},
                      {"word": "bad"},
                      {"word": "late", "start": 31.0, "end": 31.5}]}
    got = words.parse_words(data, duration=30.0)
    assert got == [(pytest.approx(1.0 - words.LEAD), 1.4, "Hello")]


def test_parse_words_gives_zero_length_words_a_visible_duration():
    from clipforge.edit import words
    (s, e, _), = words.parse_words({"words": [{"word": "x", "start": 0.0, "end": 0.0}]}, 10.0)
    assert s == 0.0 and e >= 0.05


def test_captions_show_two_words_starting_on_the_first_word():
    ws = [(0.3, 0.5, "this"), (0.5, 0.7, "could"), (0.7, 0.8, "be"), (0.8, 1.0, "them"),
          (5.0, 5.2, "oh")]
    ev = subs.caption_events(ws)
    assert [e[2] for e in ev] == ["this could", "be them", "oh"]
    assert ev[0][0] == pytest.approx(0.3) and ev[0][1] == pytest.approx(0.7)
    assert ev[2][0] == pytest.approx(5.0) and ev[2][1] == pytest.approx(5.5)


def test_nearly_touching_captions_run_into_each_other():
    ws = [(0.0, 0.5, "a"), (0.5, 1.0, "b"), (1.1, 1.3, "c"), (1.3, 1.6, "d")]
    ev = subs.caption_events(ws)
    assert ev[0][1] == pytest.approx(ev[1][0])


# ---- rubric derivation ------------------------------------------------------

def test_iso_durations_parse():
    from clipforge.moments import rubric_build as rbd
    assert rbd.parse_duration("PT45S") == 45 and rbd.parse_duration("PT1M5S") == 65
    assert rbd.parse_duration("garbage") == 0.0


def test_weights_follow_median_views_and_skip_thin_categories(tmp_path):
    from clipforge.moments import rubric as rb, rubric_build as rbd
    C = rb.Category
    cur = rb.Rubric("gta", 1, (C("police_chase", 1.0, ""), C("physics_glitch", 0.8, ""),
                               C("heist_fail", 1.0, ""), C("setup_payoff", 1.0, ""),
                               C("stakes_early", 1.1, "")), (C("slow_start", -0.8, ""),))
    shorts = [{"title": str(i), "views": v} for i, v in
              enumerate([1000] * 5 + [500] * 5 + [9999] * 2)]
    cl = rbd.Clusters(assignments=(
        [rbd.Assignment(i=i, category="police_chase") for i in range(5)]
        + [rbd.Assignment(i=i, category="physics_glitch") for i in range(5, 10)]
        + [rbd.Assignment(i=i, category="heist_fail") for i in range(10, 12)]
        + [rbd.Assignment(i=0, category="setup_payoff")]))
    w = rbd.weights(shorts, cl, cur)
    assert w["police_chase"]["weight"] == pytest.approx(1.2)
    assert w["physics_glitch"]["weight"] == pytest.approx(0.9)
    assert "heist_fail" not in w and "setup_payoff" not in w
    out = rbd.assemble(cur, w, cl, sample=12, today="1 Jan 2027")
    ids = {c["id"]: c["weight"] for c in out["categories"]}
    assert ids["police_chase"] == 1.2 and ids["heist_fail"] == 1.0 and ids["stakes_early"] == 1.1
    assert out["version"] == cur.version + 1 and len(out["penalties"]) == 1


def test_official_trailers_are_shown_whole_over_a_blurred_copy(tmp_path):
    from clipforge.edit import layout

    trailer = layout.for_moment("someone", {"rubric_category": "official_trailer"})
    graph = layout.filter_graph(trailer)
    assert trailer.fit and "boxblur" in graph and "overlay" in graph
    assert "crop=1080:1920,setsar" not in graph        # the whole frame is kept, not centre-cropped
    plain = layout.for_moment("someone", {"rubric_category": "comedy"})
    assert not plain.fit and "boxblur" not in layout.filter_graph(plain)
    cam = layout.for_moment("skiimasktimmy", {"rubric_category": "official_trailer"})
    assert cam.facecam is None and cam.fit


def test_footage_with_subtitles_in_its_name_is_not_captioned_twice():
    from clipforge.edit import subs

    assert subs.has_burned_subs("D:/x/FM27_MATTY_ENGLISH US_30s_16x9_SUBS (1).mp4")
    assert not subs.has_burned_subs("D:/x/Vhi-1-XWUuA.mp4")
    assert not subs.has_burned_subs("D:/x/subsonic_stream.mp4")


def test_a_campaign_logo_is_overlaid_and_counts_as_the_required_overlay(tmp_path, monkeypatch):
    from dataclasses import replace
    from pathlib import Path
    from clipforge.edit import layout, render

    logo = tmp_path / "logo.png"
    logo.write_bytes(b"x")
    lay = replace(layout.load("nobody"), logo=str(logo), fit=True)
    cmd = render.build_command(Path("a.mp4"), ss=0, duration=10, layout=lay,
                               ass_name="subs.ass", dest=Path("o.mp4"))
    assert cmd.count("-i") == 2 and str(logo) in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[1:v]scale=" in graph and graph.endswith("[vout]")
    plain = render.build_command(Path("a.mp4"), ss=0, duration=10, layout=layout.load("nobody"),
                                 ass_name="subs.ass", dest=Path("o.mp4"))
    assert plain.count("-i") == 1
    assert layout.logo_for(None) is None


def _clip(tmp_path, colour):
    from clipforge.core import media
    out = tmp_path / f"{colour}.mp4"
    media.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", f"color=c={colour}:s=1080x1920:d=2:r=10", "-pix_fmt", "yuv420p", str(out)])
    return out


def test_blank_cam_band_is_detected(tmp_path):
    lay = layout.load("skiimasktimmy")
    assert lay.facecam is not None
    assert render.cam_band_blank(_clip(tmp_path, "black"), lay) is True
    assert render.cam_band_blank(_clip(tmp_path, "white"), lay) is False
    assert render.cam_band_blank(_clip(tmp_path, "black"), layout.load("nobody")) is False


def test_blank_cam_fails_compliance():
    lay = layout.load("skiimasktimmy")
    kw = dict(rules=None, layout=lay, meta={}, duration=30, width=1080, height=1920,
              has_audio=True, overlay_burned=True, hook="", spoken="")
    assert not any(f.code == "facecam" for f in compliance.check(**kw))
    assert any(f.code == "facecam" and f.level == "FAIL" for f in compliance.check(**kw, cam_blank=True))


def test_caption_events_never_overlap_when_word_times_touch():
    from clipforge.edit import subs

    # the transcriber pads each word, so neighbours overlap by 50 ms
    words = [(0.27, 0.56, "I'm"), (0.51, 0.64, "gonna"), (0.59, 0.76, "go"), (0.71, 0.92, "rob"),
             (0.87, 1.34, "someone."), (1.30, 1.60, "now")]
    for events in (subs.highlight_events(words, 2, "&H00FFFFFF", "&H0000E0FF"),
                   subs.caption_events(words)):
        assert len(events) >= 3
        for a, b in zip(events, events[1:]):
            assert b[0] >= a[0] + subs.MIN_STEP - 1e-9
            assert a[1] <= b[0] + 1e-9
            assert a[1] > a[0]


def test_no_overlap_keeps_order_and_text():
    from clipforge.edit import subs

    out = subs.no_overlap([(1.0, 2.0, "b"), (0.0, 1.5, "a"), (1.01, 1.4, "c")])
    assert [e[2] for e in out] == ["a", "b", "c"]
    assert out[0][1] <= out[1][0] and out[1][1] <= out[2][0]


def test_draft_tells_the_model_who_the_streamer_is_and_what_the_brief_says(monkeypatch):
    sent = {}

    def fake(schema, system, user, **kw):
        sent.update(system=system, user=user)
        return meta.Draft(title="Timmy loses it", description="d")

    monkeypatch.setattr(meta.llm, "parse", fake)
    rules = BriefRules(caption_requirements="Caption must include the handle")
    d = meta.draft({"title": "T", "hook": "H", "description": "D"}, "words",
                   settings=__import__("clipforge.core.config", fromlist=["get_settings"]).get_settings(),
                   creator="skiimasktimmy", rules=rules)
    assert d.title == "Timmy loses it"
    assert "Streamer: skiimasktimmy" in sent["user"] and "Caption must include the handle" in sent["user"]
    assert "teammate" in sent["system"] and "brief" in sent["system"]


def test_the_briefs_required_caption_always_leads_the_post_text_and_is_checked():
    rules = BriefRules(required_caption="Jake Paul On YouTube")
    got = meta.finish(meta.Draft(title="Why your AI needs context", description="He explains it."),
                      rules=rules, handle=None)
    assert got["description"].splitlines()[0] == "Jake Paul On YouTube"
    same = meta.finish(meta.Draft(title="Jake Paul On YouTube", description="He explains it."),
                       rules=rules, handle=None)
    assert same["description"].count("Jake Paul On YouTube") == 0          # the title already carries it
    assert "Jake Paul On YouTube" not in meta.finish(
        meta.Draft(title="T", description="d"), rules=BriefRules(), handle=None)["description"]
    assert "caption" not in codes(_check(rules, meta=got), "FAIL")
    assert "caption" in codes(_check(rules, meta={"title": "T", "description": "d"}), "FAIL")


def test_a_required_name_is_added_when_the_caption_has_none_and_checked():
    rules = BriefRules(required_names_any=["Sypher", "Maddynf", "Replays"])
    plain = meta.finish(meta.Draft(title="Insane build battle", description="A wild round."),
                        rules=rules, handle=None)
    assert plain["description"].splitlines()[0] == "Featuring Sypher"
    named = meta.finish(meta.Draft(title="Maddynf wins the round", description="Wild."),
                        rules=rules, handle=None)
    assert "Featuring" not in named["description"]
    assert "caption" not in codes(_check(rules, meta=plain), "FAIL")
    assert "caption" in codes(_check(rules, meta={"title": "t", "description": "d"}), "FAIL")


def test_a_stream_clip_credits_the_channel_in_the_text_only():
    credit = meta.stream_credit("https://twitch.tv/videos/2929917089", "Replays")
    assert credit == "twitch.tv/replays"
    got = meta.finish(meta.Draft(title="Replays wins", description="Wild.", hashtags=["#uefn"]),
                      rules=None, handle=None, credit=credit)
    assert "Full stream: twitch.tv/replays" in got["description"] and got["handle"] is None
    assert got["description"].splitlines()[-1] == "#uefn"                    # tags stay last
    assert meta.stream_credit("https://youtube.com/watch?v=x", "Replays") is None
    assert meta.stream_credit("https://twitch.tv/videos/1", "名前") is None    # not a login
    with_handle = meta.finish(meta.Draft(title="t", description="d"), rules=None,
                              handle="kick.com/x", credit="twitch.tv/y")
    assert "Full stream" not in with_handle["description"]


def test_swear_words_are_starred_like_s_star_star_t_and_innocent_words_are_kept():
    from clipforge.edit.subs import has_profanity, mask

    assert mask("SHIT") == "S**T" and mask("fuck") == "f**k"
    assert mask("bullshit move") == "b******t move" and mask("fuckin hell") == "f****n hell"
    assert mask("Dickens in the cockpit") == "Dickens in the cockpit"
    assert mask("what a dick") == "what a d**k" and mask("ok bro") == "ok bro"
    assert has_profanity("shitting") and not has_profanity("assist the cockpit")


def test_an_edited_video_is_shown_whole_and_the_hook_never_sits_on_a_face():
    from clipforge.edit import layout as lay_mod

    m = {"rubric_category": "insight"}
    assert lay_mod.for_moment("tjr", m, None, produced=True).fit
    assert not lay_mod.for_moment("tjr", m, None).fit
    assert lay_mod.is_stream("https://www.twitch.tv/videos/1") and not lay_mod.is_stream("https://youtube.com/watch?v=x")
    assert subs.hook_y(400, False) == subs.OUT_H // 2 + 60          # on the seam under a facecam band
    assert subs.hook_y(0, True) < (subs.OUT_H - subs.OUT_W * 9 // 16) // 2   # inside the empty top band
    assert subs.hook_y(0, False) < subs.OUT_H // 4                  # well above a centred face
    ass = subs.build_ass(hook="Hi", handle=None, events=[], duration=3, cam_height=0, fit=True)
    assert f"\pos({subs.OUT_W // 2},{subs.hook_y(0, True)})" in ass


def test_full_gameplay_puts_the_whole_game_under_the_facecam_and_text_below_it():
    from dataclasses import replace

    from clipforge.edit import layout as lay_mod

    lay = replace(lay_mod.for_moment("replays", {"rubric_category": "x"}, None, game_full=True),
                  facecam=lay_mod.Box(8, 150, 194, 106))
    graph = lay_mod.filter_graph(lay)
    assert f"scale={subs.OUT_W}:{lay_mod.GAME_FULL_H}" in graph and "crop=194:106:8:150" in graph
    assert f"overlay=0:{lay.cam_height}" in graph
    assert lay.cam_height <= lay_mod.MAX_FULL_CAM and lay.game_bottom == lay.cam_height + lay_mod.GAME_FULL_H
    assert subs.hook_y(lay.cam_height, False, lay.game_bottom) > lay.game_bottom      # never on the game
    no_cam = lay_mod.for_moment("nobody", {"rubric_category": "x"}, None, game_full=True)
    assert "boxblur" in lay_mod.filter_graph(no_cam)                                  # whole picture, blurred bands
    assert not lay_mod.for_moment("replays", {"rubric_category": "x"}, None).game_full


def test_a_banner_title_stays_for_the_whole_clip_on_a_box_in_its_own_font():
    from clipforge.taste.style import Style

    st = Style.model_validate({"hook": {"banner": True, "box": True, "font": "Impact"}})
    ass = subs.build_ass(hook="Big title", handle=None, events=[], duration=12.0, cam_height=0, style=st)
    assert "Style: Hook,Impact," in ass and ",3,14,0,5," in ass
    assert "Dialogue: 1,0:00:00.00,0:00:12.00,Hook" in ass
    plain = subs.build_ass(hook="Big title", handle=None, events=[], duration=12.0, cam_height=0)
    assert "Dialogue: 1,0:00:00.00,0:00:01.80,Hook" in plain and "Style: Hook,Arial," in plain
