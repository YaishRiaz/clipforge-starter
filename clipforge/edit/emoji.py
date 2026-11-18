"""Reaction emoji that pop up beside the captions, the way top Shorts use them.

The images are Twemoji (CC-BY 4.0, credit in assets/emoji/CREDIT.txt), a fixed set fetched once
with `python -m clipforge emoji-fetch`. libass cannot draw colour emoji, so they are overlaid as
pictures. Which emoji appears is decided from the words being spoken, never invented: a laugh
gets a laughing face, "no way" a shocked one, money talk a money bag. A clip with none of these
words gets none.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.config import package_root

log = logging.getLogger("clipforge.edit.emoji")

VERSION = "15.1.0"
BASE = f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@{VERSION}"

# name -> codepoint file stem
SET = {
    "laugh": "1f602", "rofl": "1f923", "skull": "1f480", "cry": "1f62d", "scream": "1f631",
    "mindblown": "1f92f", "flushed": "1f633", "fire": "1f525", "hundred": "1f4af", "eyes": "1f440",
    "angry": "1f620", "rage": "1f92c", "trophy": "1f3c6", "crown": "1f451", "money": "1f4b0",
    "cash": "1f4b8", "target": "1f3af", "zap": "26a1", "siren": "1f6a8", "exclaim": "2757",
    "question": "2753", "wow": "1f62e", "cold": "1f976", "devil": "1f608", "pray": "1f64f",
    "clap": "1f44f", "cool": "1f60e", "clown": "1f921", "party": "1f389", "goat": "1f410",
    "think": "1f914", "grimace": "1f62c", "sweat": "1f605", "smirk": "1f60f", "boom": "1f4a5",
    "run": "1f3c3", "egg": "1f95a", "golf": "26f3", "rocket": "1f680", "muscle": "1f4aa",
}

# Spoken words that call for an emoji, checked in this order (first match wins for a word).
TRIGGERS: list[tuple[str, re.Pattern]] = [
    ("laugh", re.compile(r"^(l+o+l+|lmao+|haha+|hahaha+|lmfao)$", re.I)),
    ("skull", re.compile(r"^(dead|died|dying|i'?m dead)$", re.I)),
    ("scream", re.compile(r"^(no+|nooo+|ahh+|aaa+h*|run|help)$", re.I)),
    ("mindblown", re.compile(r"^(insane|crazy|unbelievable|impossible|wild)$", re.I)),
    ("wow", re.compile(r"^(wow+|whoa+|omg|holy)$", re.I)),
    ("trophy", re.compile(r"^(win|won|winner|victory|first|champion|gold)$", re.I)),
    ("money", re.compile(r"^(money|cash|dollars?|rich|paid|\$\d+\w*)$", re.I)),
    ("fire", re.compile(r"^(fire|lit|cracked|clean|perfect)$", re.I)),
    ("rage", re.compile(r"^(why|seriously|kidding|unfair|rigged)$", re.I)),
    ("eyes", re.compile(r"^(secret|hidden|look|wait)$", re.I)),
    ("question", re.compile(r"^(what|huh|how)\??$", re.I)),
    ("golf", re.compile(r"^(hole|golf|par|birdie|bogey)$", re.I)),
    ("egg", re.compile(r"^(egg|eggs)$", re.I)),
    ("run", re.compile(r"^(chase|chasing|chased|fast|faster)$", re.I)),
]

MIN_GAP = 3.5       # seconds between two emoji
SHOW = 1.4          # seconds each stays on screen
SIZE = 150          # pixels on the 1080-wide frame


@dataclass(frozen=True)
class Pop:
    name: str
    start: float
    end: float
    path: Path


def emoji_dir() -> Path:
    return package_root() / "assets" / "emoji"


def path_of(name: str, directory: Path | None = None) -> Path:
    return (directory or emoji_dir()) / f"{SET[name]}.png"


def available(directory: Path | None = None) -> bool:
    return all(path_of(n, directory).is_file() for n in SET)


def pick(words: list[tuple[float, float, str]], *, max_events: int = 4, duration: float | None = None,
         directory: Path | None = None) -> list[Pop]:
    """Emoji for the spoken words that call for one, spaced out, at most `max_events`."""
    out: list[Pop] = []
    for start, _end, text in words:
        token = re.sub(r"[^\w$']", "", text)
        name = next((n for n, pat in TRIGGERS if pat.match(token)), None)
        if name is None or (out and start - out[-1].start < MIN_GAP):
            continue
        path = path_of(name, directory)
        if not path.is_file():
            continue
        end = start + SHOW if duration is None else min(duration, start + SHOW)
        out.append(Pop(name, round(start, 2), round(end, 2), path))
        if len(out) >= max_events:
            break
    return out


def fetch(directory: Path | None = None, *, get=None) -> int:
    """Download the fixed set and the licence once. Returns how many images were fetched."""
    import httpx

    get = get or (lambda url: httpx.get(url, timeout=30, follow_redirects=True))
    d = directory or emoji_dir()
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, stem in SET.items():
        dest = d / f"{stem}.png"
        if dest.is_file():
            continue
        r = get(f"{BASE}/assets/72x72/{stem}.png")
        if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
            raise RuntimeError(f"emoji {name} ({stem}): HTTP {r.status_code}")
        dest.write_bytes(r.content)
        n += 1
    lic = d / "LICENSE-GRAPHICS.txt"
    if not lic.is_file():
        r = get(f"{BASE}/LICENSE-GRAPHICS")
        if r.status_code == 200:
            lic.write_bytes(r.content)
    (d / "CREDIT.txt").write_text(
        f"Emoji graphics: Twemoji {VERSION} (https://github.com/jdecked/twemoji), "
        "Copyright 2019 Twitter, Inc and other contributors, licensed under CC-BY 4.0 "
        "(https://creativecommons.org/licenses/by/4.0/). Used unmodified, scaled.\n", encoding="utf-8")
    return n
