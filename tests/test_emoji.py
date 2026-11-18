"""Reaction emoji: picked from the spoken words, fetched once, drawn under the captions."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from clipforge.edit import emoji, layout, render
from clipforge.ingest.rules import BriefRules
from clipforge.taste import style as style_mod

PNG = b"\x89PNG\r\n\x1a\nfake"


def fake_set(tmp_path: Path) -> Path:
    for stem in emoji.SET.values():
        (tmp_path / f"{stem}.png").write_bytes(PNG)
    return tmp_path


def test_emoji_follow_the_words_spaced_out_and_capped(tmp_path):
    d = fake_set(tmp_path)
    words = [(0.5, 0.8, "LMAO"), (1.0, 1.3, "that's"), (2.0, 2.4, "insane"), (5.0, 5.3, "no"),
             (9.0, 9.3, "money"), (12.0, 12.4, "win")]
    got = emoji.pick(words, max_events=3, duration=10.0, directory=d)
    assert [(p.name, p.start) for p in got] == [("laugh", 0.5), ("scream", 5.0), ("money", 9.0)]
    assert got[-1].end == 10.0                                   # never past the clip
    assert emoji.pick([(0, 1, "hello"), (2, 3, "there")], directory=d) == []      # nothing called for one


def test_fetch_downloads_the_set_once_with_the_credit(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return SimpleNamespace(status_code=200, content=PNG if url.endswith(".png") else b"CC-BY 4.0")

    assert emoji.fetch(tmp_path, get=get) == len(emoji.SET)
    assert emoji.available(tmp_path) and "CC-BY 4.0" in (tmp_path / "CREDIT.txt").read_text(encoding="utf-8")
    n = len(calls)
    assert emoji.fetch(tmp_path, get=get) == 0 and len(calls) == n          # nothing fetched twice


def test_render_overlays_each_emoji_only_while_it_shows_and_under_the_captions(tmp_path):
    d = fake_set(tmp_path)
    pops = emoji.pick([(1.0, 1.3, "insane"), (6.0, 6.2, "lol")], directory=d)
    args = render.build_command(Path("in.mp4"), ss=0, duration=10, layout=layout.load("nobody"),
                                ass_name="s.ass", dest=Path("o.mp4"), pops=pops, pop_y=1400)
    graph = args[args.index("-filter_complex") + 1]
    assert "enable='between(t,1.00,2.40)'" in graph and "enable='between(t,6.00,7.40)'" in graph
    assert graph.index("overlay=") < graph.index("ass=s.ass")              # captions drawn on top
    assert args.count("-i") == 3


def test_a_brief_that_bans_graphics_turns_emoji_off():
    book = {"style": {"emoji": {"enabled": True}}}
    assert style_mod.resolve(book, None).style.emoji.enabled
    banned = style_mod.resolve(book, BriefRules(banned_edits=["No stickers or emoji"]))
    assert not banned.style.emoji.enabled and any("emoji" in d for d in banned.dropped)
