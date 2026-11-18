"""A live Twitch chat, read as it happens.

Twitch lets anyone read a public chat over IRC without an account: a nick of the form
`justinfan<digits>` joins read-only and can never send. That is all this does. It keeps the
time, name and text of each message so `spike` can tell when the audience reacts.
"""
from __future__ import annotations

import logging
import random
import re
import socket
import ssl
import statistics
import threading
import time
from typing import Callable

from .chat import LAG, Message, kind
from .signals import Peak

log = logging.getLogger("clipforge.moments.irc")

HOST, PORT = "irc.chat.twitch.tv", 6697
SILENCE = 400.0           # Twitch pings about every five minutes; longer than this is a dead link
WINDOW = 10.0             # seconds per bucket while live
BASELINE = 300.0          # a burst is judged against the five minutes before it
FACTOR = 3.0              # ...and must be this many times busier
WARMUP = 120.0            # history needed before anything can be called a burst
MIN_VALUE = 12.0          # about five chatters reacting together inside ten seconds; less is noise

_PRIVMSG = re.compile(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")


def parse(line: str) -> tuple[str, str] | None:
    """(who, text) for a chat message, None for every other IRC line."""
    m = _PRIVMSG.match(line)
    if not m:
        return None
    text = m.group(2)
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):      # /me
        text = text[8:-1]
    return m.group(1), text


def _connect() -> socket.socket:
    raw = socket.create_connection((HOST, PORT), timeout=30)
    return ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)


def read(login: str, on_message: Callable[[float, str, str], None], stop: threading.Event, *,
         connect: Callable[[], socket.socket] = _connect, clock: Callable[[], float] = time.time,
         pause: float = 5.0) -> None:
    """Join #login read-only and hand each message to on_message(time, who, text) until stop is set."""
    wait = pause
    while not stop.is_set():
        try:
            sock = connect()
        except OSError as exc:
            log.warning("chat %s: cannot connect (%s)", login, type(exc).__name__)
            stop.wait(wait)
            wait = min(wait * 2, 120.0)
            continue
        try:
            sock.settimeout(5.0)
            sock.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{login.lower()}\r\n".encode())
            buf, heard = b"", clock()
            while not stop.is_set():
                try:
                    data = sock.recv(4096)
                except (socket.timeout, TimeoutError):
                    if clock() - heard > SILENCE:
                        raise OSError("no data from Twitch chat")
                    continue
                if not data:
                    raise OSError("Twitch closed the chat connection")
                heard, wait = clock(), pause
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw_line in lines:
                    line = raw_line.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(b"PONG" + raw_line[4:] + b"\r\n")
                        continue
                    msg = parse(line)
                    if msg:
                        on_message(clock(), msg[0], msg[1][:200])
        except OSError as exc:
            if not stop.is_set():
                log.info("chat %s: reconnecting (%s)", login, exc)
                stop.wait(wait)
                wait = min(wait * 2, 120.0)
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _value(messages: list[Message], start: float, end: float) -> float:
    talkers = {m.who for m in messages if start <= m.t < end}
    reactors = {m.who for m in messages if start <= m.t < end and kind(m.text)}
    return float(len(talkers) + 2 * len(reactors))


def spike(messages: list[Message], now: float, since: float) -> Peak | None:
    """The last full ten seconds, if chat was at least three times busier than the five minutes before.

    Times are wall-clock. The peak is moved back by LAG, to where the event most likely happened.
    `since` is when reading began: with too little history there is nothing to compare against.
    """
    end = (now // WINDOW) * WINDOW
    start = end - WINDOW
    first = max(since, start - BASELINE)
    if start - first < WARMUP:
        return None
    value = _value(messages, start, end)
    if value < MIN_VALUE:
        return None
    before = []
    t = start - WINDOW
    while t >= first:
        before.append(_value(messages, t, t + WINDOW))
        t -= WINDOW
    mean = statistics.fmean(before)
    if value < FACTOR * max(mean, 1.0):
        return None
    spread = max(statistics.pstdev(before), 1.0)
    inside = [m for m in messages if start <= m.t < end]
    laughs = sum(1 for m in inside if kind(m.text) == "laugh")
    hypes = sum(1 for m in inside if kind(m.text) == "hype")
    mood = "laughing" if laughs >= hypes else "hyped"
    note = f"{len({m.who for m in inside})} chatters in {WINDOW:.0f}s, mostly {mood}, live"
    return Peak(start=start - LAG, end=end - LAG, value=value, z=round((value - mean) / spread, 2),
                signal="chat", note=note)
