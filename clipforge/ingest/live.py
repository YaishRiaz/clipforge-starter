"""Clip a stream while it is still live.

When a channel a joined campaign approves goes live, its chat is read (see `moments.irc`). A burst
of reaction marks a moment; the stretch around it is taken from the broadcast's own in-progress
VOD, transcribed, and offered to the same scoring and clip-making steps every other moment goes
through. When the stream ends, the finished VOD is mined in full as usual.

Kick chat cannot be read without Kick's developer API, so a live Kick stream is taken in rolling
stretches instead: every ROLL seconds the newest stretch of its in-progress VOD is transcribed and
its loudest, busiest moments are scored. A brief that limits footage to a date or a game (see
`BriefRules.footage_ok`) is checked against the stream before anything is taken.

Nothing is recorded wholesale: a multi-hour recording would not fit this laptop's disk, and the
VOD already holds every second. The gates are the usual ones: the campaign must be active and
joined, the channel must be one Yaish approved, and a VOD belongs to one campaign only.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ..core import db, queue
from ..core.config import Settings
from ..moments import irc
from ..moments.chat import CLIENT_ID, GQL_URL, Message
from ..moments.signals import Peak
from . import download, kick, sources
from .briefs import load_rules, set_window
from .rules import channel_root, normalize_url

log = logging.getLogger("clipforge.ingest.live")

JOB = "live_moment"
KV_STATE = "live_state"
SETTING = "setting_live_clips"
DEFAULT_CLIPS = 4         # clips made during one stream; 0 turns live watching off

POLL = 120.0              # how often each channel is asked whether it is live
MIN_GAP = 180.0           # between two moments of one stream
SETTLE = 75.0             # wait this long after a burst so the VOD holds the aftermath
BEFORE, AFTER = 60.0, 40.0    # the stretch transcribed around a burst
SLICE_HEIGHT = 360        # the stretch is only listened to, so the smallest picture will do
KEEP = 600.0              # chat kept in memory, in seconds
MAX_TRIES = 3
ROLL = 1200.0             # a live stream without chat is taken in stretches this long
ROLL_HEIGHT = 160         # ...listened to at the smallest picture Kick offers
PRIORITY_CLIPS = 15       # clips per stream for a priority campaign (core.priority)

_QUERY = ("query($login:String!){user(login:$login){stream{id createdAt game{name}} "
          "videos(first:3,type:ARCHIVE,sort:TIME){edges{node{id createdAt status}}}}}")


@dataclass(frozen=True)
class Stream:
    login: str
    stream_id: str
    started_at: float             # wall-clock, seconds
    vod_url: str | None           # the broadcast's own VOD while it is still being written
    games: tuple[str, ...] | None = None    # what it is playing now; None when unknown


def login_of(channel_url: str) -> str | None:
    """The watcher's key for a channel: the Twitch login, or 'kick:<slug>'."""
    root = channel_root(channel_url)
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)$", root)
    if m:
        return m.group(1).lower()
    m = re.search(r"kick\.com/([A-Za-z0-9_-]+)$", root)
    return f"kick:{m.group(1).lower()}" if m else None


def is_kick_key(key: str) -> bool:
    return key.startswith("kick:")


def any_stream_info(key: str) -> Stream | None:
    """Live status for a Twitch login or a 'kick:<slug>' key."""
    if is_kick_key(key):
        k = kick.stream_info(key[5:])
        if k is None:
            return None
        return Stream(login=key, stream_id=k.stream_id, started_at=k.started_at, vod_url=k.vod_url,
                      games=k.games)
    return stream_info(key)


def any_reader(key: str, on_message, stop: threading.Event) -> None:
    if is_kick_key(key):
        return                    # no chat for Kick; the stream is taken in stretches (ROLL)
    irc.read(key, on_message, stop)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def stream_info(login: str, *, post=None) -> Stream | None:
    """The channel's current broadcast, or None when it is offline."""
    if post is None:
        import httpx

        def post(body):
            return httpx.post(GQL_URL, json=body, headers={"Client-ID": CLIENT_ID}, timeout=30.0)
    r = post({"query": _QUERY, "variables": {"login": login}})
    if r.status_code != 200:
        raise RuntimeError(f"live status: HTTP {r.status_code}")
    user = ((r.json() or {}).get("data") or {}).get("user") or {}
    stream = user.get("stream")
    if not stream:
        return None
    game = (stream.get("game") or {}).get("name")
    started = _epoch(stream["createdAt"])
    vod = None
    for edge in (user.get("videos") or {}).get("edges") or []:
        node = edge.get("node") or {}
        # The archive being written right now: marked as recording, or begun with this broadcast.
        if node.get("status") == "RECORDING" or abs(_epoch(node["createdAt"]) - started) < 180:
            vod = f"https://www.twitch.tv/videos/{node['id']}"
            break
    return Stream(login=login, stream_id=str(stream["id"]), started_at=started, vod_url=vod,
                  games=(game,) if game else ())


