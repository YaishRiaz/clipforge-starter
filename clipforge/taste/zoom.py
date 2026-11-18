"""Where a punch-in is allowed to happen.

Trigger: a sudden rise in the clip's own audio level (a shout, an explosion, a
crash). It is measured on the clip's audio, so a loud stream and a quiet one
both get their own peaks. Nothing here guesses at what is on screen.
"""
from __future__ import annotations

from pathlib import Path

from ..moments import signals
from .style import Zoom

LEAD_IN = 0.10       # start growing just before the peak so the cut feels motivated
HEAD_FREE = 1.0      # leave the opening to the hook
TAIL_FREE = 1.2


def plan(audio: Path, duration: float, cfg: Zoom) -> list[tuple[float, float]]:
    """(start, length) pairs in clip seconds, strongest peaks first, returned in time order."""
    if not cfg.enabled or duration < HEAD_FREE + TAIL_FREE + cfg.seconds:
        return []
    series = signals.loudness_series(audio, window=0.25)
    found = signals.peaks(series, z=cfg.z, min_gap=cfg.min_gap, top_n=cfg.max_events * 2)
    picks: list[tuple[float, float]] = []
    for p in sorted(found, key=lambda p: -p.z):
        start = max(0.0, p.start - LEAD_IN)
        if start < HEAD_FREE or start + cfg.seconds > duration - TAIL_FREE:
            continue
        if all(abs(start - s) >= cfg.min_gap for s, _ in picks):
            picks.append((round(start, 2), cfg.seconds))
        if len(picks) >= cfg.max_events:
            break
    return sorted(picks)
