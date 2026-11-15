"""Brief rules, and the URL approval test hard limit 1 rests on."""
from __future__ import annotations

import pytest

from clipforge.ingest.rules import BriefRules, normalize_url, youtube_id


@pytest.mark.parametrize("url, expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://example.com/watch?v=dQw4w9WgXcQ", None),
    ("https://www.youtube.com/@somechannel", None),
    ("not a url", None),
])
def test_youtube_id(url, expected):
    assert youtube_id(url) == expected


def test_normalize_drops_tracking_and_case():
    a = normalize_url("https://WWW.Example.com/folder/?utm_source=x&id=7")
    b = normalize_url("http://example.com/folder?id=7")
    assert a.endswith("example.com/folder?id=7")
    assert a.split("://")[1] == b.split("://")[1]


def test_normalize_collapses_youtube_shapes():
    forms = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share",
    ]
    assert len({normalize_url(f) for f in forms}) == 1


def test_approves_exact_link_in_any_shape():
    rules = BriefRules(approved_source_urls=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
    assert rules.approves_url("https://youtu.be/dQw4w9WgXcQ?si=tracking")
    assert rules.approves_url("https://m.youtube.com/shorts/dQw4w9WgXcQ")


def test_refuses_anything_the_brief_does_not_name():
    rules = BriefRules(approved_source_urls=["https://youtu.be/dQw4w9WgXcQ"])
    assert not rules.approves_url("https://youtu.be/aaaaaaaaaaa")
    assert not rules.approves_url("https://example.com/some.mp4")


def test_a_brief_with_no_links_approves_nothing():
    """The default must be refusal, or hard limit 1 would leak."""
    assert not BriefRules().approves_url("https://youtu.be/dQw4w9WgXcQ")
    assert not BriefRules().approves_url("")


def test_folder_approves_what_is_under_it():
    rules = BriefRules(approved_source_urls=["https://drive.google.com/drive/folders/XYZ"])
    assert rules.approves_url("https://drive.google.com/drive/folders/XYZ/clip1.mp4")
    assert rules.approves_url("https://drive.google.com/drive/folders/XYZ")


def test_folder_match_respects_the_path_boundary():
    """A prefix match on raw text would approve a different, similarly named folder."""
    rules = BriefRules(approved_source_urls=["https://example.com/approved"])
    assert not rules.approves_url("https://example.com/approved-private/clip.mp4")
    assert not rules.approves_url("https://example.com/approvedother")


def test_clip_length_bounds():
    rules = BriefRules(clip_min_seconds=15, clip_max_seconds=60)
    assert rules.clip_length_ok(30)[0]
    ok, why = rules.clip_length_ok(8)
    assert not ok and "at least 15" in why
    ok, why = rules.clip_length_ok(75)
    assert not ok and "at most 60" in why


def test_clip_length_unbounded_when_the_brief_is_silent():
    assert BriefRules().clip_length_ok(300)[0]


def test_platform_gate():
    rules = BriefRules(allowed_platforms=["YouTube_Shorts", "tiktok"])
    assert rules.allows_platform("youtube_shorts")
    assert not rules.allows_platform("instagram_reels")
    # Silence on platforms is not a refusal; the campaign page decides.
    assert BriefRules().allows_platform("anything")


def test_own_footage_rules_carry_no_brief_restrictions():
    rules = BriefRules.for_own_footage()
    assert rules.approved_source_urls == []
    assert rules.clip_length_ok(45)[0]
    assert rules.allows_platform("youtube_shorts")
    assert not rules.pre_approval


# --- fields added for the SkiiMaskTimmy campaign ----------------------------
# That brief required a channel-URL overlay on every clip, 9:16, and comments
# and likes on. The parser flagged all three as having nowhere to go, so they
# would have been dropped and the clips rejected.

def test_required_overlay_and_post_settings_are_held():
    rules = BriefRules(
        required_overlay="twitch.tv/skiimasktimmy overlay on every clip",
        required_aspect_ratio="9:16",
        required_post_settings=["comments on", "likes on"],
    )
    assert "skiimasktimmy" in rules.required_overlay
    assert rules.required_aspect_ratio == "9:16"
    assert rules.required_post_settings == ["comments on", "likes on"]


def test_the_new_fields_default_to_empty():
    """A brief that says nothing about them must not imply a requirement."""
    rules = BriefRules()
    assert rules.required_overlay is None
    assert rules.required_aspect_ratio is None
    assert rules.required_post_settings == []


def test_rules_saved_before_these_fields_existed_still_load():
    """rules_json is a blob, so old rows must survive the schema growing."""
    old = {
        "approved_source_urls": ["https://youtu.be/dQw4w9WgXcQ"],
        "clip_min_seconds": 7.0,
        "banned_content": ["other streamers"],
    }
    rules = BriefRules.model_validate(old)
    assert rules.clip_min_seconds == 7.0
    assert rules.required_overlay is None
    assert rules.approves_url("https://youtu.be/dQw4w9WgXcQ")


def test_a_brief_with_no_links_still_approves_nothing_with_notes_set():
    """The SkiiMaskTimmy shape: footage described in words, no URLs."""
    rules = BriefRules(
        approved_source_notes="Footage from SkiiMaskTimmy's Twitch streams only, live or VODs",
        clip_min_seconds=7.0,
    )
    assert rules.approved_source_urls == []
    assert not rules.approves_url("https://www.twitch.tv/videos/1234567890")
    assert not rules.approves_url("https://www.twitch.tv/skiimasktimmy")


@pytest.mark.parametrize("variant", [
    "https://www.twitch.tv/videos/1234567890/",
    "https://www.twitch.tv/videos/1234567890?t=1h2m3s",
    "https://twitch.tv/videos/1234567890",
    "https://www.twitch.tv/videos/1234567890?filter=all&sort=time",
])
def test_twitch_vod_forms_normalize_together(variant):
    """A VOD pasted with a timestamp or list filter is still the same footage."""
    base = normalize_url("https://www.twitch.tv/videos/1234567890")
    assert normalize_url(variant) == base


def test_a_different_twitch_vod_does_not_normalize_together():
    assert (normalize_url("https://www.twitch.tv/videos/1234567890")
            != normalize_url("https://www.twitch.tv/videos/9999999999"))