def clips_per_stream(conn: sqlite3.Connection, campaign_id: int | None = None) -> int:
    try:
        n = max(0, int(db.kv_get(conn, SETTING, str(DEFAULT_CLIPS)) or DEFAULT_CLIPS))
    except ValueError:
        n = DEFAULT_CLIPS
    if n and campaign_id is not None:
        from ..core import priority

        if priority.is_priority(conn, campaign_id):
            return max(n, PRIORITY_CLIPS)
    return n


def channels(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """Twitch login -> (campaign id, channel) for every approved channel of a joined campaign.

    Two campaigns can approve the same creator; the earlier one gets the stream, as it does
    for VODs, because footage never crosses campaigns.
    """
    out: dict[str, tuple[int, str]] = {}
    for row in conn.execute(
            "SELECT id FROM campaigns WHERE status='active' AND joined_at IS NOT NULL "
            "AND platform != 'own' ORDER BY id").fetchall():
        rules = load_rules(conn, row["id"])
        for ch in (rules.approved_channels if rules else []):
            login = login_of(ch)
            if login and login not in out:
                out[login] = (int(row["id"]), ch)
    return out


def chat_log(settings: Settings, vod_url: str) -> Path:
    """Where a broadcast's live chat is kept once it ends, in VOD time."""
    vod = re.search(r"(\d+)$", vod_url).group(1)
    return settings.data_dir / "live" / f"vod-{vod}.json"


# --------------------------------------------------------------------------
# The watcher: one light thread per live channel, inside the dashboard process
# --------------------------------------------------------------------------

@dataclass
class _Live:
    stream: Stream
    campaign_id: int
    channel: str
    since: float
    path: Path
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    recent: deque = field(default_factory=deque)
    count: int = 0
    made: int = 0
    last_moment: float = 0.0
    misses: int = 0
    rolled_to: float = 0.0              # stream seconds already taken in stretches
    first_ok: float | None = None       # stream seconds when the brief first allowed the footage
    blocked: str | None = None          # why it is not allowed right now, logged once

    def add(self, t: float, who: str, text: str) -> None:
        with self.lock:
            self.recent.append(Message(t, who, text))
            while self.recent and self.recent[0].t < t - KEEP:
                self.recent.popleft()
            self.count += 1
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps([round(t, 2), who, text], ensure_ascii=False) + chr(10))

    def messages(self) -> list[Message]:
        with self.lock:
            return list(self.recent)


