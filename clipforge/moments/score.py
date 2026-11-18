"""Picking the moments worth clipping (PRD section 6 steps 3 to 5).

The transcript and the local signal peaks go to the model; it comes back with
candidate moments, each with a hook line, a title and which rubric categories
it hits. The weighted score is then computed here from the rubric file, not by
the model, so re-weighting a rubric never needs a prompt change and two
moments are always compared the same way.

A five-hour stream will not fit in one request, and sending all of it would be
wasteful anyway: the local signals already say where something happened, so
only the windows around those peaks are sent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import Field

from ..core import llm
from ..core.config import Settings, get_settings
from ..core.schema import LLMModel
from ..ingest import captions as caps
from ..ingest.rules import BriefRules
from .rubric import Rubric
from .signals import Peak

log = logging.getLogger("clipforge.moments.score")

#: Prompts are assembled from a list of lines rather than one long
#: f-string, so the same text can be reused for a split window.
NEWLINE = chr(10)

SYSTEM = """You choose the moments in a livestream transcript that would work as \
short vertical clips, for a clipping pipeline.

You are given a window of transcript with timestamps, and the reason the window was \
picked out (a loudness spike, a burst of fast talking, or both).

For each moment you propose:
- start and end, in seconds, from the transcript's own timestamps. Start on the first \
word of the sentence that sets the moment up, not mid-sentence (skip a filler opener such as \"um\" or \"so okay\" and start where the point begins), and end on the payoff, \
not after it has fizzled out. The payoff is the outcome the viewer is waiting for: if the \
streamer makes a bet, an attempt, a threat or a prediction, the moment must run through \
the result and the immediate reaction to it, never stop at the announcement. A stranger \
who has never seen the stream must understand the situation from the clip alone: start \
early enough to show who is chasing whom, what the goal is or what was just said, rather \
than in the middle of the action. If the setup is not in the transcript, flag the \
needs-context penalty instead of starting cold.
- description: one line on what actually happens.
- hook: the words to burn on the first frame. Under 60 characters, it must make a \
stranger stay, and it must be true to the clip. Not a question unless the clip answers \
it. No "wait for it", no clickbait that the clip does not pay off.
- title: under 70 characters, states the stakes.
- categories: which of the listed ids this moment genuinely hits. Only what is there.
- penalties: which of the listed problems this moment has. Be honest; a moment you flag \
as needing context is more useful than one that is quietly broken.
- confidence: 0 to 1, how sure you are this works as a standalone clip.

Rules:
- Only propose a moment you can point at in the transcript given. Never invent dialogue.
- If a window contains nothing worth clipping, return no moments for it. A short honest \
list beats a padded one.
- Do not propose overlapping moments.
- Respect the length limits you are given."""


class Candidate(LLMModel):
    start: float
    end: float
    description: str
    hook: str = Field(description="Burned on the first frame. Under 60 characters.")
    title: str = Field(description="Under 70 characters.")
    categories: list[str] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class CandidateList(LLMModel):
    moments: list[Candidate] = Field(default_factory=list)


@dataclass
class Scored:
    """A candidate with the rubric's arithmetic applied."""

    candidate: Candidate
    score: float
    rubric_category: str

    @property
    def start(self) -> float:
        return self.candidate.start

    @property
    def end(self) -> float:
        return self.candidate.end

    @property
    def length(self) -> float:
        return self.candidate.end - self.candidate.start


def window_around(peak: Peak, *, before: float = 25.0, after: float = 25.0,
                  duration: float | None = None) -> tuple[float, float]:
    """The slice of transcript worth sending for one peak."""
    start = max(0.0, peak.start - before)
    end = peak.end + after
    if duration is not None:
        end = min(end, duration)
    return start, end


