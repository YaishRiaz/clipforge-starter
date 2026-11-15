"""Brief parsing and tagging, with a fake model in place of the API."""
from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.core import db, llm
from clipforge.core.config import get_settings
from clipforge.ingest import briefs, tagger
from clipforge.ingest import captions as caps
from clipforge.ingest.rules import BriefRules
from clipforge.ingest.tagger import Chapter, SourceTags

BRIEF = """Clip our GTA 6 launch stream.
Use only the VODs linked here: https://youtu.be/dQw4w9WgXcQ
Clips must be 20-60 seconds. Tag #GTA6 and mention @studio.
Include the line "Paid partnership with Studio" in the description.
No spoilers past the prologue. Do not add your own music.
Submit a draft for approval before posting.
$1.50 per 1000 views, capped at $100 per clip.
"""


@pytest.fixture()
def conn(tmp_path: Path):
    c = db.open_db(tmp_path / "data" / "clipforge.db")
    yield c
    c.close()


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used-by-the-fake")
    s = get_settings()
    s.ensure_dirs()
    yield s
    get_settings.cache_clear()


def fake_caller(answer):
    """Stand in for llm.parse, recording what it was asked."""
    calls: list[dict] = []

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        calls.append({"model": model, "system": system, "user": user,
                      "output_model": output_model})
        return answer

    call.calls = calls  # type: ignore[attr-defined]
    return call


def test_parse_brief_text_sends_the_brief_and_returns_rules(settings):
    expected = BriefRules(
        approved_source_urls=["https://youtu.be/dQw4w9WgXcQ"],
        clip_min_seconds=20, clip_max_seconds=60,
        required_hashtags=["#GTA6"], required_mentions=["@studio"],
        disclosure_text="Paid partnership with Studio",
        banned_content=["spoilers past the prologue"],
        banned_edits=["added music"],
        pre_approval=True, rate_per_1k=1.5, clip_cap_usd=100.0,
    )
    call = fake_caller(expected)
    rules = briefs.parse_brief_text(BRIEF, campaign_name="GTA 6 launch", call=call,
                                   settings=settings)
    assert rules.pre_approval is True
    assert rules.approves_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    sent = call.calls[0]
    assert "GTA 6 launch" in sent["user"] and "Use only the VODs" in sent["user"]
    assert sent["model"] == llm.model_for(settings, "brief")
    assert sent["output_model"] is BriefRules


def test_empty_brief_is_refused(settings):
    with pytest.raises(briefs.BriefUnavailable):
        briefs.parse_brief_text("   ", call=fake_caller(BriefRules()), settings=settings)


def test_a_wrong_answer_shape_is_an_error(settings):
    with pytest.raises(llm.LLMBadAnswer):
        briefs.parse_brief_text(BRIEF, call=fake_caller(SourceTags(summary="x")),
                                settings=settings)


def test_missing_brief_text_says_what_to_do(conn):
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, brief_url, status) "
        "VALUES ('whop','e1','Needs brief','https://example.com/brief','active')"
    )
    with pytest.raises(briefs.BriefUnavailable, match="paste it in"):
        briefs.brief_text_for(conn, int(cur.lastrowid))


def test_rules_survive_a_save_and_load(conn):
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status) "
        "VALUES ('whop','e2','Round trip','active')"
    )
    campaign_id = int(cur.lastrowid)
    original = BriefRules(
        approved_source_urls=["https://youtu.be/dQw4w9WgXcQ"],
        disclosure_text="Paid partnership with Studio",
        pre_approval=True, uncertain=["clip length stated as 'short' with no number"],
    )
    briefs.save_rules(conn, campaign_id, original)
    loaded = briefs.load_rules(conn, campaign_id)
    assert loaded == original


def test_unparsed_brief_loads_as_none(conn):
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status) "
        "VALUES ('whop','e3','Unparsed','active')"
    )
    assert briefs.load_rules(conn, int(cur.lastrowid)) is None