class Watcher:
    def __init__(self, settings: Settings, *, info=any_stream_info, reader=any_reader, clock=time.time):
        self.settings, self.info, self.reader, self.clock = settings, info, reader, clock
        self.live: dict[str, _Live] = {}
        self.polled = 0.0

    def step(self, conn: sqlite3.Connection) -> None:
        now = self.clock()
        if now - self.polled >= POLL:
            self.polled = now
            self._poll(conn, now)
        for lv in list(self.live.values()):
            self._check(conn, lv, now)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        conn = db.open_db(self.settings.db_path)
        while not stop.is_set():
            try:
                self.step(conn)
            except Exception as exc:                 # the dashboard must outlive a bad pass
                log.warning("live watch: %s: %s", type(exc).__name__, str(exc)[:200])
            stop.wait(5.0)
        for lv in self.live.values():
            lv.stop.set()

    def _poll(self, conn: sqlite3.Connection, now: float) -> None:
        wanted = channels(conn) if clips_per_stream(conn) > 0 else {}
        for login in set(wanted) | set(self.live):
            lv = self.live.get(login)
            try:
                stream = self.info(login) if login in wanted else None
            except Exception as exc:
                log.warning("live status %s: %s", login, type(exc).__name__)
                continue                              # unknown is not offline
            if stream is None or (lv and stream.stream_id != lv.stream.stream_id):
                if lv:
                    lv.misses += 1
                    if lv.misses >= 2 or login not in wanted or stream is not None:
                        self._end(conn, lv)
                continue
            if lv:
                lv.misses = 0
                if (lv.stream.vod_url is None and stream.vod_url) or stream.games != lv.stream.games:
                    lv.stream = replace(stream, vod_url=lv.stream.vod_url or stream.vod_url)
                continue
            folder = self.settings.data_dir / "live"
            folder.mkdir(parents=True, exist_ok=True)
            campaign_id, channel = wanted[login]
            lv = _Live(stream=stream, campaign_id=campaign_id, channel=channel, since=now,
                       path=folder / f"{login}-{stream.stream_id}.jsonl")
            self.live[login] = lv
            threading.Thread(target=self.reader, args=(login, lv.add, lv.stop), daemon=True,
                             name=f"chat-{login}").start()
            log.info("%s is live; reading chat%s", login,
                     "" if stream.vod_url else " (no VOD is being written, so nothing can be clipped yet)")
        self._publish(conn, now)

    def _eligible(self, conn: sqlite3.Connection, lv: _Live, now: float) -> bool:
        """Does the brief allow what is on stream right now? Records where its game began."""
        rules = load_rules(conn, lv.campaign_id)
        if rules is None:
            return False
        ok, why = rules.footage_ok(now, lv.stream.games)
        if not ok:
            if lv.blocked != why:
                log.info("%s: not clipping yet: %s", lv.stream.login, why)
                lv.blocked = why
            return False
        if lv.blocked is not None or lv.first_ok is None:
            lv.blocked = None
            if lv.first_ok is None:
                lv.first_ok = max(0.0, now - lv.stream.started_at)
                if (rules.footage_after or rules.footage_games) and lv.stream.vod_url:
                    from .channel import GAME_START

                    key = GAME_START + normalize_url(lv.stream.vod_url)
                    if not db.kv_get(conn, key):
                        db.kv_set(conn, key, f"{lv.first_ok:.1f}")
        return True

    def _check(self, conn: sqlite3.Connection, lv: _Live, now: float) -> None:
        if lv.made >= clips_per_stream(conn, lv.campaign_id) or not lv.stream.vod_url:
            return
        if not self._eligible(conn, lv, now):
            return
        if is_kick_key(lv.stream.login):
            self._roll(conn, lv, now)
            return
        if now - lv.last_moment < MIN_GAP:
            return
        peak = irc.spike(lv.messages(), now, lv.since)
        if peak is None:
            return
        lv.made += 1
        lv.last_moment = now
        offset = max(0.0, peak.start - lv.stream.started_at)
        queue.enqueue(conn, JOB, payload={
            "campaign_id": lv.campaign_id, "channel": lv.channel, "vod_url": lv.stream.vod_url,
            "offset": round(offset, 1), "value": peak.value, "z": peak.z, "note": peak.note,
        }, delay_seconds=SETTLE, dedupe=False, priority=10)
        log.info("%s: chat reacted at %.0fs into the stream (%s)", lv.stream.login, offset, peak.note)
        self._publish(conn, now)

    def _roll(self, conn: sqlite3.Connection, lv: _Live, now: float) -> None:
        """Take the newest whole stretch of a stream that has no chat to follow."""
        at = max(lv.rolled_to, lv.first_ok or 0.0)
        if now - lv.stream.started_at < at + ROLL + SETTLE:
            return
        lv.made += 1
        lv.rolled_to = at + ROLL
        lv.last_moment = now
        from ..core import priority

        queue.enqueue(conn, JOB, payload={
            "campaign_id": lv.campaign_id, "channel": lv.channel, "vod_url": lv.stream.vod_url,
            "offset": round(at, 1), "span": ROLL, "started_at": lv.stream.started_at,
            "note": "live stretch",
        }, dedupe=False, priority=priority.job_priority(conn, lv.campaign_id, 10))
        log.info("%s: taking %.0f-%.0fs of the live stream", lv.stream.login, at, at + ROLL)
        self._publish(conn, now)

    def _end(self, conn: sqlite3.Connection, lv: _Live) -> None:
        lv.stop.set()
        self.live.pop(lv.stream.login, None)
        log.info("%s went offline after %d chat message(s), %d moment(s)",
                 lv.stream.login, lv.count, lv.made)
        try:
            finish(conn, self.settings, lv.stream, lv.path)
        except Exception as exc:
            log.warning("live handover %s: %s", lv.stream.login, type(exc).__name__)

    def _publish(self, conn: sqlite3.Connection, now: float) -> None:
        db.kv_set(conn, KV_STATE, json.dumps({"at": now, "live": [
            {"login": lv.stream.login, "campaign_id": lv.campaign_id, "since": lv.since,
             "messages": lv.count, "moments": lv.made, "vod": bool(lv.stream.vod_url)}
            for lv in self.live.values()]}))


