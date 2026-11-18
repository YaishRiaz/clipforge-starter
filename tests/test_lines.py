import subprocess

from clipforge.core import media
from clipforge.core.config import get_settings
from clipforge.taste import lines


def _words():
    # line 1 (0-1.5), false start (2.0-2.6), long silence, line 3 (5.0-6.5), payoff (6.8-8.0)
    return [(0.1, 0.5, "So"), (0.5, 1.0, "this"), (1.0, 1.5, "happened."),
            (2.0, 2.3, "I"), (2.3, 2.6, "I"),
            (5.0, 5.5, "then"), (5.5, 6.0, "he"), (6.0, 6.5, "ran."),
            (6.8, 7.4, "Insane"), (7.4, 8.0, "honestly.")]


class Ans:
    def __init__(self, *dels):
        self.lines = [lines.Verdict(n=n, verdict="DELETE") for n in dels]


def test_group_splits_on_pause_and_sentence_end():
    ls = lines.group(_words())
    assert [l.text for l in ls] == ["So this happened.", "I I", "then he ran.", "Insane honestly."]


def test_plan_cuts_deleted_line_and_long_silence():
    keep = lines.plan(_words(), 8.2, get_settings(), min_seconds=3, ask=lambda u: Ans(2))
    assert keep is not None and len(keep) == 2          # splice where the false start was
    assert keep[0][1] < 2.0 and keep[1][0] > 4.5
    assert sum(e - s for s, e in keep) < 8.2 - 1.0


def test_first_and_last_lines_are_never_deleted_and_at_most_a_quarter_by_lines():
    keep = lines.plan(_words(), 8.2, get_settings(), min_seconds=3, ask=lambda u: Ans(1, 4, 3))
    assert keep is not None
    assert keep[0][0] == 0.0 and keep[-1][1] >= 8.0      # opening and payoff survive


def test_no_cuts_when_nothing_to_remove():
    tight = [(0.0, 0.4, "a"), (0.45, 0.9, "b."), (1.0, 1.4, "c"), (1.45, 1.9, "d."), (2.0, 2.4, "e")]
    assert lines.plan(tight, 2.5, get_settings(), ask=lambda u: Ans()) is None


def test_remap_moves_words_onto_the_new_clock():
    keep = [(0.0, 1.8), (4.9, 8.0)]
    out = lines.remap(_words(), keep)
    assert [w[2] for w in out][:4] == ["So", "this", "happened.", "then"]
    assert "I" not in [w[2] for w in out]
    assert abs(out[3][0] - (5.0 - 4.9 + 1.8)) < 1e-6


def test_render_tight_joins_the_parts(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
    out = lines.render_tight(src, ss=0.5, keep=[(0.0, 1.0), (3.0, 4.0)], dest=tmp_path / "t.mkv")
    assert abs(media.probe(out).duration - 2.0) < 0.2


def test_deleted_lines_are_capped_by_share():
    words = [(0.0, 1.0, "Start."), (1.1, 3.1, "aside one."), (3.2, 5.2, "aside two."),
             (5.3, 6.3, "joke."), (6.4, 7.4, "end.")]
    keep = lines.plan(words, 8.0, get_settings(), min_seconds=3, ask=lambda u: Ans(2, 3))
    kept = sum(e - s for s, e in keep)
    assert kept >= 8.0 * (1 - lines.MAX_CUT_SHARE) - 0.7     # one 2 s aside may go, not both


def test_clip_tighten_writes_choice_and_returns_new_clock(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clipforge.core import handlers

    src = tmp_path / "src.mp4"
    subprocess.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=9",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=9", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
    monkeypatch.setattr(lines, "plan", lambda *a, **k: [(0.0, 2.0), (5.0, 8.0)])
    ctx = SimpleNamespace(settings=get_settings(), beat=lambda: None, ref_id=1)
    plan = {"footage": str(src), "start": 10.0, "offset": 10.0}
    words, dur, footage, ss, plan2 = handlers._clip_tighten(ctx, tmp_path, plan, _words(), 8.2, 0.0, None)
    assert abs(dur - 5.0) < 1e-6 and ss == 0.0 and footage.name == "tight.mkv"
    assert plan2["start"] == 0.0 and (tmp_path / "cuts.json").is_file()
    assert abs(media.probe(footage).duration - 5.0) < 0.2
    again = handlers._clip_tighten(ctx, tmp_path, plan, _words(), 8.2, 0.0, None)   # a rerun reuses the choice
    assert again[1] == dur
