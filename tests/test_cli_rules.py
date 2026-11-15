"""The campaign-rules command: correcting a parsed brief by hand.

The parser writes a first draft (PRD section 5 step 1). The SkiiMaskTimmy brief
named its footage in words only — "SkiiMaskTimmy's Twitch streams, live or
VODs" — so the gate correctly refuses everything until a human names the
specific videos the campaign permits. This command is that step, and it must
never be a way for a model's guess to widen what is approved.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.__main__ import main
from clipforge.core import db
from clipforge.core.config import get_settings
from clipforge.ingest import briefs, sources
from clipforge.ingest.rules import BriefRules

VOD = "https://www.twitch.tv/videos/1234567890"


@pytest.fixture()
def campaign(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    conn = db.open_db(get_settings().db_path)
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status, joined_at) "
        "VALUES ('whop','skii','SkiiMaskTimmy','active',datetime('now'))"
    )
    campaign_id = int(cur.lastrowid)
    briefs.save_rules(conn, campaign_id, BriefRules(
        approved_source_notes="Footage from SkiiMaskTimmy's Twitch streams only",
        clip_min_seconds=7.0,
        uncertain=["rate per 1k not specified", "clip max seconds not specified"],
    ))
    conn.close()
    yield campaign_id


def reload(campaign_id: int) -> BriefRules:
    conn = db.open_db(get_settings().db_path)
    try:
        return briefs.load_rules(conn, campaign_id)
    finally:
        conn.close()


def test_shows_rules_and_warns_when_nothing_is_approved(campaign, capsys):
    assert main(["campaign-rules", str(campaign)]) == 0
    out = capsys.readouterr().out
    assert "NOTHING can be ingested" in out
    assert "SkiiMaskTimmy" in out


def test_adding_a_source_unblocks_the_gate(campaign):
    """The whole point: the gate refuses, a human approves, the gate accepts."""
    conn = db.open_db(get_settings().db_path)
    with pytest.raises(sources.NotApproved):
        sources.register(conn, campaign, url=VOD)
    conn.close()

    assert main(["campaign-rules", str(campaign), "--add-source", VOD]) == 0
    assert reload(campaign).approved_source_urls == [VOD]

    conn = db.open_db(get_settings().db_path)
    source_id = sources.register(conn, campaign, url=VOD)
    assert source_id > 0
    conn.close()


@pytest.mark.parametrize("second", [
    VOD,                          # the same string
    VOD + "/",                    # a trailing slash
    VOD + "?t=1h2m3s",            # a timestamp: same VOD, different moment
    VOD + "?filter=all&sort=time",  # the viewer's own list filter
    VOD.replace("www.", ""),      # without www
])
def test_the_same_vod_in_another_form_is_not_a_duplicate(campaign, capsys, second):
    main(["campaign-rules", str(campaign), "--add-source", VOD])
    main(["campaign-rules", str(campaign), "--add-source", second])
    assert len(reload(campaign).approved_source_urls) == 1
    assert "already approved" in capsys.readouterr().out


def test_a_genuinely_different_vod_is_added(campaign):
    main(["campaign-rules", str(campaign), "--add-source", VOD])
    main(["campaign-rules", str(campaign), "--add-source",
          "https://www.twitch.tv/videos/9999999999"])
    assert len(reload(campaign).approved_source_urls) == 2


def test_removing_a_source_closes_the_gate_again(campaign):
    main(["campaign-rules", str(campaign), "--add-source", VOD])
    assert main(["campaign-rules", str(campaign), "--remove-source", VOD]) == 0
    assert reload(campaign).approved_source_urls == []
    conn = db.open_db(get_settings().db_path)
    with pytest.raises(sources.NotApproved):
        sources.register(conn, campaign, url=VOD)
    conn.close()


def test_set_coerces_types(campaign):
    assert main([
        "campaign-rules", str(campaign),
        "--set", "min_views=2000",
        "--set", "rate_per_1k=1.75",
        "--set", "pre_approval=true",
        "--set", "required_post_settings=[\"comments on\"]",
    ]) == 0
    rules = reload(campaign)
    assert rules.min_views == 2000 and isinstance(rules.min_views, int)
    assert rules.rate_per_1k == 1.75
    assert rules.pre_approval is True
    assert rules.required_post_settings == ["comments on"]


def test_set_can_write_null(campaign):
    main(["campaign-rules", str(campaign), "--set", "clip_min_seconds=null"])
    assert reload(campaign).clip_min_seconds is None


def test_set_accepts_a_bare_string(campaign):
    main(["campaign-rules", str(campaign), "--set", "required_aspect_ratio=9:16"])
    assert reload(campaign).required_aspect_ratio == "9:16"


def test_an_unknown_field_is_refused_and_lists_the_real_ones(campaign, capsys):
    assert main(["campaign-rules", str(campaign), "--set", "nonsense=1"]) == 2
    assert "no such field" in capsys.readouterr().err


def test_a_value_of_the_wrong_type_changes_nothing(campaign, capsys):
    assert main(["campaign-rules", str(campaign), "--set", "min_views=not-a-number"]) == 2
    assert "not be a valid rule set" in capsys.readouterr().err
    assert reload(campaign).min_views is None


def test_set_without_equals_is_refused(campaign, capsys):
    assert main(["campaign-rules", str(campaign), "--set", "min_views"]) == 2
    assert "FIELD=VALUE" in capsys.readouterr().err


def test_clear_uncertain_only_after_review(campaign):
    assert len(reload(campaign).uncertain) == 2
    main(["campaign-rules", str(campaign), "--clear-uncertain"])
    assert reload(campaign).uncertain == []


def test_an_unparsed_campaign_says_to_parse_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d2"))
    get_settings.cache_clear()
    conn = db.open_db(get_settings().db_path)
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, status) "
        "VALUES ('whop','none','No brief','active')"
    )
    campaign_id = int(cur.lastrowid)
    conn.close()
    assert main(["campaign-rules", str(campaign_id)]) == 2
    assert "parse-brief first" in capsys.readouterr().err