def state(conn: sqlite3.Connection, *, now: float | None = None) -> list[dict]:
    """Channels being watched right now, for the dashboard. Empty when the watcher is not running."""
    try:
        data = json.loads(db.kv_get(conn, KV_STATE, "") or "{}")
    except ValueError:
        return []
    if (now or time.time()) - float(data.get("at") or 0) > 3 * POLL:
        return []
    return list(data.get("live") or [])


def finish(conn: sqlite3.Connection, settings: Settings, stream: Stream, log_path: Path) -> int | None:
    """The stream ended: keep its chat in VOD time, and hand a live-clipped VOD to full mining."""
    if not stream.vod_url:
        return None
    if log_path.is_file() and not is_kick_key(stream.login):
        rows = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            t, who, text = json.loads(line)
            if t >= stream.started_at:
                rows.append([round(t - stream.started_at, 2), who, text])
        dest = chat_log(settings, stream.vod_url)
        dest.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        log_path.unlink(missing_ok=True)
    row = conn.execute("SELECT id FROM sources WHERE url=? AND live=1",
                       (normalize_url(stream.vod_url),)).fetchone()
    if row is None:
        return None                                   # nothing was clipped live; the VOD scan finds it
    # The partial transcript covered only the moments clipped live. The whole VOD is mined now,
    # from where the brief's footage begins when it limits the date or the game.
    from .channel import Vod, eligible_from

    cid = conn.execute("SELECT campaign_id FROM sources WHERE id=?", (row["id"],)).fetchone()[0]
    rules = load_rules(conn, cid)
    if rules is not None and (rules.footage_after or rules.footage_games):
        start = eligible_from(conn, rules, Vod(url=stream.vod_url, title="", duration=None,
                                               started_at=stream.started_at))
        set_window(conn, cid, stream.vod_url, start)
    conn.execute("UPDATE sources SET status='new', live=0, transcript_path=NULL WHERE id=?", (row["id"],))
    queue.enqueue(conn, "ingest_source", ref_id=row["id"])
    return int(row["id"])


# --------------------------------------------------------------------------
# One burst of chat becomes a candidate moment (the live_moment job)
# --------------------------------------------------------------------------

STRETCH_PEAKS = 4


def _stretch_peaks(got, heard) -> list[Peak]:
    """The loudest and busiest-talking moments of a stretch, in source time."""
    from ..moments import signals as sig

    found: list[Peak] = []
    try:
        loud = sig.loudness_series(got.path)
        found += [replace(p, start=p.start + got.offset, end=p.end + got.offset)
                  for p in sig.peaks(loud, z=1.8, min_gap=20.0, top_n=STRETCH_PEAKS)]
    except Exception as exc:  # noqa: BLE001 - the talk alone still gives peaks
        log.info("live stretch: no loudness (%s)", type(exc).__name__)
    found += list(sig.peaks(sig.speech_rate_series(heard, duration=heard[-1].end), z=1.8,
                            min_gap=20.0, top_n=2))
    found.sort(key=lambda p: p.z, reverse=True)
    return sorted(found[:STRETCH_PEAKS + 1], key=lambda p: p.start)


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    shared = min(a_end, b_end) - max(a_start, b_start)
    return shared > 0.5 * min(a_end - a_start, b_end - b_start)