def test_own_campaign_always_has_rules(conn):
    from clipforge.ingest import sources

    rules = briefs.load_rules(conn, sources.own_campaign_id(conn))
    assert rules is not None and rules.approved_source_urls == []


def test_unreadable_rules_json_does_not_crash(conn):
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, rules_json) "
        "VALUES ('whop','e4','Broken','active','{not json')"
    )
    assert briefs.load_rules(conn, int(cur.lastrowid)) is None


def test_no_api_key_is_a_clear_hold_not_a_crash(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    settings = get_settings()
    with pytest.raises(llm.LLMUnavailable, match="ANTHROPIC_API_KEY"):
        llm.parse(BriefRules, "s", "u", settings=settings)
    get_settings.cache_clear()


# --- tagging ---------------------------------------------------------------

SEGMENTS = [
    caps.Segment(0.0, 4.0, "We're starting the heist now."),
    caps.Segment(4.0, 9.0, "The cops showed up almost immediately."),
    caps.Segment(9.0, 14.0, "And that is how the whole thing fell apart."),
]


def test_describe_asks_with_timestamps_and_sorts_chapters(settings):
    answer = SourceTags(
        summary="A heist goes wrong.",
        chapters=[Chapter(start=9.0, title="Fallout"), Chapter(start=0.0, title="Setup")],
        language="en",
    )
    call = fake_caller(answer)
    tags = tagger.describe(SEGMENTS, title="Heist", duration=14.0, call=call,
                           settings=settings)
    assert [c.start for c in tags.chapters] == [0.0, 9.0]
    assert "[00:00.00]" in call.calls[0]["user"]
    assert "Heist" in call.calls[0]["user"]


def test_describe_drops_chapters_past_the_end(settings):
    answer = SourceTags(
        summary="s",
        chapters=[Chapter(start=5.0, title="real"), Chapter(start=9999.0, title="invented")],
    )
    tags = tagger.describe(SEGMENTS, duration=14.0, call=fake_caller(answer),
                           settings=settings)
    assert [c.title for c in tags.chapters] == ["real"]


def test_describe_needs_a_transcript(settings):
    with pytest.raises(ValueError):
        tagger.describe([], call=fake_caller(SourceTags(summary="s")), settings=settings)


def by_type(**answers):
    """A fake caller that answers according to the schema it was asked for."""
    calls: list[dict] = []

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        calls.append({"model": model, "system": system, "user": user,
                      "output_model": output_model})
        return answers[output_model.__name__]

    call.calls = calls  # type: ignore[attr-defined]
    return call


def test_a_long_transcript_is_summarised_in_windows_not_truncated(settings):
    """Truncating would describe the opening and call it the whole stream.

    Groq's free tier allows 8,000 tokens a minute, and the real 5h45 VOD renders
    to about 300,000 characters, so one request was never an option.
    """
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(4000)]
    call = by_type(
        SourceTags=SourceTags(summary="part summary",
                              chapters=[Chapter(start=10.0, title="A bit")]),
        Overall=tagger.Overall(summary="The whole stream.", language="en"),
    )
    tags = tagger.describe(many, duration=4000.0, call=call, settings=settings)

    assert tags.summary == "The whole stream."
    assert tags.language == "en"
    # Many window requests plus one reduce, and every one inside the budget.
    assert len(call.calls) > 5
    assert all(len(c["user"]) < settings.max_transcript_chars + 3000
               for c in call.calls)
    assert call.calls[-1]["output_model"] is tagger.Overall
    assert "Summaries of each part" in call.calls[-1]["user"]
    assert "part 1 of" in call.calls[0]["user"]


def test_window_summaries_use_the_cheaper_model(settings):
    """One call per window: the map step is extraction, the reduce is judgement."""
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(4000)]
    call = by_type(
        SourceTags=SourceTags(summary="part"),
        Overall=tagger.Overall(summary="whole"),
    )
    tagger.describe(many, duration=4000.0, call=call, settings=settings)
    assert call.calls[0]["model"] == llm.model_for(settings, "scoring")
    assert call.calls[-1]["model"] == llm.model_for(settings, "brief")


