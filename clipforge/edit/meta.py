"""Title, description and hashtags for one clip.

The model writes the title and the description. Everything the brief *requires*
(tags, mentions, disclosure, the creator's handle) is added here in code
afterwards, never left to the model to remember.
"""
from __future__ import annotations

import logging
import re

from pydantic import Field

from ..core import llm
from ..core.schema import LLMModel
from ..ingest.rules import BriefRules
from .subs import mask

log = logging.getLogger("clipforge.meta")

TITLE_MAX = 70
EXTRA_TAGS = 3


class Draft(LLMModel):
    title: str = Field("", description="Under 70 characters. States the stakes or the surprise.")
    description: str = Field("", description="One or two short lines. No hashtags.")
    hashtags: list[str] = Field(default_factory=list, description="Up to three relevant tags.")


SYSTEM = """You write the title and description for a short vertical clip cut from a livestream.

Rules:
- The title is under 70 characters, plain, and says what happens or what is at stake. No clickbait lies,
  no all-caps, no emoji.
- The description is one or two short lines that add context the title lacks. No hashtags in it.
- Up to three hashtags about the content (game, event type), lowercase, no spaces.
- Use only what the transcript shows. Never invent events or names.
- Put the streamer's name in the title when it reads naturally ("Timmy loses it when..."), so their viewers recognise it. Never use a vague stand-in such as "teammate", "streamer", "this guy" or "someone" when a name is known. If the transcript clearly names a friend or guest the moment is about, name them too. Never name anyone the transcript does not name, and never name other channels or promote them.
- The brief's own rules for captions and titles, when given, win over everything above."""


def _tag(raw: str) -> str | None:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", raw.lstrip("#"))
    return f"#{cleaned.lower()}" if cleaned else None


def clip_title(text: str) -> str:
    text = " ".join(mask(text).split())
    if len(text) <= TITLE_MAX:
        return text
    cut = text[:TITLE_MAX - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def handle_from(rules: BriefRules | None, layout_handle: str | None) -> str | None:
    if layout_handle:
        return layout_handle
    if rules:
        for text in (rules.caption_requirements, rules.required_overlay):
            m = re.search(r"\b[\w-]+\.(?:tv|com|gg)/[\w.-]+", text or "")
            if m:
                return m.group(0)
    return None


def draft(moment, transcript: str, *, settings, creator: str | None = None,
          rules: BriefRules | None = None) -> Draft:
    """Ask the model, and fall back to what the moment already carries."""
    fallback = Draft(title=moment["title"] or moment["hook"] or "",
                     description=moment["description"] or "")
    user = (f"Streamer: {creator or 'unknown'}\n"
            + (f"Brief's caption requirements: {rules.caption_requirements}\n"
               if rules and rules.caption_requirements else "")
            + f"Moment: {moment['description']}\nHook line: {moment['hook']}\n"
            f"Working title: {moment['title']}\n\nTranscript of the clip:\n{transcript[:3000]}")
    try:
        got = llm.parse(Draft, SYSTEM, user, model=llm.model_for(settings, "scoring"),
                        settings=settings, max_tokens=800)
    except (llm.LLMUnavailable, llm.LLMQuotaExhausted, llm.LLMBadAnswer,
            llm.LLMRequestTooLarge) as exc:
        log.warning("title written without the model: %s", exc)
        return fallback
    return Draft(title=got.title or fallback.title,
                 description=got.description or fallback.description,
                 hashtags=got.hashtags)


def stream_credit(url: str | None, streamer: str | None) -> str | None:
    """'twitch.tv/<channel>' for a clip cut from a Twitch or Kick stream, to credit and link the
    streamer in the post text. Twitch logins are the display name in lower case."""
    name = (streamer or "").strip()
    if not url or not re.fullmatch(r"\w{3,25}", name):
        return None
    for host in ("twitch.tv", "kick.com"):
        if host in url.lower():
            return f"{host}/{name.lower()}"
    return None


def finish(d: Draft, *, rules: BriefRules | None, handle: str | None, credit: str | None = None) -> dict:
    """The final fields, with everything the brief requires added in code. `credit` is the
    streamer's channel, written in the text only (never burned in, so it is not an overlay)."""
    required = [t for t in (_tag(x) for x in (rules.required_hashtags if rules else [])) if t]
    extra: list[str] = []
    for raw in d.hashtags:
        t = _tag(raw)
        if t and t not in required and t not in extra:
            extra.append(t)
    tags = required + extra[:EXTRA_TAGS]

    lines = [" ".join(mask(d.description).split())] if d.description.strip() else []
    caption = (rules.required_caption or "").strip() if rules else ""
    if caption and caption.lower() not in clip_title(d.title).lower():
        lines.insert(0, caption)                  # the brief's own caption leads the post text
    names = [n for n in (rules.required_names_any if rules else []) if n.strip()]
    if names and not any(n.lower() in (clip_title(d.title) + " " + " ".join(lines)).lower() for n in names):
        lines.insert(0, f"Featuring {names[0]}")   # at least one named creator, as the brief requires
    if handle:
        lines.append(f"Watch live: {handle}")
    elif credit:
        lines.append(f"Full stream: {credit}")
    if rules and rules.required_mentions:
        lines.append(" ".join(m if m.startswith("@") else f"@{m}" for m in rules.required_mentions))
    disclosure = rules.disclosure_text if rules else None
    if disclosure:
        lines.append(disclosure)
    if tags:
        lines.append(" ".join(tags))

    return {
        "title": clip_title(d.title) or "Untitled clip",
        "description": "\n".join(lines),
        "hashtags": tags,
        "disclosure": disclosure,
        "handle": handle,
    }