def make_moment(conn: sqlite3.Connection, settings: Settings, p: dict, *, beat=None,
                fetch=None, transcribe=None, propose=None) -> list[int]:
    """Listen to the stretch around a burst and record what is worth clipping. Returns moment ids."""
    from ..edit import hls
    from ..moments import chat as chat_mod
    from ..moments import rubric as rubric_mod
    from ..moments import score as score_mod
    from ..moments import transcribe as tr
    from ..core import llm
    from . import captions as caps
    from . import tagger

    beat = beat or (lambda: None)
    cid, url, at = int(p["campaign_id"]), normalize_url(p["vod_url"]), float(p["offset"])
    span = float(p.get("span") or 0)
    ok, why = sources.campaign_may_ingest(conn, cid)
    rules = load_rules(conn, cid)
    if not ok or rules is None or not rules.approves_channel(p["channel"]):
        log.warning("live moment refused: %s", why or "the channel is not approved for this campaign")
        return []
    if p.get("started_at") is not None:
        fine, why = rules.footage_ok(float(p["started_at"]) + at, None)
        if not fine:
            log.warning("live moment refused: %s", why)
            return []
    known = conn.execute("SELECT id, campaign_id, live FROM sources WHERE url=?", (url,)).fetchone()
    if known and (known["campaign_id"] != cid or not known["live"]):
        return []                 # another campaign's footage, or the stream ended and it is being mined whole
    if known:
        sid = int(known["id"])
    else:
        try:
            sid = sources.register(conn, cid, url=url, title=p.get("title") or "Live stream", rules=rules,
                                   media_kind="audio", from_channel=p["channel"], length_approved=True)
        except sources.NotApproved as exc:
            log.warning("live moment refused: %s", exc)
            return []
        conn.execute("UPDATE sources SET status='mined', live=1 WHERE id=?", (sid,))

    work = settings.data_dir / "work" / f"live-{sid}-{int(at)}"
    sources.check_disk(settings)
    try:
        lo_t, hi_t = (at, at + span) if span else (max(0.0, at - BEFORE), at + AFTER)
        got = (fetch or hls.fetch_section)(url, lo_t, hi_t, work / "slice.mp4",
                                           max_height=ROLL_HEIGHT if span else SLICE_HEIGHT, beat=beat)
    except (hls.SectionError, download.DownloadError) as exc:
        tries = int(p.get("tries") or 0) + 1
        if tries < MAX_TRIES:                         # the VOD trails the broadcast; ask again shortly
            queue.enqueue(conn, JOB, payload={**p, "tries": tries}, delay_seconds=120.0,
                          dedupe=False, priority=10)
        else:
            log.warning("live moment at %.0fs dropped: %s", at, exc)
        return []
    try:
        try:
            segments, _backend = (transcribe or tr.transcribe)(got.path, settings, work_dir=work / "tr",
                                                              beat=beat)
        except tr.TranscriptionUnavailable as exc:
            log.warning("live moment at %.0fs: cannot transcribe (%s)", at, exc)
            return []
        heard = [caps.Segment(s.start + got.offset, s.end + got.offset, s.text) for s in segments]
        if not heard:
            return []
        lo, hi = heard[0].start, heard[-1].end
        dest = tagger.transcript_path(settings, sid)
        old = [s for s in (caps.load(dest) if dest.is_file() else []) if s.end <= lo or s.start >= hi]
        caps.save(sorted(old + heard, key=lambda s: s.start), dest, source="live")
        conn.execute("UPDATE sources SET transcript_path=? WHERE id=?", (str(dest), sid))

        if span:
            peaks = _stretch_peaks(got, heard)
            if not peaks:
                log.info("live stretch at %.0fs: nothing stood out", at)
                return []
        else:
            peaks = [Peak(start=at, end=at + irc.WINDOW, value=float(p.get("value") or 0),
                          z=float(p.get("z") or 0), signal="chat", note=p.get("note") or "")]
        campaign = sources.campaign_of(conn, sid)
        rubric = rubric_mod.for_campaign(settings.rubrics_dir, campaign["category"] if campaign else None)
        try:
            chosen = (propose or score_mod.propose)(heard, peaks, rubric, rules=rules, duration=hi,
                                                    settings=settings, beat=beat)
        except (llm.LLMUnavailable, llm.LLMQuotaExhausted) as exc:
            log.warning("live moment at %.0fs: cannot score (%s)", at, exc)
            return []
        if not span:
            chat_mod.boost(chosen, peaks)
        chosen = [c for c in chosen if rules.inside_window(url, c.start, c.end) and c.start >= lo_t]
        taken = conn.execute('SELECT start, "end" FROM moments WHERE source_id=?', (sid,)).fetchall()
        fresh = [c for c in chosen
                 if not any(_overlaps(c.start, c.end, t["start"], t["end"]) for t in taken)]
        ids = []
        for rank, item in enumerate(sorted(fresh, key=lambda s: s.score, reverse=True)):
            cur = conn.execute(
                'INSERT INTO moments (source_id, start, "end", description, hook, title, score, '
                "rubric_category, status, live) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (sid, item.start, item.end, item.candidate.description, item.candidate.hook,
                 item.candidate.title, item.score, item.rubric_category,
                 "candidate" if rank == 0 else "dropped"))
            ids.append(int(cur.lastrowid))
        if ids:
            from ..core import priority

            queue.enqueue(conn, "make_clip", ref_id=ids[0], priority=priority.job_priority(conn, cid, 10))
            log.info("live moment at %.0fs: moment %s is being clipped", at, ids[0])
        else:
            log.info("live moment at %.0fs: nothing there worth a clip", at)
        return ids
    finally:
        shutil.rmtree(work, ignore_errors=True)
