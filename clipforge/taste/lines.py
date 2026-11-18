"""Jump cuts: drop filler lines and dead air from a clip, when the style asks for it.

The words of the clip are grouped into spoken lines. A small model marks each line KEEP or
DELETE (false starts, repeats, filler, off-topic asides); long silences are shortened without a
model. Guard rails are plain code: the first line (the hook) and the last (the payoff) are never
deleted, no more than a quarter of the clip may go, and the result must still meet the minimum
length. The output is a list of (start, end) spans of the clip to keep, in clip seconds.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ..core import llm, media
from ..core.config import Settings

log = logging.getLogger("clipforge.taste.lines")

LINE_GAP = 0.45          # a pause this long ends a line
LONG_PAUSE = 1.2         # a silence longer than this is cut out, leaving only the padding
PAD_BEFORE, PAD_AFTER = 0.10, 0.18
MAX_CUT_SHARE = 0.25
MIN_SAVING = 0.6         # cutting less than this is not worth a splice
FADE = 0.02


@dataclass(frozen=True)
class Line:
    start: float
    end: float
    text: str


class Verdict(BaseModel):
    n: int
    verdict: str = Field(description="KEEP or DELETE")
    reason: str = ""


class Verdicts(BaseModel):
    lines: list[Verdict] = Field(default_factory=list)


SYSTEM = """You are a short-form video editor trimming a clip of a streamer. You get the clip's \
spoken lines, numbered. Mark each line KEEP or DELETE. DELETE only a line that adds nothing: a false \
start that is restarted right after, a line repeated word for word, pure filler ("um", "uh", "you \
know") or an aside unrelated to the moment. KEEP anything that sets up or lands the joke, the \
reaction, the story or the skill play; when unsure, KEEP. Line 1 and the last line are always KEEP. \
Answer with one verdict per line."""


def group(words, gap: float = LINE_GAP) -> list[Line]:
    lines: list[Line] = []
    cur: list = []
    for w in words:
        if cur and (w[0] - cur[-1][1] >= gap or cur[-1][2].rstrip()[-1:] in ".?!"):
            lines.append(Line(cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
            cur = []
        cur.append(w)
    if cur:
        lines.append(Line(cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
    return lines


def spans(lines: list[Line], deleted: set[int], duration: float) -> list[tuple[float, float]]:
    """Parts of the clip to keep: every kept line, with long silences between them shortened."""
    kept = [(i, l) for i, l in enumerate(lines) if i not in deleted]
    if not kept:
        return [(0.0, duration)]
    out: list[list[float]] = []
    prev_i = None
    for i, l in kept:
        s, e = max(0.0, l.start - PAD_BEFORE), min(duration, l.end + PAD_AFTER)
        if out and prev_i == i - 1 and s - out[-1][1] <= LONG_PAUSE:
            out[-1][1] = e                     # neighbours with a natural pause stay joined
        else:
            out.append([s, e])                 # a deleted line or a long silence between: splice here
        prev_i = i
    out[-1][1] = min(duration, out[-1][1] + 0.2)
    merged: list[list[float]] = []
    for s, e in out:
        if merged and s <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in merged if e - s > 0.15]


def plan(words, duration: float, settings: Settings, *, min_seconds: float = 5.0, ask=None):
    """Spans to keep, or None when nothing worth cutting was found (or the judge is unavailable)."""
    lines = group(words)
    if len(lines) < 3:
        return None
    deleted: set[int] = set()
    try:
        numbered = "\n".join(f"{i + 1}. {l.text}" for i, l in enumerate(lines))
        answer = (ask or (lambda u: llm.parse(Verdicts, SYSTEM, u, model=llm.model_for(settings, "scoring"),
                                              settings=settings, max_tokens=600)))(numbered)
        for v in answer.lines:
            if v.verdict.strip().upper() == "DELETE" and 1 < v.n < len(lines):
                deleted.add(v.n - 1)
    except (llm.LLMUnavailable, llm.LLMQuotaExhausted, llm.LLMBadAnswer, llm.LLMRequestTooLarge) as exc:
        log.warning("line judge unavailable, only long pauses are cut: %s", exc)
    # Never take out more than the share allowed: drop the shortest deletions first from the list.
    while deleted and sum(lines[i].end - lines[i].start for i in deleted) > MAX_CUT_SHARE * duration:
        deleted.discard(min(deleted, key=lambda i: lines[i].end - lines[i].start))
    keep = spans(lines, deleted, duration)
    kept_len = sum(e - s for s, e in keep)
    if duration - kept_len < MIN_SAVING or kept_len < min_seconds:
        return None
    return keep


def remap(words, keep: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """Words on the new clock; words inside a removed part are dropped."""
    out = []
    for w in words:
        offset = 0.0
        for s, e in keep:
            if s - 0.02 <= w[0] < e:
                out.append((w[0] - s + offset, min(w[1], e) - s + offset, w[2]))
                break
            offset += e - s
    return out


def render_tight(footage: Path, *, ss: float, keep: list[tuple[float, float]], dest: Path,
                 timeout: float = 1800) -> Path:
    """The kept parts of the footage joined into one file (near-lossless; the final render follows)."""
    inputs, parts, joined = [], [], []
    for i, (s, e) in enumerate(keep):
        dur = e - s
        inputs += ["-ss", f"{ss + s:.3f}", "-t", f"{dur:.3f}", "-i", str(footage)]      # seek per part: footage may be hours long
        parts.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}];"
                     f"[{i}:a]asetpts=PTS-STARTPTS,afade=t=in:d={FADE},"
                     f"afade=t=out:st={max(0.0, dur - FADE):.3f}:d={FADE}[a{i}]")
        joined.append(f"[v{i}][a{i}]")
    graph = ";".join(parts) + ";" + "".join(joined) + f"concat=n={len(keep)}:v=1:a=1[v][a]"
    cmd = [media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise media.MediaError(f"ffmpeg failed joining the kept parts: {(proc.stderr or '')[-800:]}")
    return dest
