"""Tagging a source video (PRD section 5, "Tagging").

Every source ends up with: campaign, creator, title, original URL, duration,
publish date, language, a 3-5 sentence summary of what happens, and chapters
with timestamps.

Duration and language come from ffprobe. The summary and chapters need to know
what is said, so they come from the LLM reading the transcript. When no captions
came with the footage, the summary waits for Phase 2 to transcribe it rather
than being guessed from the title.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from pydantic import Field

from ..core import llm, media
from ..core.schema import LLMModel
from ..core.config import Settings, get_settings
from . import captions as caps

log = logging.getLogger("clipforge.ingest.tagger")

# How much transcript one request may carry. A provider limit, not a cost
# preference: Groq's free tier allows 8,000 tokens a MINUTE, and it counts
# `max_tokens` against that budget as well as the prompt. A five-hour stream
# renders to about 180,000 characters, so truncating to fit would summarise a
# fraction of it and call it the whole thing.
#
# Measured directly against the API on 13 Nov 2026, sending a real window:
# 4,827 characters of prompt came to 1,619 tokens (about 3 characters a token)
# and the whole request cost 2,002 tokens. So roughly four of these fit in a
# minute. The earlier "22,371 tokens requested" refusals were NOT the transcript
# -- they were the prompt plus a 16,000-token max_tokens reservation, which is
# now capped per call site. 9,000 characters is about 3,000 prompt tokens, and
# `_digest` halves anything the provider still refuses, so this only has to be
# close.
MAX_TRANSCRIPT_CHARS = 9_000

#: PRD section 5 asks for a chapter list; more than this is unreadable.
MAX_CHAPTERS = 12

#: Output ceilings. On a per-minute token budget the answer costs as much as the
#: question, and a window digest is a sentence plus a few chapters, not a
#: 16,000-token essay. Measured 13 Nov 2026: leaving the default cost about
#: 1,900 output tokens a window, which against 8,000 tokens a minute allowed
#: roughly one request a minute and turned 44 windows into an hour of rate
#: limiting with three windows lost outright.
WINDOW_ANSWER_TOKENS = 1200
REDUCE_ANSWER_TOKENS = 900

SYSTEM = """You describe a source video for a clipping pipeline's records, from its \
transcript.

Write for someone deciding which part of the video to clip, who has not watched it:

- summary: 3 to 5 sentences on what actually happens, in order. Name what is done and said, \
not how good it is. No marketing language, no "in this video".
- chapters: the natural sections, in order, each with the second it starts and a short label. \
Base every start on the transcript's timestamps. Six to twelve chapters for a long video, \
fewer for a short one. Do not invent a chapter for a section the transcript does not cover.
- language: the ISO code of the language spoken.

If the transcript is too sparse to describe, say so in the summary rather than filling it in."""


class Chapter(LLMModel):
    start: float = Field(description="Seconds from the start of the video.")
    title: str


class SourceTags(LLMModel):
    summary: str
    chapters: list[Chapter] = Field(default_factory=list)
    language: str | None = None


REDUCE_SYSTEM = """You are given summaries of consecutive parts of one long video, in \
order, each with the time it covers.

Write the overall record: 3 to 5 sentences on what happens across the whole video, in \
order, for someone deciding which part to clip. Name what is done and said. No marketing \
language, no "in this video". Also give the ISO code of the language spoken.