def test_a_short_transcript_is_still_one_request(settings):
    call = by_type(SourceTags=SourceTags(summary="short one", chapters=[]))
    tags = tagger.describe(SEGMENTS, duration=14.0, call=call, settings=settings)
    assert tags.summary == "short one"
    assert len(call.calls) == 1
    assert call.calls[0]["model"] == llm.model_for(settings, "brief")


def test_one_unreadable_window_does_not_lose_the_whole_record(settings):
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(2000)]
    state = {"n": 0}

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        if output_model is tagger.Overall:
            return tagger.Overall(summary="survived", language="en")
        state["n"] += 1
        if state["n"] == 2:
            raise llm.LLMBadAnswer("that window came back as soup")
        return SourceTags(summary=f"part {state['n']}")

    tags = tagger.describe(many, duration=2000.0, call=call, settings=settings)
    assert tags.summary == "survived"


def test_every_window_failing_is_an_error(settings):
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(2000)]

    def call(output_model, system, user, **kw):
        raise llm.LLMBadAnswer("no")

    with pytest.raises(llm.LLMBadAnswer, match="no part of the transcript"):
        tagger.describe(many, duration=2000.0, call=call, settings=settings)


def test_chapters_are_thinned_not_piled_up(settings):
    chapters = [Chapter(start=float(i * 10), title=f"c{i}") for i in range(100)]
    kept = tagger.thin_chapters(chapters, duration=1000.0)
    assert len(kept) <= tagger.MAX_CHAPTERS
    assert [c.start for c in kept] == sorted(c.start for c in kept)


def test_thinning_leaves_a_short_list_alone():
    chapters = [Chapter(start=0.0, title="a"), Chapter(start=50.0, title="b")]
    assert tagger.thin_chapters(chapters, duration=100.0) == chapters


def test_split_by_chars_respects_the_budget():
    segments = [caps.Segment(float(i), float(i + 1), "x" * 100) for i in range(50)]
    windows = tagger.split_by_chars(segments, 500)
    assert len(windows) > 1
    assert all(windows)
    assert sum(len(w) for w in windows) == 50


def test_save_tags_writes_summary_and_chapters(conn, tmp_path):
    from clipforge.ingest import sources

    clip = tmp_path / "own.mp4"
    clip.write_bytes(b"v")
    source_id = sources.register(conn, sources.own_campaign_id(conn), path=clip)
    tagger.save_tags(conn, source_id, SourceTags(
        summary="A heist goes wrong.",
        chapters=[Chapter(start=0.0, title="Setup")], language="en",
    ))
    row = conn.execute("SELECT summary, chapters_json, language FROM sources WHERE id=?",
                       (source_id,)).fetchone()
    assert row["summary"] == "A heist goes wrong."
    assert "Setup" in row["chapters_json"]
    assert row["language"] == "en"


# --- the payout cross-check -------------------------------------------------
# gpt-oss-120b read "Clips under 2,000 views do not earn" and left min_views
# null, without flagging it. A silent miss here over-reports earnings, so the
# cross-check is a deterministic alarm beside the model's answer.

def test_flags_a_min_views_the_model_missed():
    notes = briefs.audit_against_brief(
        BriefRules(), "Payout: $1.75 per 1,000 views. Clips under 2,000 views do not earn."
    )
    joined = " ".join(notes)
    assert "min_views" in joined
    assert "2,000 views do not earn" in joined


@pytest.mark.parametrize("text", [
    "Clips under 2,000 views do not earn.",
    "Minimum 2,000 views to qualify.",
    "A minimum of 2000 views is required.",
    "2,000 views minimum.",
    "Your clip must reach 2000 views.",
    "Clips with fewer than 2,000 views are not paid.",
    "At least 5,000 views to earn.",
])
def test_min_views_phrasings(text):
    assert any("min_views" in n for n in briefs.audit_against_brief(BriefRules(), text))


