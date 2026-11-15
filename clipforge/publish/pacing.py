"""When a clip may go out (PRD section 8, Pacing). Pure functions, no I/O.

All times are the laptop's local clock, which is where Yaish set the slots.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

START_PER_DAY = 5


def parse_times(text: str) -> list[time]:
    out: list[time] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        h, _, m = part.partition(":")
        out.append(time(int(h), int(m or 0)))
    return sorted(set(out))


def effective_cap(setting: int, ceiling: int, first_post: datetime | None, now: datetime) -> int:
    """Start at 5 a day (the YouTube upload quota allows about 6), add one per full week since the first post, never above
    the dashboard setting or the hard ceiling."""
    weeks = 0 if first_post is None else max(0, (now - first_post).days // 7)
    return max(1, min(setting, ceiling, START_PER_DAY + weeks))


def _slots(day: datetime, times: list[time], cap: int) -> list[datetime]:
    return [datetime.combine(day.date(), t) for t in times[:cap]]


def is_due(now: datetime, times: list[time], cap: int, posted: list[datetime],
           min_gap_hours: float) -> bool:
    """True when a slot has passed that no post today has used, and the last post
    is at least `min_gap_hours` ago."""
    today = [p for p in posted if p.date() == now.date()]
    elapsed = [s for s in _slots(now, times, cap) if s <= now]
    if len(elapsed) <= len(today):
        return False
    last = max(posted, default=None)
    return last is None or now - last >= timedelta(hours=min_gap_hours)


def next_slot(now: datetime, times: list[time], cap: int, posted: list[datetime],
              min_gap_hours: float) -> datetime | None:
    """The next moment `is_due` will turn true (an approximation for display)."""
    if not times:
        return None
    today = [p for p in posted if p.date() == now.date()]
    last = max(posted, default=None)
    slots = _slots(now, times, cap)
    for i, s in enumerate(slots):
        if i >= len(today) and s >= now:
            return max(s, last + timedelta(hours=min_gap_hours)) if last else s
    if len(slots) > len(today) and slots and slots[-1] <= now:
        return now if is_due(now, times, cap, posted, min_gap_hours) else (
            last + timedelta(hours=min_gap_hours) if last else now)
    tomorrow = datetime.combine((now + timedelta(days=1)).date(), times[0])
    return tomorrow