Do not describe the parts one by one. Say what the video is."""


class Overall(LLMModel):
    summary: str
    language: str | None = None


def split_by_chars(segments: list[caps.Segment], budget: int) -> list[list[caps.Segment]]:
    """Group segments into windows whose rendered text fits the budget."""
    windows: list[list[caps.Segment]] = []
    current: list[caps.Segment] = []
    size = 0
    for segment in segments:
        cost = len(segment.text) + 14          # the timestamp prefix
        if current and size + cost > budget:
            windows.append(current)
            current, size = [], 0
        current.append(segment)
        size += cost
    if current:
        windows.append(current)
    return windows


def _sample(windows: list[list[caps.Segment]], most: int) -> tuple[list[list[caps.Segment]], int]:
    """At most `most` windows, spread evenly across the whole transcript.

    Always keeps the first and last, so the record covers how the stream opened
    and how it ended rather than a random middle.
    """
    total = len(windows)
    if most <= 0 or total <= most:
        return windows, total
    step = (total - 1) / (most - 1)
    picked = sorted({min(total - 1, round(i * step)) for i in range(most)})
    return [windows[i] for i in picked], total


def thin_chapters(chapters: list[Chapter], *, duration: float | None,
                  most: int = MAX_CHAPTERS) -> list[Chapter]:
    """Keep at most `most` chapters, spaced out rather than clustered."""
    ordered = sorted(chapters, key=lambda c: c.start)
    if len(ordered) <= most:
        return ordered
    span = (duration or (ordered[-1].start or 1.0))
    gap = span / most
    kept: list[Chapter] = []
    for chapter in ordered:
        if not kept or chapter.start - kept[-1].start >= gap:
            kept.append(chapter)
    return kept[:most]


def describe(
    segments: list[caps.Segment],
    *,
    title: str | None = None,
    duration: float | None = None,
    call: llm.Caller | None = None,
    settings: Settings | None = None,
    beat=None,
) -> SourceTags:
    """Summary and chapters from a transcript, however long it is."""
    settings = settings or get_settings()
    call = call or llm.parse
    if not segments:
        raise ValueError("describe needs a transcript")

    budget = settings.max_transcript_chars
    windows = split_by_chars(segments, budget)
    if len(windows) == 1:
        tags = _describe_window(windows[0], title=title, duration=duration,
                                call=call, settings=settings,
                                model=llm.model_for(settings, "brief"))
        return _tidy(tags, duration)

    # A long stream: digest windows spread across it, then summarise the digests.
    # Summarising EVERY window is not worth what it costs. Measured 13 Nov 2026:
    # a 5h45 transcript is 44 windows, about 237,000 input tokens, which against
    # a budget of 8,000 tokens a minute is half an hour of pure rate limiting --
    # to produce a 3-5 sentence summary and the 6-12 chapters the PRD asks for.
    # So sample evenly instead. The moments do not come from here: they come
    # from signal peaks measured over the whole transcript, so a sampled record
    # costs the pipeline nothing.
    sampled, total = _sample(windows, settings.summary_windows)
    log.info("transcript is %d chars in %d window(s); summarising %d of them",
             sum(len(s.text) for s in segments), total, len(sampled))
    windows = sampled
    digests: list[tuple[float, float, SourceTags]] = []
    for index, window in enumerate(windows, start=1):
        if beat:
            beat()
        digests += _digest(window, index, len(windows), title=title, duration=duration,
                           call=call, settings=settings, beat=beat)

    if not digests:
        raise llm.LLMBadAnswer("no part of the transcript could be summarised")

    joined = "\n\n".join(
        f"[{start / 60:.0f}-{end / 60:.0f} min] {tags.summary}"
        for start, end, tags in digests
    )
    header = [f"Title as published: {title}"] if title else []
    if duration:
        header.append(f"Total duration: {duration / 60:.0f} minutes")
    if len(digests) < total:
        header.append(
            f"These are {len(digests)} samples spread across the video, not every part "
            f"of it. Describe what the video is from what you can see, and do not claim "
            f"to cover parts you were not shown."
        )
    overall = call(
        Overall, REDUCE_SYSTEM,
        "\n".join(header) + "\n\nSummaries of each part, in order:\n\n" + joined,
        model=llm.model_for(settings, "brief"), settings=settings,
        max_tokens=REDUCE_ANSWER_TOKENS,
    )
    if not isinstance(overall, Overall):
        raise llm.LLMBadAnswer(f"expected Overall, got {type(overall).__name__}")

    chapters = [c for _s, _e, tags in digests for c in tags.chapters]
    language = overall.language or next(
        (tags.language for _s, _e, tags in digests if tags.language), None)
    return _tidy(SourceTags(summary=overall.summary, chapters=chapters,
                            language=language), duration)


#: How many times a window may be halved before giving up on it.
MAX_SPLITS = 3


def _digest(
    window: list[caps.Segment], index: int, total: int, *, title: str | None,
    duration: float | None, call: llm.Caller, settings: Settings, beat=None,
    depth: int = 0,
) -> list[tuple[float, float, SourceTags]]:
    """Summarise one window, halving it if the provider says it is too big.

    How many tokens a transcript costs is not something to estimate: measured
    against Groq on 13 Nov 2026, 14,000 characters of this stream came to
    between 9,000 and 13,000 tokens, roughly one token per character rather
    than the usual four. So the provider's own refusal is the signal, and the
    window is split until it fits. That adapts to any transcript, any
    tokeniser and any limit without a constant to keep up to date.
    """
    start, end = window[0].start, window[-1].end
    try:
        tags = _describe_window(
            window, title=title, duration=duration, call=call, settings=settings,
            model=llm.model_for(settings, "scoring"),
            note=f"This is part {index} of {total}, covering {start / 60:.0f} to "
                 f"{end / 60:.0f} minutes. Summarise only it.",
        )
        return [(start, end, tags)]
    except llm.LLMRequestTooLarge as exc:
        if depth >= MAX_SPLITS or len(window) < 2:
            log.warning("window %d/%d given up on after %d split(s): %s",
                        index, total, depth, exc)
            return []
        middle = len(window) // 2
        log.info("window %d/%d is %s tokens against a %s limit; splitting it",
                 index, total, exc.requested, exc.limit)
        out: list[tuple[float, float, SourceTags]] = []
        for half in (window[:middle], window[middle:]):
            if beat:
                beat()
            out += _digest(half, index, total, title=title, duration=duration,
                           call=call, settings=settings, beat=beat, depth=depth + 1)
        return out
    except llm.LLMBadAnswer as exc:
        # One unreadable window must not lose the record of a five-hour stream.
        log.warning("window %d/%d not summarised: %s", index, total, exc)
        return []


def _describe_window(
    segments: list[caps.Segment], *, title: str | None, duration: float | None,
    call: llm.Caller, settings: Settings, model: str, note: str = "",
) -> SourceTags:
    header = []
    if title:
        header.append(f"Title as published: {title}")
    if duration:
        header.append(f"Duration: {duration / 60:.1f} minutes")
    if note:
        header.append(note)
    tags = call(
        SourceTags, SYSTEM,
        "\n".join(header) + "\n\nTranscript with timestamps:\n\n" + caps.as_text(segments),
        model=model, settings=settings, max_tokens=WINDOW_ANSWER_TOKENS,
    )
    if not isinstance(tags, SourceTags):
        raise llm.LLMBadAnswer(f"expected SourceTags, got {type(tags).__name__}")
    return tags


def _tidy(tags: SourceTags, duration: float | None) -> SourceTags:
    if duration:
        tags.chapters = [c for c in tags.chapters if 0 <= c.start <= duration]
    tags.chapters = thin_chapters(tags.chapters, duration=duration)
    return tags


def transcript_path(settings: Settings, source_id: int) -> Path:
    return settings.data_dir / "transcripts" / f"{source_id}.json"


def probe_into(conn: sqlite3.Connection, source_id: int, path: Path) -> media.Probe:
    """Record what ffprobe can see. Cheap and needs no model."""
    info = media.probe(path)
    conn.execute(
        "UPDATE sources SET duration=?, bytes=?, language=COALESCE(language, ?) WHERE id=?",
        (info.duration, info.bytes, info.language, source_id),
    )
    return info


def save_tags(conn: sqlite3.Connection, source_id: int, tags: SourceTags) -> None:
    import json

    conn.execute(
        "UPDATE sources SET summary=?, chapters_json=?, language=COALESCE(?, language) "
        "WHERE id=?",
        (
            tags.summary,
            json.dumps([c.model_dump() for c in tags.chapters]),
            tags.language,
            source_id,
        ),
    )
