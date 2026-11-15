"""Local signals: loudness, speech rate, scene changes, peak picking.

No network and no model. The loudness tests run real ffmpeg against generated
audio whose answer is known in advance, because the thing worth testing is that
a streamed reduction gets the same answer as the obvious one.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from clipforge.core import media
from clipforge.ingest import captions as caps
from clipforge.moments import signals


def tone(path: Path, *, seconds: float, volume: float = 1.0,
         frequency: int = 300) -> Path:
    media.run([
        media._tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"sine=frequency={frequency}:sample_rate=16000:duration={seconds}",
        "-af", f"volume={volume}", "-c:a", "pcm_s16le", str(path),
    ], timeout=300)
    return path


def quiet_then_loud(path: Path, work: Path) -> Path:
    """5 s quiet, then 5 s loud: a known loudness step."""
    a = tone(work / "a.wav", seconds=5, volume=0.02)
    b = tone(work / "b.wav", seconds=5, volume=1.0)
    listing = work / "list.txt"
    listing.write_text(f"file '{a.as_posix()}'\nfile '{b.as_posix()}'\n", encoding="utf-8")
    media.run([
        media._tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", str(path),
    ], timeout=300)
    return path


# --- loudness ---------------------------------------------------------------

def test_loudness_has_one_value_per_window(tmp_path):
    src = tone(tmp_path / "t.wav", seconds=10)
    sig = signals.loudness_series(src, window=1.0)
    assert len(sig) == 10
    assert sig.times[:3] == [0.0, 1.0, 2.0]
    assert sig.name == "loudness_dbfs"


def test_loudness_finds_the_loud_half(tmp_path):
    src = quiet_then_loud(tmp_path / "step.wav", tmp_path)
    sig = signals.loudness_series(src, window=1.0)
    quiet = sum(sig.values[:5]) / 5
    loud = sum(sig.values[6:]) / 4
    assert loud > quiet + 20          # a volume factor of 50 is about 34 dB
    assert loud < 0                   # dBFS is negative below full scale


def test_silence_does_not_become_negative_infinity(tmp_path):
    src = tmp_path / "silent.wav"
    media.run([
        media._tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=sample_rate=16000:cl=mono",
        "-t", "4", "-c:a", "pcm_s16le", str(src),
    ], timeout=300)
    sig = signals.loudness_series(src, window=1.0)
    assert all(math.isfinite(v) for v in sig.values)
    assert all(v <= -100 for v in sig.values)


def test_streaming_matches_a_single_block(tmp_path, monkeypatch):
    """The block size must not change the answer."""
    src = quiet_then_loud(tmp_path / "step.wav", tmp_path)
    full = signals.loudness_series(src, window=1.0)
    monkeypatch.setattr(signals, "BLOCK_SECONDS", 1)      # force many reads
    chopped = signals.loudness_series(src, window=1.0)
    assert len(full) == len(chopped)
    for a, b in zip(full.values, chopped.values):
        assert a == pytest.approx(b, abs=0.01)


def test_a_window_longer_than_the_file_still_works(tmp_path):
    src = tone(tmp_path / "t.wav", seconds=2)
    assert len(signals.loudness_series(src, window=10.0)) == 0


def test_a_missing_file_is_a_media_error(tmp_path):
    with pytest.raises(media.MediaError):
        signals.loudness_series(tmp_path / "nope.wav", window=1.0)


# --- speech rate ------------------------------------------------------------

def test_speech_rate_in_words_per_minute():
    # 10 words spoken over 10 s is 60 wpm.
    segments = [caps.Segment(0.0, 10.0, " ".join(["word"] * 10))]
    sig = signals.speech_rate_series(segments, window=10.0, duration=10.0)
    assert sig.values[0] == pytest.approx(60.0)


def test_a_long_sentence_is_spread_over_the_windows_it_covers():
    """Otherwise it spikes whichever window it happens to start in."""
    segments = [caps.Segment(0.0, 20.0, " ".join(["word"] * 20))]
    sig = signals.speech_rate_series(segments, window=10.0, duration=20.0)
    assert sig.values[0] == pytest.approx(60.0)
    assert sig.values[1] == pytest.approx(60.0)


def test_speech_rate_spike_is_visible():
    segments = [
        caps.Segment(0.0, 10.0, " ".join(["slow"] * 5)),
        caps.Segment(10.0, 20.0, " ".join(["fast"] * 40)),
    ]
    sig = signals.speech_rate_series(segments, window=10.0, duration=20.0)
    assert sig.values[1] > sig.values[0] * 5


def test_empty_transcript_gives_an_empty_signal():
    sig = signals.speech_rate_series([], window=10.0)
    assert sig.values == [0.0]


def test_silence_between_segments_reads_as_zero():
    segments = [caps.Segment(0.0, 5.0, "a b c"), caps.Segment(25.0, 30.0, "d e f")]
    sig = signals.speech_rate_series(segments, window=10.0, duration=30.0)
    assert sig.values[1] == 0.0


# --- scene changes ----------------------------------------------------------

def test_an_audio_only_source_has_no_scene_changes(tmp_path):
    """Normal for a Twitch VOD fetched as audio, not an error."""
    src = tone(tmp_path / "a.wav", seconds=3)
    assert signals.scene_change_times(src) == []


def test_scene_changes_found_in_a_cut_video(tmp_path):
    src = tmp_path / "cuts.mp4"
    # Three very different solid colours, one second each: two hard cuts.
    media.run([
        media._tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1,format=yuv420p",
        "-f", "lavfi", "-i", "color=c=white:s=320x240:d=1,format=yuv420p",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1,format=yuv420p",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-r", "10", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(src),
    ], timeout=300)
    found = signals.scene_change_times(src, threshold=0.3)
    assert len(found) >= 2


def test_density_counts_events_per_window():
    sig = signals.density([1.0, 2.0, 3.0, 25.0], window=10.0, duration=30.0)
    assert sig.values == [3.0, 0.0, 1.0]


# --- peak picking -----------------------------------------------------------

def make_signal(values, window=1.0, name="test"):
    return signals.Signal(name, window, [i * window for i in range(len(values))], values)


def test_finds_a_peak_above_the_threshold():
    sig = make_signal([0, 0, 0, 0, 10, 0, 0, 0, 0, 0])
    found = signals.peaks(sig, z=1.5, min_gap=2.0)
    assert len(found) == 1
    assert found[0].start == 4.0 and found[0].value == 10
    assert found[0].signal == "test"


def test_a_flat_signal_has_no_peaks():
    assert signals.peaks(make_signal([5] * 10)) == []


def test_adjacent_windows_become_one_peak():
    sig = make_signal([0, 0, 9, 10, 9, 0, 0, 0, 0, 0])
    found = signals.peaks(sig, z=1.0, min_gap=2.0)
    assert len(found) == 1
    assert found[0].start == 2.0 and found[0].end == 5.0


def test_nearby_peaks_are_merged():
    """Two shouts a second apart are one moment."""
    sig = make_signal([0, 0, 10, 0, 10, 0, 0, 0, 0, 0, 0, 0])
    assert len(signals.peaks(sig, z=1.0, min_gap=5.0)) == 1


def test_distant_peaks_stay_separate():
    sig = make_signal([0] * 5 + [10] + [0] * 20 + [10] + [0] * 5)
    assert len(signals.peaks(sig, z=1.0, min_gap=5.0)) == 2


def test_top_n_keeps_the_strongest_in_time_order():
    sig = make_signal([0] * 5 + [20] + [0] * 10 + [10] + [0] * 10 + [30] + [0] * 5)
    found = signals.peaks(sig, z=0.5, min_gap=5.0, top_n=2)
    assert len(found) == 2
    assert [p.value for p in found] == [20, 30]       # time order, not rank
    assert found[0].start < found[1].start


def test_z_is_relative_to_the_signal_itself():
    """The same threshold must work on a quiet stream and a loud one."""
    quiet = make_signal([1, 1, 1, 1, 5, 1, 1, 1, 1, 1])
    loud = make_signal([v + 100 for v in [1, 1, 1, 1, 5, 1, 1, 1, 1, 1]])
    assert [p.start for p in signals.peaks(quiet, z=1.5)] == \
           [p.start for p in signals.peaks(loud, z=1.5)]


def test_too_short_to_judge():
    assert signals.peaks(make_signal([1, 10])) == []


def test_signal_statistics_and_lookup():
    sig = make_signal([0, 2, 4, 6, 8], window=2.0)
    assert sig.mean == pytest.approx(4.0)
    assert sig.stdev == pytest.approx(math.sqrt(10.0))
    assert sig.value_at(0.0) == 0
    assert sig.value_at(5.0) == 4          # window 2 covers 4-6 s
    assert sig.value_at(10_000) == 8       # clamped, not an error


# --- robust thresholds ------------------------------------------------------
# Measured on the real 5h45 VOD: silence floors at -109 dBFS while speech sits
# near -26, giving mean -30 and st. dev 15. mean + 1.8sd is -3 dBFS, which is
# above the loudest window in the whole stream (-15), so loudness produced zero
# peaks and the only signal left was speech rate.

def test_a_silence_tail_does_not_kill_the_signal():
    """The exact shape that broke it: mostly speech, a long tail of silence."""
    speech = [-26.0 + (i % 5) for i in range(900)]
    speech[500] = -16.0                      # one genuine shout
    silence = [-109.0] * 100
    sig = make_signal(speech + silence)
    assert sig.mean + 1.8 * sig.stdev > 0, "this is the broken threshold"
    found = signals.peaks(sig, z=1.8, min_gap=5.0)
    assert found, "the shout must still be found"
    assert any(abs(p.start - 500.0) < 5 for p in found)


def test_median_and_mad_ignore_the_tail():
    sig = make_signal([-26.0] * 900 + [-109.0] * 100)
    assert sig.median == pytest.approx(-26.0)
    assert sig.mad < abs(sig.stdev)


def test_mad_falls_back_to_stdev_when_it_is_zero():
    """A signal that is mostly one value has a MAD of zero."""
    sig = make_signal([0, 0, 0, 0, 10, 0, 0, 0, 0, 0])
    assert sig.mad == 0
    assert signals.threshold_for(sig, 1.5) == pytest.approx(sig.mean + 1.5 * sig.stdev)
    assert len(signals.peaks(sig, z=1.5, min_gap=2.0)) == 1


def test_a_truly_flat_signal_has_no_threshold():
    assert signals.threshold_for(make_signal([5] * 10), 1.5) is None


def test_a_stream_with_a_silent_third_still_has_reachable_peaks():
    """Measured: a two-sided MAD came to 10.2 on a stream with long silences,
    putting a 1.8 threshold above the loudest window. Spread is taken from the
    speech side, so the shouts in the top tail are still found."""
    import random

    rng = random.Random(1)
    silence = [-109.0] * 1500
    speech = [-28.0 + rng.gauss(0, 4) for _ in range(3500)]
    speech[100] = -9.0
    speech[1800] = -8.5
    sig = make_signal(silence + speech)
    found = signals.peaks(sig, z=1.8, min_gap=20.0)
    for shout in (1600, 3300):
        assert any(p.start <= shout < p.end for p in found), shout
