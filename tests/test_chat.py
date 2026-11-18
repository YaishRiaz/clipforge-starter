"""Chat reaction as a clipping signal."""
from __future__ import annotations

import json
from types import SimpleNamespace

from clipforge.moments import chat
from clipforge.moments.chat import Message
from clipforge.moments.score import Candidate, Scored
from clipforge.moments.signals import Peak


def test_vod_id_and_message_kinds():
    assert chat.vod_id("https://www.twitch.tv/videos/2922205926") == "2922205926"
    assert chat.vod_id("https://www.twitch.tv/skiimasktimmy/video/55") == "55"
    assert chat.vod_id("https://youtube.com/watch?v=x") is None and chat.vod_id(None) is None
    assert chat.kind("LMAOOO") == "laugh" and chat.kind("KEKW") == "laugh" and chat.kind("\U0001F602") == "laugh"
    assert chat.kind("no way") == "hype" and chat.kind("WWWW") == "hype" and chat.kind("clip it") == "hype"
    assert chat.kind("what map is this") == "" and chat.kind("how are you") == ""


def burst(at, n=12, text="LMAO"):
    return [Message(at + i * 0.3, f"user{i}", text) for i in range(n)]


def quiet(duration):
    return [Message(t, f"u{t % 3}", "hello there") for t in range(0, duration, 9)]


def test_a_laughing_burst_is_a_peak_placed_before_the_chat_reacted():
    msgs = quiet(600) + burst(300)
    found = chat.find_peaks(msgs, 600)
    assert len(found) == 1
    p = found[0]
    assert p.signal == "chat" and p.start < 300 and p.end > 290 and "laughing" in p.note and "lmao" in p.note
    assert chat.find_peaks(quiet(600), 600) == []


def test_a_small_burst_in_a_dead_chat_is_not_a_peak():
    msgs = [Message(100 + i, f"u{i}", "lol") for i in range(2)]
    assert chat.find_peaks(msgs, 600) == []


def test_boost_raises_only_moments_the_chat_reacted_to():
    def mk(start, end, score):
        return Scored(Candidate(start=start, end=end, description="d", hook="h", title="t"), score, "x")

    a, b = mk(280, 320, 50.0), mk(400, 440, 50.0)
    n = chat.boost([a, b], [Peak(start=292, end=302, value=30, z=5.0, signal="chat")])
    assert n == 1 and a.score == 58.0 and b.score == 50.0
    c = mk(280, 320, 50.0)
    chat.boost([c], [Peak(start=292, end=302, value=99, z=40.0, signal="chat")])
    assert c.score == 50.0 + chat.MAX_BONUS


class Reply:
    status_code = 200

    def __init__(self, edges, more=True):
        self._d = [{"data": {"video": {"comments": {"edges": edges, "pageInfo": {"hasNextPage": more}}}}}]

    def json(self):
        return self._d


def edge(i, t, text="lol"):
    return {"cursor": str(i), "node": {"id": f"id{i}", "contentOffsetSeconds": t,
                                       "commenter": {"login": f"u{i}"},
                                       "message": {"fragments": [{"text": text}]}}}


def test_fetch_pages_by_offset_without_duplicates_and_moves_on_when_a_second_is_full(tmp_path):
    asked = []
    pages = {0: [edge(1, 1), edge(2, 5)],
             5: [edge(2, 5), edge(3, 5), edge(4, 5)],          # overlaps the last page; all in second 5
             6: [edge(5, 7)]}

    def post(body):
        off = body[0]["variables"]["contentOffsetSeconds"]
        asked.append(off)
        if off in pages:
            return Reply(pages[off], more=off != 6)
        return Reply([edge(9, 5), edge(10, 5), edge(11, 5)] if off == 5 else [], more=bool(off == 5))

    got = chat.fetch("77", tmp_path / "c.json", post=post, pause=0)
    assert [m.t for m in got] == [1, 5, 5, 5, 7] and len({m.who for m in got}) == 5
    assert asked[:3] == [0, 5, 6] and (tmp_path / "c.json").is_file()
    assert not (tmp_path / "c.part").exists() and not (tmp_path / "c.offset").exists()
    assert chat.fetch("77", tmp_path / "c.json", post=lambda b: 1 / 0) == got           # cached


def test_chat_that_is_not_available_is_no_signal_not_a_failed_job(tmp_path, monkeypatch):
    import dataclasses

    from clipforge.core import handlers
    from clipforge.core.config import get_settings

    class R:
        status_code = 200

        def json(self):
            return [{"data": {"video": {"comments": None}}}]

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    ctx = SimpleNamespace(settings=dataclasses.replace(get_settings(), data_dir=tmp_path), beat=lambda: None)
    row = {"id": 5, "url": "https://www.twitch.tv/videos/123", "duration": 600}
    assert handlers._chat_peaks(ctx, row) == []
    assert handlers._chat_peaks(ctx, {"id": 6, "url": "https://youtube.com/watch?v=x", "duration": 60}) == []
    (tmp_path / "chat").mkdir(exist_ok=True)
    (tmp_path / "chat" / "7.json").write_text(json.dumps(
        [[300 + i * 0.3, f"u{i}", "LMAO"] for i in range(12)] + [[t, "a", "hi"] for t in range(0, 600, 9)]))
    got = handlers._chat_peaks(ctx, {"id": 7, "url": "https://www.twitch.tv/videos/9", "duration": 600})
    assert got and got[0].signal == "chat"