def test_flags_a_missed_rate_and_cap():
    notes = " ".join(briefs.audit_against_brief(
        BriefRules(), "We pay $2.50 per 1000 views, capped at $150 per clip."))
    assert "rate_per_1k" in notes and "clip_cap_usd" in notes


def test_says_nothing_when_the_model_already_got_it():
    rules = BriefRules(min_views=2000, rate_per_1k=1.75, clip_cap_usd=250.0)
    assert briefs.audit_against_brief(
        rules, "Clips under 2,000 views do not earn. $1.75 per 1,000. Max $250 per clip."
    ) == []


def test_says_nothing_when_the_brief_is_genuinely_silent():
    """A brief with no payout terms must not produce noise."""
    assert briefs.audit_against_brief(
        BriefRules(), "Clip our launch stream. Keep it vertical. Tag #GTA6."
    ) == []


def test_zero_is_not_treated_as_absent():
    """0 is a real answer; only None means the model left it empty."""
    rules = BriefRules(min_views=0)
    assert not any("min_views" in n for n in briefs.audit_against_brief(
        rules, "Clips under 2,000 views do not earn."))


def test_the_cross_check_never_changes_a_value():
    rules = BriefRules(min_views=None)
    briefs.audit_against_brief(rules, "Clips under 2,000 views do not earn.")
    assert rules.min_views is None     # a human sets it, not a regex


def test_parse_brief_appends_the_notes_and_does_not_duplicate(conn, settings):
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at, brief_text) "
        "VALUES ('whop','audit','Audit','active',datetime('now'),?)",
        ("Payout $1.75 per 1,000 views. Clips under 2,000 views do not earn.",),
    )
    campaign_id = int(cur.lastrowid)
    answer = BriefRules(rate_per_1k=1.75)        # model missed min_views
    rules = briefs.parse_brief(conn, campaign_id, call=fake_caller(answer),
                               settings=settings)
    assert any("min_views" in n for n in rules.uncertain)
    assert briefs.load_rules(conn, campaign_id).uncertain == rules.uncertain
    before = len(rules.uncertain)
    again = briefs.parse_brief(conn, campaign_id, call=fake_caller(answer),
                               settings=settings)
    assert len(again.uncertain) == before        # re-parsing must not stack notes


# --- adaptive window splitting ----------------------------------------------
# Measured against Groq on 13 Nov 2026: 14,000 characters of the real stream
# came to 9,000-13,000 tokens, roughly one token per character instead of the
# usual four, and 12 of 19 windows were refused. Token cost is not something to
# estimate, so the provider's own refusal drives a split.

def too_large(requested=13000, limit=8000):
    return llm.LLMRequestTooLarge(
        f"this one request is {requested} tokens against a {limit}-token limit",
        limit=limit, requested=requested)


def test_a_refused_window_is_split_and_retried(settings):
    """The window that was too big must still end up summarised."""
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(400)]
    state = {"calls": 0, "refusals": 0}
    # Derive the refusal size from the real budget, so this test does not break
    # every time the measured window size is retuned.
    ceiling = settings.max_transcript_chars * 2 // 3

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        if output_model is tagger.Overall:
            return tagger.Overall(summary="whole", language="en")
        state["calls"] += 1
        if len(user) > ceiling:
            state["refusals"] += 1
            raise too_large()
        return SourceTags(summary=f"part {state['calls']}")

    tags = tagger.describe(many, duration=400.0, call=call, settings=settings)
    assert state["refusals"] > 0, "the test should have exercised a refusal"
    assert tags.summary == "whole"


def test_splitting_gives_up_rather_than_looping_forever(settings):
    """A window refused at every size must not recurse without end."""
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(200)]
    calls = {"n": 0}

    def call(output_model, system, user, *, model=None, settings=None, **kw):
        if output_model is tagger.Overall:
            return tagger.Overall(summary="whole")
        calls["n"] += 1
        raise too_large()

    with pytest.raises(llm.LLMBadAnswer, match="no part of the transcript"):
        tagger.describe(many, duration=200.0, call=call, settings=settings)
    # Bounded: each window halves at most MAX_SPLITS times.
    windows = len(tagger.split_by_chars(many, settings.max_transcript_chars))
    assert calls["n"] <= windows * (2 ** (tagger.MAX_SPLITS + 1))