def merge_windows(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Overlapping windows become one, so no transcript is sent twice."""
    if not windows:
        return []
    ordered = sorted(windows)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def segments_between(segments: list[caps.Segment], start: float,
                     end: float) -> list[caps.Segment]:
    return [s for s in segments if s.end > start and s.start < end]


#: Used when the brief is silent. PRD section 7: under 60 s unless the brief
#: says otherwise, and a Short under 15 s rarely has room for setup and payoff.
DEFAULT_MIN_SECONDS = 15.0
DEFAULT_MAX_SECONDS = 60.0


def _length_rule(rules: BriefRules | None) -> tuple[float, float]:
    """The clip length this campaign allows. The brief wins where it speaks."""
    low, high = DEFAULT_MIN_SECONDS, DEFAULT_MAX_SECONDS
    if rules is not None:
        if rules.clip_min_seconds is not None:
            low = rules.clip_min_seconds
        if rules.clip_max_seconds is not None:
            high = rules.clip_max_seconds
    if low > high:
        raise ValueError(
            f"the brief allows no clip length: minimum {low:g}s is above maximum {high:g}s"
        )
    return low, high


def propose(
    segments: list[caps.Segment],
    peaks: list[Peak],
    rubric: Rubric,
    *,
    rules: BriefRules | None = None,
    duration: float | None = None,
    call: llm.Caller | None = None,
    settings: Settings | None = None,
    beat=None,
) -> list[Scored]:
    """Ask for candidate moments around each peak, then score them here."""
    settings = settings or get_settings()
    call = call or llm.parse
    if not segments:
        return []
    low, high = _length_rule(rules)

    windows = merge_windows([
        window_around(p, duration=duration) for p in peaks
    ])
    if not windows:
        log.info("no signal peaks, so nothing to send")
        return []

    reasons: dict[tuple[float, float], list[str]] = {}
    for peak in peaks:
        for window in windows:
            if window[0] <= peak.start <= window[1]:
                reasons.setdefault(window, []).append(
                    f"{peak.signal} {peak.z:.1f} sd above normal at {peak.start:.0f}s"
                    + (f" ({peak.note})" if peak.note else "")
                )
                break

    out: list[Scored] = []
    for window in windows:
        if beat:
            beat()
        slice_ = segments_between(segments, *window)
        if not slice_:
            continue
        why = "; ".join(reasons.get(window, ["no signal reason recorded"])[:4])
        user = _prompt_for(window, slice_, rubric, rules=rules, low=low,
                           high=high, reason=why)
        try:
            answer = call(CandidateList, SYSTEM, user,
                          model=llm.model_for(settings, "scoring"), settings=settings,
                          max_tokens=ANSWER_TOKENS)
        except llm.LLMRequestTooLarge as exc:
            # The transcript for this window will not fit one request. Splitting
            # it is better than losing it; measured on a real VOD, a 160 s window
            # of dense speech came to 10,700 tokens against an 8,000 limit.
            log.info("window %.0f-%.0f is %s tokens against %s; halving it",
                     *window, exc.requested, exc.limit)
            out += _score_halves(window, slice_, rubric, rules=rules, duration=duration,
                                 call=call, settings=settings, low=low, high=high,
                                 reason=why)
            continue
        except llm.LLMBadAnswer as exc:
            # One bad window must not lose the whole source.
            log.warning("window %.0f-%.0f scored nothing: %s", *window, exc)
            continue
        if not isinstance(answer, CandidateList):
            raise llm.LLMBadAnswer(f"expected CandidateList, got {type(answer).__name__}")
        for candidate in answer.moments:
            scored = _accept(candidate, rubric, low, high, window, duration)
            if scored is not None:
                out.append(scored)

    out.sort(key=lambda s: s.score, reverse=True)
    return dedupe(out)


#: A window refused at every size is dropped rather than retried forever.
MAX_SPLITS = 2

#: A handful of moments with a hook and a title each, not an essay. On a
#: per-minute token budget the answer costs as much as the question.
ANSWER_TOKENS = 2500


def _prompt_for(window: tuple[float, float], slice_: list[caps.Segment], rubric: Rubric,
                *, rules: BriefRules | None, low: float, high: float,
                reason: str) -> str:
    """The one place the scoring prompt is built, so halves ask the same way."""
    parts = [
        f"Window {window[0]:.0f}s to {window[1]:.0f}s was picked out because: {reason}",
        "",
        rubric.describe_for_prompt(),
        "",
        f"Clips must be between {low:g} and {high:g} seconds long.",
        "Give start and end as the absolute seconds shown in the transcript below, "
        "not as offsets from the start of this window.",
        _brief_note(rules),
        "",
        "Transcript:",
        "",
        caps.as_text(slice_),
    ]
    return NEWLINE.join(parts)


def _score_halves(window: tuple[float, float], slice_: list[caps.Segment], rubric: Rubric,
                  *, rules: BriefRules | None, duration: float | None, call: llm.Caller,
                  settings: Settings, low: float, high: float, reason: str,
                  depth: int = 1) -> list[Scored]:
    """Score a window the provider refused, by halving it.

    Same reasoning as the summariser: how many tokens a transcript costs is not
    worth estimating, so the provider's refusal is the signal. Measured on a
    real VOD, a 160 s window of dense speech came to 10,700 tokens against an
    8,000-token-per-minute limit.
    """
    if depth > MAX_SPLITS or len(slice_) < 2:
        log.warning("window %.0f-%.0f dropped: still too large after %d split(s)",
                    window[0], window[1], depth - 1)
        return []
    middle = len(slice_) // 2
    out: list[Scored] = []
    for part in (slice_[:middle], slice_[middle:]):
        if not part:
            continue
        sub = (part[0].start, part[-1].end)
        try:
            answer = call(CandidateList, SYSTEM,
                          _prompt_for(sub, part, rubric, rules=rules, low=low,
                                      high=high, reason=reason),
                          model=llm.model_for(settings, "scoring"), settings=settings,
                          max_tokens=ANSWER_TOKENS)
        except llm.LLMRequestTooLarge:
            out += _score_halves(sub, part, rubric, rules=rules, duration=duration,
                                 call=call, settings=settings, low=low, high=high,
                                 reason=reason, depth=depth + 1)
            continue
        except llm.LLMBadAnswer as exc:
            log.warning("window %.0f-%.0f scored nothing: %s", sub[0], sub[1], exc)
            continue
        if not isinstance(answer, CandidateList):
            raise llm.LLMBadAnswer(f"expected CandidateList, got {type(answer).__name__}")
        for candidate in answer.moments:
            scored = _accept(candidate, rubric, low, high, sub, duration)
            if scored is not None:
                out.append(scored)
    return out


def _brief_note(rules: BriefRules | None) -> str:
    if rules is None:
        return ""
    notes = []
    if rules.banned_content:
        notes.append("Never propose a moment containing: "
                     + "; ".join(rules.banned_content[:8]) + ".")
    if rules.required_overlay:
        notes.append("Every clip will carry an overlay, so leave room: "
                     + rules.required_overlay)
    return " ".join(notes)


def _absolute(candidate: Candidate, window: tuple[float, float]) -> Candidate:
    """Repair times the model gave relative to the window instead of the video.

    Measured on a real run: asked about a window at 16885-16945 s, gpt-oss-20b
    answered 10.2-17.4 -- offsets from the start of the window rather than the
    absolute timestamps it had been shown. The moment was real; only its
    arithmetic was wrong. So shift it when the absolute reading is impossible
    and the relative one lands inside the window, and leave it alone otherwise.
    """
    start, end = window
    if candidate.start >= start or start <= 0:
        return candidate                       # already absolute, or nothing to shift
    span = end - start
    # Only when the pair reads as an offset INTO this window. A number larger
    # than the window is not an offset into it, so it stays what it was and
    # gets dropped as out of range: better to lose a moment than to attach a
    # hook to footage the model never saw.
    if candidate.end > span + 1:
        return candidate
    shifted_start, shifted_end = candidate.start + start, candidate.end + start
    log.info("reading %.1f-%.1f as relative to the window: %.1f-%.1f",
             candidate.start, candidate.end, shifted_start, shifted_end)
    return candidate.model_copy(update={"start": shifted_start, "end": shifted_end})


def _accept(candidate: Candidate, rubric: Rubric, low: float, high: float,
            window: tuple[float, float], duration: float | None) -> Scored | None:
    """Drop a candidate that breaks the rules rather than trusting it."""
    candidate = _absolute(candidate, window)
    length = candidate.end - candidate.start
    if length <= 0:
        log.info("dropping a moment with end <= start (%.1f-%.1f)",
                 candidate.start, candidate.end)
        return None
    if length < low or length > high:
        log.info("dropping a %.1fs moment, outside %.0f-%.0fs", length, low, high)
        return None
    if duration is not None and candidate.end > duration + 1:
        log.info("dropping a moment past the end of the source (%.1fs)", candidate.end)
        return None
    # The model may only point inside the window it was shown.
    if candidate.start < window[0] - 1 or candidate.end > window[1] + 1:
        log.info("dropping a moment outside its window (%.1f-%.1f vs %.0f-%.0f)",
                 candidate.start, candidate.end, *window)
        return None
    if not candidate.title.strip():
        # Seen on a real run: a good moment with the title left blank.
        text = candidate.description.strip()
        candidate = candidate.model_copy(update={"title": text[:67] + "..." if len(text) > 70 else text})
    score = rubric.score(candidate.categories, candidate.penalties)
    score *= max(0.0, min(1.0, candidate.confidence))
    best = max(
        (c for c in candidate.categories if c in rubric.category_ids),
        key=rubric.weight_of, default="unscored",
    )
    return Scored(candidate=candidate, score=round(score, 1), rubric_category=best)


def dedupe(scored: list[Scored], *, overlap: float = 0.5) -> list[Scored]:
    """Drop the weaker of two moments that cover the same footage.

    PRD section 6 step 5. Input must be strongest-first; output is in time order
    so the caller can read it like a running order.
    """
    kept: list[Scored] = []
    for item in scored:
        if any(_overlap_share(item, other) > overlap for other in kept):
            continue
        kept.append(item)
    kept.sort(key=lambda s: s.start)
    return kept


def _overlap_share(a: Scored, b: Scored) -> float:
    """How much of the shorter moment the two share, 0 to 1."""
    latest_start = max(a.start, b.start)
    earliest_end = min(a.end, b.end)
    shared = earliest_end - latest_start
    if shared <= 0:
        return 0.0
    shortest = min(a.length, b.length)
    return shared / shortest if shortest > 0 else 0.0
