"""What a clip's edit looks like, and how a campaign brief limits it.

`Style` holds only things the renderer can really do. A niche's taste book
(`tastebook/<niche>.json`) fills it in; `resolve` then switches off whatever
the campaign's brief bans or constrains. The brief always wins: a taste-book
feature the brief might forbid is dropped, never argued with.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from ..ingest.rules import BriefRules


class Captions(BaseModel):
    enabled: bool = True
    words_per_event: int = Field(2, ge=1, le=6)
    font: str = "Arial"
    size: int = Field(66, ge=30, le=140)
    uppercase: bool = True
    color: str = "#FFFFFF"
    highlight: str = ""                 # colour of the word being spoken; empty = no highlight
    outline: int = Field(6, ge=0, le=14)
    pop: bool = False                   # each new caption grows a little then settles
    margin_v: int = Field(330, ge=60, le=900)


class Hook(BaseModel):
    enabled: bool = True
    seconds: float = Field(1.8, ge=0.5, le=5.0)
    size: int = Field(92, ge=40, le=160)
    color: str = "#FFF000"
    font: str = "Arial"
    banner: bool = False                # the title stays on screen for the whole clip
    box: bool = False                   # the title sits on a solid box


class Zoom(BaseModel):
    enabled: bool = False
    scale: float = Field(1.10, ge=1.02, le=1.4)
    seconds: float = Field(0.7, ge=0.2, le=2.0)
    max_events: int = Field(4, ge=1, le=8)
    min_gap: float = Field(4.0, ge=1.0, le=20.0)
    z: float = Field(2.0, ge=0.5, le=6.0)      # loudness spike, in robust deviations above the clip's own level


class Cuts(BaseModel):
    trim_lines: bool = False            # drop filler lines a small model marks DELETE


class Emoji(BaseModel):
    enabled: bool = False               # reaction emoji pop up beside the captions
    max_events: int = Field(3, ge=1, le=6)


class Style(BaseModel):
    captions: Captions = Captions()
    hook: Hook = Hook()
    zoom: Zoom = Zoom()
    cuts: Cuts = Cuts()
    emoji: Emoji = Emoji()


# Words in a brief's ban lists that switch a feature off. Matched on the whole
# lower-case text, so "No zoom effects", "zooms" and "Zoom-ins" all count.
_BANS = {
    "zoom": ("zoom", "punch-in", "punch in", "crop in", "scale up", "effect", "filter",
             "heavy edit", "no edit", "any edit", "unedited", "raw footage"),
    "cuts": ("cutting", "splic", "jump cut", "trim", "remove parts", "heavy edit", "no edit",
             "any edit", "unedited", "raw footage", "out of context", "alter the footage",
             "altering the footage"),
    "highlight": ("effect", "animation", "animated", "heavy edit", "no edit", "any edit",
                  "unedited"),
    "emoji": ("emoji", "sticker", "graphic", "overlay", "effect", "heavy edit", "no edit",
              "any edit", "unedited"),
}
_CAPTION_LOOK = re.compile(r"\b(font|colou?r|position|bottom|top of|size|style|subtitle|lower third)\b", re.I)


class Resolved(BaseModel):
    style: Style
    niche: str = "none"
    book_status: str = "none"
    dropped: list[str] = []
    notes: list[str] = []


def _ban_text(rules: BriefRules | None) -> str:
    if not rules:
        return ""
    return " | ".join([*rules.banned_edits, *rules.banned_content]).lower()


def from_book(book: dict[str, Any] | None) -> Style:
    return Style.model_validate((book or {}).get("style") or {})


def resolve(book: dict[str, Any] | None, rules: BriefRules | None, *, fit: bool = False) -> Resolved:
    """The style for one clip: taste book first, then the brief takes things away."""
    style = from_book(book)
    out = Resolved(style=style, niche=(book or {}).get("niche", "none"),
                   book_status=(book or {}).get("status", "none"))
    ban = _ban_text(rules)

    def drop(what: str, why: str) -> None:
        out.dropped.append(f"{what}: {why}")

    if fit and style.zoom.enabled:
        style.zoom.enabled = False
        drop("zoom", "the whole frame is shown, so there is nothing to punch in on")
    for feature, words in _BANS.items():
        hit = next((w for w in words if w in ban), None)
        if hit is None:
            continue
        if feature == "zoom" and style.zoom.enabled:
            style.zoom.enabled = False
            drop("zoom", f"brief bans '{hit}'")
        elif feature == "cuts" and style.cuts.trim_lines:
            style.cuts.trim_lines = False
            drop("cuts", f"brief bans '{hit}'")
        elif feature == "emoji" and style.emoji.enabled:
            style.emoji.enabled = False
            drop("emoji", f"brief bans '{hit}'")
        elif feature == "highlight" and (style.captions.highlight or style.captions.pop):
            style.captions.highlight, style.captions.pop = "", False
            drop("caption animation", f"brief bans '{hit}'")
    if rules and rules.caption_requirements and _CAPTION_LOOK.search(rules.caption_requirements):
        plain = Captions()
        if style.captions != plain:
            style.captions = plain
            drop("caption style", "the brief sets caption requirements; kept to the plain default")
    if style.zoom.enabled:
        out.notes.append("zoom only touches the gameplay band; the creator's cam is never cropped")
    return out
