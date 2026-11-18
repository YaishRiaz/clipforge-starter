"""Who is on screen and what they are playing, so a clip can use that streamer's and that game's
own taste book and rubric on top of the campaign's niche.

The streamer is the source's creator. The game comes from Twitch itself: a VOD records its game,
and a stream that switches games carries a chapter marker for each switch, so a moment at
1:20:00 knows it was during the second game. Nothing is guessed: a source with no such record
(a YouTube video, a local file) simply has no game, and the niche book and rubric apply alone.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from ..moments.chat import CLIENT_ID, GQL_URL, vod_id

log = logging.getLogger("clipforge.taste.subjects")

_QUERY = ('query($id:ID!){video(id:$id){lengthSeconds owner{login displayName} game{displayName} '
          'moments(momentRequestType:VIDEO_CHAPTER_MARKERS, first:50){edges{node{positionMilliseconds '
          'durationMilliseconds type details{... on GameChangeMomentDetails{game{displayName}}}}}}}}')


def slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def streamer_key(name: str | None) -> str | None:
    s = slug(name)
    return f"streamer-{s}" if s else None


_CANON = {"grandtheftautovi": "Grand Theft Auto VI", "grandtheftautov": "Grand Theft Auto V"}


def game_key(name: str | None) -> str | None:
    from ..ingest.rules import _game

    name = _CANON.get(_game(name or ""), name)       # 'GTA 6' and 'GTA VI' share one book
    s = slug(name)
    return f"game-{s}" if s and s not in ("just-chatting", "irl") else None


def queries(key: str, name: str) -> tuple[str, ...]:
    """Searches for the most-viewed Shorts about this streamer or game."""
    if key.startswith("streamer-"):
        return (f"{name} shorts", f"{name} funny moments", f"{name} clips")
    return (f"{name} funny moments", f"{name} shorts", f"{name} clips")


def fetch_twitch(video_id: str, *, post=None) -> dict:
    """{"streamer": display name, "games": [[start, end, game], ...]} for a Twitch VOD."""
    if post is None:
        import httpx

        def post(body):
            return httpx.post(GQL_URL, json=body, headers={"Client-ID": CLIENT_ID}, timeout=30.0)
    r = post({"query": _QUERY, "variables": {"id": video_id}})
    if r.status_code != 200:
        raise RuntimeError(f"twitch video info: HTTP {r.status_code}")
    v = ((r.json() or {}).get("data") or {}).get("video")
    if not v:
        raise RuntimeError("twitch has no such video")
    length = float(v.get("lengthSeconds") or 0)
    spans = []
    for edge in ((v.get("moments") or {}).get("edges") or []):
        n = edge.get("node") or {}
        game = (((n.get("details") or {}).get("game")) or {}).get("displayName")
        if n.get("type") == "GAME_CHANGE" and game:
            start = (n.get("positionMilliseconds") or 0) / 1000
            spans.append([start, start + (n.get("durationMilliseconds") or 0) / 1000, game])
    main = (v.get("game") or {}).get("displayName")
    if not spans and main:
        spans = [[0.0, length, main]]
    return {"streamer": (v.get("owner") or {}).get("displayName"), "games": sorted(spans)}


def fetch_kick(url: str, creator: str | None, *, lister=None) -> dict:
    """Kick lists a broadcast's categories without times. The one game that is not 'Just
    Chatting' is taken for the whole VOD; a brief's window already cuts away the rest."""
    from ..ingest import kick

    vods = (lister or kick.list_vods)(f"https://kick.com/{kick.slug_of(url)}")
    v = next((x for x in vods if kick.vod_uuid(x.url) == kick.vod_uuid(url)), None)
    games = [g for g in (v.games if v else ()) if game_key(g)]
    spans = [[0.0, (v.duration if v and v.duration else 1e9), games[-1]]] if games else []
    return {"streamer": creator or kick.slug_of(url), "games": spans}


def game_at(spans: list, start: float, end: float) -> str | None:
    """The game played for most of [start, end]."""
    best, most = None, 0.0
    for s, e, game in spans or []:
        shared = min(end, e) - max(start, s)
        if shared > most:
            best, most = game, shared
    return best


def of_source(conn: sqlite3.Connection, row, *, fetch=fetch_twitch) -> dict:
    """The source's streamer and game spans, read once and kept on the row."""
    if row["games_json"]:
        try:
            return json.loads(row["games_json"])
        except ValueError:
            pass
    info = {"streamer": row["creator"], "games": []}
    vid = vod_id(row["url"])
    from ..ingest import kick

    if kick.is_kick(row["url"]):
        try:
            info = fetch_kick(row["url"], row["creator"])
        except Exception as exc:                    # no game is better than a stopped job
            log.warning("source %s: no game information (%s)", row["id"], exc)
            return info
    elif vid:
        try:
            got = fetch(vid)
            info = {"streamer": got["streamer"] or row["creator"], "games": got["games"]}
        except Exception as exc:                    # no game is better than a stopped job
            log.warning("source %s: no game information (%s)", row["id"], exc)
            return info
    conn.execute("UPDATE sources SET games_json=? WHERE id=?", (json.dumps(info), row["id"]))
    return info


def for_moment(conn: sqlite3.Connection, moment) -> tuple[str | None, str | None]:
    """(streamer key, game key) for a moment, from its source and its own recorded game."""
    row = conn.execute("SELECT * FROM sources WHERE id=?", (moment["source_id"],)).fetchone()
    if row is None:
        return None, None
    info = of_source(conn, row)
    game = moment["game"] if "game" in moment.keys() and moment["game"] else \
        game_at(info["games"], moment["start"], moment["end"])
    return streamer_key(info.get("streamer")), game_key(game)
