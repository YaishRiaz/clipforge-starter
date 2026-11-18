"""Chat reaction as a signal: where the audience laughed, gasped or hyped.

A stream's chat reacts a few seconds after something happens, so a burst of distinct chatters
(more so when they spam laughter or hype) points at a moment worth reading the transcript of.
It adds to the loudness / speech / scene signals; it does not replace them, and a stream with no
chat to download simply has this signal missing.

Twitch VOD chat is read from the same public web endpoint the Twitch site uses to replay it. Only
the message time, name and text are kept, in the work area; nothing is posted or sent anywhere.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .signals import Peak, Signal, peaks

log = logging.getLogger("clipforge.moments.chat")

GQL_URL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"          # the Twitch website's own public id
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

WINDOW = 6.0              # seconds per bucket
LAG = 8.0                 # chat answers an event about this late, so the moment sits before the burst
MIN_VALUE = 8.0           # about four chatters laughing together; less is noise, however quiet the stream
MAX_BONUS = 10.0

LAUGH = re.compile(
    r"\b(l+o+l+|lmao+|lmfao+|rofl|ha(ha)+|he(he)+|kek\w*|omegalul|lul\w*|icant|i can'?t|so funny|"
    r"hilarious|im crying|i'm crying)\b|[\U0001F602\U0001F923\U0001F480]", re.I)
HYPE = re.compile(
    r"\b(wtf|omg|no ?way|clip (it|that)|pog\w*|insane|crazy|holy|let'?s? ?go+|nuts|cracked|unreal|"
    r"sheesh|what the|can'?t believe|cant believe)\b|^w{1,4}$", re.I)


@dataclass(frozen=True)
class Message:
    t: float
    who: str
    text: str


def vod_id(url: str | None) -> str | None:
    m = re.search(r"twitch\.tv/(?:videos|[^/]+/v(?:ideo)?)/(\d+)", url or "")
    return m.group(1) if m else None


def kind(text: str) -> str:
    """'laugh', 'hype' or '' for one chat message."""
    text = text.strip()
    if LAUGH.search(text):
        return "laugh"
    return "hype" if HYPE.search(text) else ""


def _page(video_id: str, *, offset: int, post) -> dict:
    # Pages follow the video clock. The cursor form of this query needs an integrity token, which
    # a script has no business faking, so the offset form is used.
    body = [{"operationName": "VideoCommentsByOffsetOrCursor",
             "variables": {"videoID": video_id, "contentOffsetSeconds": offset},
             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": COMMENTS_HASH}}}]
    r = post(body)
    if r.status_code != 200:
        raise RuntimeError(f"chat page: HTTP {r.status_code}")
    data = r.json()
    video = (data[0] if isinstance(data, list) else data).get("data", {}).get("video")
    if not video or video.get("comments") is None:
        raise RuntimeError("chat is not available for this video")
    return video["comments"]


def fetch(video_id: str, dest: Path, *, duration: float | None = None, beat=None, post=None,
          max_pages: int = 20000, pause: float = 0.1) -> list[Message]:
    """Download a VOD's chat to `dest` (JSON). An interrupted download resumes where it stopped."""
    if dest.is_file():
        return load(dest)
    import httpx

    send = post or (lambda b: httpx.post(GQL_URL, json=b, headers={"Client-ID": CLIENT_ID}, timeout=30.0))
    part, mark = dest.with_suffix(".part"), dest.with_suffix(".offset")
    dest.parent.mkdir(parents=True, exist_ok=True)
    offset = int(mark.read_text(encoding="utf-8")) if mark.is_file() else 0
    seen: set[str] = set()
    done = False
    with part.open("a", encoding="utf-8") as out:
        for _ in range(max_pages):
            if beat:
                beat()
            comments = None
            for attempt in range(3):
                try:
                    comments = _page(video_id, offset=offset, post=send)
                    break
                except (httpx.HTTPError, RuntimeError) as exc:
                    if attempt == 2 or "not available" in str(exc):
                        raise
                    time.sleep(2 * (attempt + 1))
            edges = comments.get("edges") or []
            fresh = [e for e in edges if e["node"]["id"] not in seen]
            for e in fresh:
                n = e["node"]
                text = "".join(f.get("text") or "" for f in (n.get("message") or {}).get("fragments") or [])
                if text.strip():
                    who = (n.get("commenter") or {}).get("login") or "?"
                    out.write(json.dumps([n["contentOffsetSeconds"], who, text[:200]], ensure_ascii=False) + chr(10))
            out.flush()
            if not edges or not comments.get("pageInfo", {}).get("hasNextPage"):
                done = True
                break
            last = max(e["node"]["contentOffsetSeconds"] for e in edges)
            seen = {e["node"]["id"] for e in edges if e["node"]["contentOffsetSeconds"] == last}
            offset = last if last > offset else offset + 1          # a page inside one second must still move on
            if duration and offset > duration + 60:
                done = True
                break
            mark.write_text(str(offset), encoding="utf-8")
            time.sleep(pause)
    if not done:
        raise RuntimeError(f"chat is longer than {max_pages} pages; kept the partial download")
    rows = [json.loads(line) for line in part.read_text(encoding="utf-8").splitlines() if line.strip()]
    dest.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    part.unlink(missing_ok=True)
    mark.unlink(missing_ok=True)
    return [Message(*r) for r in rows]


