"""Where a clip really starts and ends (PRD section 7: cut point discipline).

The model gives times to the nearest guess. A clip should open on the first word
of a sentence and stop on the payoff, so the edges snap to transcript phrase
boundaries when one is close, and never when snapping would break a brief's
minimum length.
"""
from __future__ import annotations

PRE_ROLL = 0.15
START_WINDOW = (-2.5, 1.5)     # a phrase start this far before/after the guess may be used
END_WINDOW = (-1.5, 2.5)
TAIL = 0.35


def snap(segments, start: float, end: float, *, min_seconds: float | None = None,
         max_seconds: float = 59.0) -> tuple[float, float]:
    new_start, new_end = start, end

    starts = [s.start for s in segments
              if start + START_WINDOW[0] <= s.start <= start + START_WINDOW[1]]
    if starts:
        # An earlier phrase wins a tie so the hook sentence is whole.
        new_start = max(0.0, min(starts, key=lambda s: (abs(s - start), s)) - PRE_ROLL)

    ends = [s.end for s in segments if end + END_WINDOW[0] <= s.end <= end + END_WINDOW[1]]
    if ends:
        new_end = min(ends, key=lambda e: (abs(e - end), e)) + TAIL

    length = new_end - new_start
    if new_end <= new_start or (min_seconds is not None and length < min_seconds):
        new_start, new_end = start, end
        length = end - start
    if length > max_seconds:
        new_end = new_start + max_seconds
    return round(new_start, 2), round(new_end, 2)
