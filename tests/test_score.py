"""Rubric arithmetic and moment selection, with a fake model.

The division of labour under test: the model says what is *in* a moment, this
code decides what that is *worth*. So re-weighting a rubric never needs a
prompt change, and a candidate that breaks the rules is dropped here rather
than trusted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.core import llm
from clipforge.core.config import get_settings
from clipforge.ingest import captions as caps
from clipforge.ingest.rules import BriefRules
from clipforge.moments import rubric as rubric_mod
from clipforge.moments import score as score_mod
from clipforge.moments.score import Candidate, CandidateList
from clipforge.moments.signals import Peak

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rubric():
    return rubric_mod.load(REPO / "rubrics" / "general.json")


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GROQ_API_KEY", "unused-by-the-fake")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    get_settings.cache_clear()
    s = get_settings()
    s.ensure_dirs()
    yield s
    get_settings.cache_clear()


def fake_caller(*answers):
    """Return each answer in turn; repeat the last one forever."""
    calls: list[dict] = []
    queue = list(answers)

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        calls.append({"user": user, "system": system, "model": model,
                      "output_model": output_model})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    call.calls = calls  # type: ignore[attr-defined]
    return call


def candidate(start, end, *, categories=(), penalties=(), confidence=1.0, hook="Hook"):
    return Candidate(start=start, end=end, description="d", hook=hook, title="t",
                     categories=list(categories), penalties=list(penalties),
                     confidence=confidence)


SEGMENTS = [caps.Segment(float(i), float(i + 5), f"line {i}") for i in range(0, 300, 5)]


# --- rubric -----------------------------------------------------------------

def test_a_perfect_moment_scores_100(rubric):
    assert rubric.score(list(rubric.category_ids), []) == 100.0


def test_penalties_cannot_push_below_zero(rubric):
    assert rubric.score(["surprise"], list(rubric.penalty_ids)) == 0.0


def test_duplicated_categories_are_counted_once(rubric):
    once = rubric.score(["surprise"], [])
    assert rubric.score(["surprise", "surprise", "surprise"], []) == once


def test_an_unknown_category_contributes_nothing(rubric):
    """A rubric gains and loses categories, and old moments stay comparable."""
    assert rubric.score(["invented_by_the_model"], []) == 0.0
    assert rubric.score(["surprise", "invented"], []) == rubric.score(["surprise"], [])


def test_weights_are_withheld_from_the_prompt(rubric):
    """Telling the model which category scores highest would bias it."""
    text = rubric.describe_for_prompt()
    assert "stakes_early" in text and "needs_context" in text
    assert "1.1" not in text and "weight" not in text


def test_a_heavier_category_scores_higher(rubric):
    assert rubric.score(["stakes_early"], []) > rubric.score(["quotable_line"], [])


def test_missing_rubric_file_is_an_error(tmp_path):
    with pytest.raises(rubric_mod.RubricError, match="no rubric"):
        rubric_mod.load(tmp_path / "nope.json")


def test_a_rubric_with_no_categories_is_refused(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"name": "empty", "categories": []}), encoding="utf-8")
    with pytest.raises(rubric_mod.RubricError, match="no categories"):
        rubric_mod.load(path)


def test_invalid_json_is_an_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(rubric_mod.RubricError, match="valid JSON"):
        rubric_mod.load(path)


def test_a_niche_rubric_falls_back_to_general():
    assert rubric_mod.for_campaign(REPO / "rubrics", "cooking").name == "general"


def test_the_gta_rubric_loads_and_every_weight_is_sane():
    got = rubric_mod.for_campaign(REPO / "rubrics", "GTA")
    assert got.name == "gta" and "police_chase" in got.category_ids
    assert all(c.weight > 0 for c in got.categories) and all(p.weight < 0 for p in got.penalties)


def test_no_category_falls_back_to_general():
    assert rubric_mod.for_campaign(REPO / "rubrics", None).name == "general"


def test_streamer_and_minecraft_have_their_own_rubric_now():
    assert rubric_mod.niche_of(REPO / "rubrics", "Streamer") == "streamer"
    assert rubric_mod.niche_of(REPO / "rubrics", "Minecraft") == "minecraft"
    assert rubric_mod.niche_of(REPO / "rubrics", "cooking") == "general"
    assert rubric_mod.for_campaign(REPO / "rubrics", "Streamer").name == "streamer"
    assert rubric_mod.for_campaign(REPO / "rubrics", "Minecraft").name == "minecraft"


# --- windows ----------------------------------------------------------------

def test_a_window_surrounds_its_peak():
    peak = Peak(start=100.0, end=105.0, value=1.0, z=2.0, signal="loudness_dbfs")
    assert score_mod.window_around(peak, before=25, after=25) == (75.0, 130.0)


def test_a_window_is_clamped_to_the_source():
    peak = Peak(start=5.0, end=8.0, value=1.0, z=2.0, signal="x")
    assert score_mod.window_around(peak, before=25, after=25, duration=20.0) == (0.0, 20.0)


def test_overlapping_windows_merge_so_transcript_is_sent_once():
    assert score_mod.merge_windows([(0, 60), (50, 120), (300, 360)]) == [(0, 120), (300, 360)]


def test_non_overlapping_windows_stay_separate():
    assert score_mod.merge_windows([(0, 30), (100, 130)]) == [(0, 30), (100, 130)]


def test_segments_between_takes_partial_overlaps():
    got = score_mod.segments_between(SEGMENTS, 7.0, 18.0)
    assert [s.start for s in got] == [5.0, 10.0, 15.0]


# --- length rules -----------------------------------------------------------

def test_the_brief_sets_the_length_window():
    low, high = score_mod._length_rule(BriefRules(clip_min_seconds=7, clip_max_seconds=90))
    assert (low, high) == (7.0, 90.0)


def test_defaults_when_the_brief_is_silent():
    assert score_mod._length_rule(BriefRules()) == (15.0, 60.0)
    assert score_mod._length_rule(None) == (15.0, 60.0)


def test_only_a_minimum_leaves_the_default_maximum():
    """The real SkiiMaskTimmy brief: minimum 7 seconds, no maximum stated."""
    assert score_mod._length_rule(BriefRules(clip_min_seconds=7)) == (7.0, 60.0)


def test_an_impossible_brief_is_an_error():
    with pytest.raises(ValueError, match="no clip length"):
        score_mod._length_rule(BriefRules(clip_min_seconds=90, clip_max_seconds=30))


# --- proposing --------------------------------------------------------------

PEAK = Peak(start=100.0, end=105.0, value=1.0, z=2.4, signal="loudness_dbfs")


def test_proposes_and_scores_a_moment(rubric, settings):
    call = fake_caller(CandidateList(moments=[
        candidate(90.0, 120.0, categories=["surprise", "big_reaction"]),
    ]))
    got = score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings,
                            duration=300.0)
    assert len(got) == 1
    assert got[0].score > 0
    assert got[0].rubric_category in ("surprise", "big_reaction")
    assert call.calls[0]["model"] == llm.model_for(settings, "scoring")


def test_the_peak_reason_is_sent_to_the_model(rubric, settings):
    call = fake_caller(CandidateList(moments=[]))
    score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings)
    sent = call.calls[0]["user"]
    assert "loudness_dbfs" in sent and "2.4 sd above normal" in sent
    assert "[01:40.00]" in sent        # the transcript window, with timestamps


def test_no_peaks_means_no_requests(rubric, settings):
    call = fake_caller(CandidateList(moments=[]))
    assert score_mod.propose(SEGMENTS, [], rubric, call=call, settings=settings) == []
    assert call.calls == []


def test_no_transcript_means_no_requests(rubric, settings):
    call = fake_caller(CandidateList(moments=[]))
    assert score_mod.propose([], [PEAK], rubric, call=call, settings=settings) == []
    assert call.calls == []


def test_confidence_scales_the_score(rubric, settings):
    sure = fake_caller(CandidateList(moments=[
        candidate(90.0, 120.0, categories=["surprise"], confidence=1.0)]))
    unsure = fake_caller(CandidateList(moments=[
        candidate(90.0, 120.0, categories=["surprise"], confidence=0.25)]))
    high = score_mod.propose(SEGMENTS, [PEAK], rubric, call=sure, settings=settings)
    low = score_mod.propose(SEGMENTS, [PEAK], rubric, call=unsure, settings=settings)
    assert high[0].score > low[0].score


def test_a_moment_outside_the_length_rule_is_dropped(rubric, settings):
    call = fake_caller(CandidateList(moments=[
        candidate(100.0, 103.0, categories=["surprise"]),      # 3 s, too short
        candidate(110.0, 125.0, categories=["surprise"]),      # 15 s, allowed
    ]))
    got = score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings)
    assert [s.length for s in got] == [15.0]


def test_a_backwards_moment_is_dropped(rubric, settings):
    call = fake_caller(CandidateList(moments=[candidate(120.0, 90.0)]))
    assert score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings) == []


def test_a_moment_past_the_end_of_the_source_is_dropped(rubric, settings):
    call = fake_caller(CandidateList(moments=[
        candidate(90.0, 130.0, categories=["surprise"])]))
    got = score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings,
                            duration=110.0)
    assert got == []


def test_a_moment_too_far_outside_its_window_is_dropped(rubric, settings):
    """The model may only point at the transcript it was actually shown.

    The window is 75-130, a span of 55 s. A pair ending at 200 is neither an
    absolute time inside the window nor a plausible offset into it, so it is
    dropped rather than relocated: better to lose a moment than to attach a
    hook to footage the model never saw.
    """
    call = fake_caller(CandidateList(moments=[
        candidate(5.0, 200.0, categories=["surprise"])]))
    assert score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings) == []


def test_an_offset_pair_inside_the_window_span_is_shifted(rubric, settings):
    """5-35 against a 75-130 window reads as an offset, and becomes 80-110."""
    call = fake_caller(CandidateList(moments=[
        candidate(5.0, 35.0, categories=["surprise"])]))
    got = score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings,
                            duration=300.0)
    assert [(s.start, s.end) for s in got] == [(80.0, 110.0)]


def test_one_bad_window_does_not_lose_the_source(rubric, settings):
    """A single failed request must not throw away every other moment."""
    peaks = [PEAK, Peak(start=250.0, end=255.0, value=1.0, z=2.0, signal="x")]
    calls = {"n": 0}

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm.LLMBadAnswer("the model dribbled")
        return CandidateList(moments=[candidate(240.0, 270.0, categories=["surprise"])])

    got = score_mod.propose(SEGMENTS, peaks, rubric, call=call, settings=settings,
                            duration=300.0)
    assert len(got) == 1 and got[0].start == 240.0


def test_a_wrong_answer_shape_is_an_error(rubric, settings):
    call = fake_caller(candidate(90.0, 120.0))        # not a CandidateList
    with pytest.raises(llm.LLMBadAnswer):
        score_mod.propose(SEGMENTS, [PEAK], rubric, call=call, settings=settings)


def test_banned_content_is_passed_to_the_model(rubric, settings):
    call = fake_caller(CandidateList(moments=[]))
    rules = BriefRules(banned_content=["spoilers past chapter 3"],
                       required_overlay="twitch.tv/skiimasktimmy")
    score_mod.propose(SEGMENTS, [PEAK], rubric, rules=rules, call=call, settings=settings)
    sent = call.calls[0]["user"]
    assert "spoilers past chapter 3" in sent
    assert "twitch.tv/skiimasktimmy" in sent


# --- dedupe -----------------------------------------------------------------

def scored(start, end, value):
    return score_mod.Scored(candidate=candidate(start, end), score=value,
                            rubric_category="surprise")


def test_the_weaker_of_two_overlapping_moments_is_dropped():
    got = score_mod.dedupe([scored(100, 130, 90.0), scored(105, 135, 50.0)])
    assert [s.score for s in got] == [90.0]


def test_distinct_moments_are_both_kept():
    got = score_mod.dedupe([scored(100, 130, 90.0), scored(200, 230, 50.0)])
    assert len(got) == 2


def test_dedupe_returns_time_order_not_rank():
    got = score_mod.dedupe([scored(200, 230, 90.0), scored(100, 130, 50.0)])
    assert [s.start for s in got] == [100, 200]


def test_a_small_overlap_is_tolerated():
    # 2 s shared out of 30 is 7%, under the 50% threshold.
    got = score_mod.dedupe([scored(100, 130, 90.0), scored(128, 158, 50.0)])
    assert len(got) == 2


def test_a_moment_entirely_inside_another_is_dropped():
    got = score_mod.dedupe([scored(100, 160, 90.0), scored(120, 140, 80.0)])
    assert [s.start for s in got] == [100]


# --- what the first real run exposed ----------------------------------------

def test_window_relative_times_are_repaired(rubric, settings):
    """gpt-oss-20b answered 10.2-17.4 for a window at 16885-16945 s.

    The moment was real; only its arithmetic was wrong.
    """
    far = Peak(start=16900.0, end=16905.0, value=1.0, z=2.0, signal="loudness_dbfs")
    segments = [caps.Segment(float(i), float(i + 5), f"line {i}")
                for i in range(16800, 17000, 5)]
    call = fake_caller(CandidateList(moments=[
        candidate(10.2, 30.2, categories=["surprise"]),
    ]))
    got = score_mod.propose(segments, [far], rubric, call=call, settings=settings,
                            duration=20794.0)
    assert len(got) == 1
    assert got[0].start == pytest.approx(16885.2)
    assert got[0].length == pytest.approx(20.0)


def test_absolute_times_are_left_alone(rubric, settings):
    far = Peak(start=16900.0, end=16905.0, value=1.0, z=2.0, signal="x")
    segments = [caps.Segment(float(i), float(i + 5), f"line {i}")
                for i in range(16800, 17000, 5)]
    call = fake_caller(CandidateList(moments=[
        candidate(16890.0, 16910.0, categories=["surprise"]),
    ]))
    got = score_mod.propose(segments, [far], rubric, call=call, settings=settings,
                            duration=20794.0)
    assert got[0].start == 16890.0


def test_a_nonsense_time_is_still_dropped(rubric, settings):
    """Repair must not become a way to accept anything."""
    far = Peak(start=16900.0, end=16905.0, value=1.0, z=2.0, signal="x")
    segments = [caps.Segment(float(i), float(i + 5), f"line {i}")
                for i in range(16800, 17000, 5)]
    # Relative reading would end far past the window, so neither reading fits.
    call = fake_caller(CandidateList(moments=[
        candidate(500.0, 530.0, categories=["surprise"]),
    ]))
    assert score_mod.propose(segments, [far], rubric, call=call,
                             settings=settings, duration=20794.0) == []


def test_a_refused_window_is_halved_rather_than_lost(rubric, settings):
    """A 160 s window of dense speech came to 10,700 tokens against 8,000."""
    segments = [caps.Segment(float(i), float(i + 2), "word " * 20)
                for i in range(0, 300, 2)]
    peak = Peak(start=100.0, end=105.0, value=1.0, z=2.0, signal="loudness_dbfs")
    state = {"refusals": 0}

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        if len(user) > 3000:
            state["refusals"] += 1
            raise llm.LLMRequestTooLarge("too big", limit=8000, requested=10700)
        return CandidateList(moments=[
            candidate(80.0, 100.0, categories=["surprise"])])

    got = score_mod.propose(segments, [peak], rubric, call=call, settings=settings,
                            duration=300.0)
    assert state["refusals"] > 0, "the test should have exercised a refusal"
    assert got, "halving should have salvaged the window"


def test_halving_gives_up_rather_than_looping(rubric, settings):
    segments = [caps.Segment(float(i), float(i + 2), "word " * 20)
                for i in range(0, 200, 2)]
    peak = Peak(start=100.0, end=105.0, value=1.0, z=2.0, signal="x")
    calls = {"n": 0}

    def call(output_model, system, user, **kw):
        calls["n"] += 1
        raise llm.LLMRequestTooLarge("too big", limit=8000, requested=99999)

    assert score_mod.propose(segments, [peak], rubric, call=call, settings=settings,
                             duration=200.0) == []
    assert calls["n"] <= 2 ** (score_mod.MAX_SPLITS + 2)


def test_a_blank_title_falls_back_to_the_description(rubric):
    blank = Candidate(start=100.0, end=125.0, description="She buys the thing by accident",
                      hook="h", title="  ", categories=["surprise"], confidence=1.0)
    scored = score_mod._accept(blank, rubric, 7.0, 60.0, (90.0, 150.0), 3600.0)
    assert scored is not None
    assert scored.candidate.title == "She buys the thing by accident"
