"""Reading captions that came with the footage."""
from __future__ import annotations

from pathlib import Path

from clipforge.ingest import captions as caps

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
Right, we're going in through the roof.

00:00:04.000 --> 00:00:07.500
<c.colorE5E5E5>Wait, there's a <00:00:05.100>cop</c> right there.

00:00:07.500 --> 00:00:09.000
&amp; then it all went wrong
"""

# YouTube's auto-captions repeat the previous line at the top of each cue.
ROLLING = """WEBVTT

00:00:00.500 --> 00:00:02.000
okay so

00:00:02.000 --> 00:00:03.500
okay so here's the plan

00:00:03.500 --> 00:00:05.000
here's the plan we go in quiet
"""

SRT = """1
00:00:02,000 --> 00:00:05,000
First line here

2
00:00:05,000 --> 00:00:08,000
Second line here
"""


def test_parses_webvtt_and_strips_markup():
    segments = caps.parse_vtt(VTT)
    assert len(segments) == 3
    assert segments[0].start == 1.0 and segments[0].end == 4.0
    assert segments[1].text == "Wait, there's a cop right there."
    assert segments[2].text == "& then it all went wrong"


def test_parses_srt_with_comma_milliseconds():
    segments = caps.parse_vtt(SRT)
    assert [s.start for s in segments] == [2.0, 5.0]
    assert segments[0].text == "First line here"


def test_rolling_auto_captions_are_not_repeated():
    text = caps.as_text(caps.parse_vtt(ROLLING), with_times=False)
    assert text.count("okay so") == 1
    assert text.count("here's the plan") == 1
    assert "we go in quiet" in text


def test_ignores_cues_with_no_text():
    segments = caps.parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n\n")
    assert segments == []


def test_handles_hour_long_timestamps():
    segments = caps.parse_vtt("WEBVTT\n\n01:02:03.400 --> 01:02:05.000\nlate in the stream\n")
    assert segments[0].start == 3723.4


def test_save_and_load_round_trip(tmp_path: Path):
    original = caps.parse_vtt(VTT)
    path = caps.save(original, tmp_path / "t.json", source="supplied captions")
    loaded = caps.load(path)
    assert [(s.start, s.end, s.text) for s in loaded] == \
           [(s.start, s.end, s.text) for s in original]


def test_as_text_carries_timestamps_for_the_model():
    line = caps.as_text(caps.parse_vtt(VTT)).splitlines()[0]
    assert line.startswith("[00:01.00]")


def test_word_count():
    assert caps.word_count(caps.parse_vtt(SRT)) == 6


def test_sidecar_prefers_english(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    (tmp_path / "clip.es.vtt").write_text(VTT, encoding="utf-8")
    (tmp_path / "clip.en.vtt").write_text(VTT, encoding="utf-8")
    assert caps.find_sidecar(video).name == "clip.en.vtt"


def test_no_sidecar_is_not_an_error(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    assert caps.find_sidecar(video) is None


def test_sidecar_with_glob_characters_in_the_name(tmp_path: Path):
    video = tmp_path / "clip [raw].mp4"
    video.write_bytes(b"v")
    (tmp_path / "clip [raw].en.vtt").write_text(VTT, encoding="utf-8")
    assert caps.find_sidecar(video) is not None