def test_a_single_segment_window_is_not_split(settings):
    """Nothing left to halve, so it is dropped rather than retried forever."""
    one = [caps.Segment(0.0, 1.0, "x" * 50)]
    calls = {"n": 0}

    def call(output_model, system, user, **kw):
        calls["n"] += 1
        raise too_large()

    result = tagger._digest(one, 1, 1, title=None, duration=1.0, call=call,
                            settings=settings)
    assert result == []
    assert calls["n"] == 1


def test_the_default_budget_matches_the_setting(settings):
    """A stale constant beside a live setting is a trap."""
    assert tagger.MAX_TRANSCRIPT_CHARS == settings.max_transcript_chars


# --- sampling a long transcript ---------------------------------------------
# Measured 13 Nov 2026: a 5h45 transcript is 44 windows and ~237,000 input
# tokens, which against 8,000 tokens a minute is half an hour of rate limiting
# to produce a 3-5 sentence summary and 6-12 chapters. The moments come from
# signal peaks over the whole transcript, so sampling here costs nothing.

def test_a_long_transcript_is_sampled_not_fully_summarised(settings):
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(4000)]
    call = by_type(SourceTags=SourceTags(summary="part"),
                   Overall=tagger.Overall(summary="whole", language="en"))
    tagger.describe(many, duration=4000.0, call=call, settings=settings)
    window_calls = [c for c in call.calls if c["output_model"] is SourceTags]
    assert len(window_calls) == settings.summary_windows == 12


def test_the_reduce_step_is_told_it_saw_a_sample(settings):
    """Otherwise it would claim to describe parts it never read."""
    many = [caps.Segment(float(i), float(i + 1), "word " * 40) for i in range(4000)]
    call = by_type(SourceTags=SourceTags(summary="part"),
                   Overall=tagger.Overall(summary="whole"))
    tagger.describe(many, duration=4000.0, call=call, settings=settings)
    assert "samples spread across the video" in call.calls[-1]["user"]


def test_a_transcript_short_enough_is_not_sampled(settings):
    # Sized from the real budget so retuning it does not break this test:
    # enough text for a few windows, comfortably under the sampling cap.
    per_window = settings.max_transcript_chars // 214      # 200 chars + timestamp
    few = [caps.Segment(float(i), float(i + 1), "word " * 40)
           for i in range(per_window * (settings.summary_windows // 2))]
    call = by_type(SourceTags=SourceTags(summary="part"),
                   Overall=tagger.Overall(summary="whole"))
    tagger.describe(few, duration=float(len(few)), call=call, settings=settings)
    windows = len(tagger.split_by_chars(few, settings.max_transcript_chars))
    assert 1 < windows <= settings.summary_windows
    assert "samples spread across" not in call.calls[-1]["user"]


def test_sampling_keeps_the_first_and_last():
    """The record should cover how the stream opened and how it ended."""
    windows = [[caps.Segment(float(i), float(i + 1), f"w{i}")] for i in range(44)]
    picked, total = tagger._sample(windows, 12)
    assert total == 44
    assert len(picked) == 12
    assert picked[0] is windows[0]
    assert picked[-1] is windows[-1]


def test_sampling_is_evenly_spread():
    windows = [[caps.Segment(float(i), float(i + 1), f"w{i}")] for i in range(44)]
    picked, _ = tagger._sample(windows, 12)
    indexes = [windows.index(w) for w in picked]
    gaps = [b - a for a, b in zip(indexes, indexes[1:])]
    assert max(gaps) - min(gaps) <= 1


def test_sampling_leaves_a_short_list_alone():
    windows = [[caps.Segment(0.0, 1.0, "a")], [caps.Segment(1.0, 2.0, "b")]]
    picked, total = tagger._sample(windows, 12)
    assert picked == windows and total == 2