def load(path: Path) -> list[Message]:
    return [Message(*r) for r in json.loads(path.read_text(encoding="utf-8"))]


def series(messages: list[Message], duration: float, *, window: float = WINDOW) -> Signal:
    """Per bucket: distinct chatters, plus two for each distinct chatter who laughed or hyped."""
    count = max(1, int(duration // window) + 1)
    talkers: list[set] = [set() for _ in range(count)]
    reactors: list[set] = [set() for _ in range(count)]
    for m in messages:
        i = int(m.t // window)
        if 0 <= i < count:
            talkers[i].add(m.who)
            if kind(m.text):
                reactors[i].add(m.who)
    values = [float(len(t) + 2 * len(r)) for t, r in zip(talkers, reactors)]
    return Signal("chat", window, [i * window for i in range(count)], values)


def _note(messages: list[Message], start: float, end: float) -> str:
    inside = [m for m in messages if start <= m.t < end]
    who = len({m.who for m in inside})
    kinds = Counter(kind(m.text) for m in inside if kind(m.text))
    words = Counter(w.lower() for m in inside if kind(m.text) for w in re.findall(r"[\w']+", m.text))
    top = ", ".join(w for w, _ in words.most_common(3))
    mood = "laughing" if kinds["laugh"] >= kinds["hype"] else "hyped"
    return f"{who} chatters in {end - start:.0f}s, mostly {mood}" + (f" ({top})" if top else "")


def find_peaks(messages: list[Message], duration: float, *, z: float = 2.0, top_n: int = 25) -> list[Peak]:
    """Chat bursts, moved back by LAG to where the event most likely happened."""
    sig = series(messages, duration)
    out = []
    for p in peaks(sig, z=z, min_gap=20.0, top_n=top_n):
        if p.value < MIN_VALUE:
            continue
        note = _note(messages, p.start, p.end)
        out.append(Peak(start=max(0.0, p.start - LAG), end=max(WINDOW, p.end - LAG),
                        value=p.value, z=p.z, signal="chat", note=note))
    return out


def boost(scored: list, chat_peaks: list[Peak]) -> int:
    """Raise the score of each moment the chat reacted to, by up to MAX_BONUS. Returns how many."""
    hit = 0
    for s in scored:
        best = max((p.z for p in chat_peaks if p.start < s.end and p.end > s.start), default=None)
        if best is None:
            continue
        s.score = round(s.score + min(MAX_BONUS, 3.0 + best), 1)
        hit += 1
    return hit
