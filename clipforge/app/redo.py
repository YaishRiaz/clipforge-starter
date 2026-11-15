"""Fix a clip from a plain-English note, e.g. a Telegram reply: "cut the last 3 seconds".

The note becomes a small structured edit (trim the start or end, change or drop the
hook). The clip is then remade from its source as a new clip row; the old one is
closed, never deleted, because Telegram buttons carry clip ids.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from pydantic import Field

from ..core import llm, queue
from ..core.config import Settings
from ..core.schema import LLMModel
from ..ingest import briefs
from . import review

MIN_SECONDS = 5.0
MAX_SECONDS = 59.0
EDITABLE = ("ready", "approved", "held")

SYSTEM = """You turn a reviewer's note about a short vertical clip into an edit. \
You are given the clip's duration, its words with times (seconds from the clip start) and its \
current hook text.

Fill in only what the note asks for:
- trim_start: seconds to cut from the start (positive). Negative adds footage before it.
- trim_end: seconds to cut from the end (positive). Negative adds footage after it.
- hook: new hook text (under 60 characters, true to the clip), or "" to remove the hook.
- unsupported: what the note asks for that these edits cannot do (music, effects, a different \
moment, camera layout). Empty if everything is covered.
"Last few seconds are unclear" means cut about 2 to 3 seconds from the end. To end after a \
phrase, use the word times to work out how much to cut. Never invent words."""


class Edit(LLMModel):
    trim_start: float = Field(0.0, description="Seconds to cut from the start; negative extends.")
    trim_end: float = Field(0.0, description="Seconds to cut from the end; negative extends.")
    hook: str | None = Field(None, description="New hook text; empty string removes it.")
    unsupported: str = Field("", description="Part of the note that cannot be done.")


class RedoError(ValueError):
    pass


def _fallback(text: str) -> Edit:
    """Without the model, understand the plainest notes: 'cut the last 3 seconds'."""
    t = text.lower()
    num = re.search(r"(\d+(?:\.\d+)?)\s*(?:s\b|sec)", t)
    n = float(num.group(1)) if num else 2.0
    if "last" in t or "end" in t:
        return Edit(trim_end=n)
    if "first" in t or "start" in t or "begin" in t:
        return Edit(trim_start=n)
    return Edit(unsupported=text[:200])


def _clip_words(settings: Settings, moment_id: int) -> list:
    f = settings.data_dir / "work" / f"clip-{moment_id}" / "words.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def interpret(conn: sqlite3.Connection, settings: Settings, clip_id: int, note: str) -> Edit:
    row = review.get(conn, clip_id)
    meta = review.meta_of(row)
    words = " ".join(f"{w[2]}@{w[0]:.1f}" for w in _clip_words(settings, row["moment_id"]))
    user = (f"Clip duration: {meta.get('duration')} s\nHook: {row['hook']}\n"
            f"Words: {words[:2500]}\n\nNote: {note[:500]}")
    try:
        return llm.parse(Edit, SYSTEM, user, model=llm.model_for(settings, "scoring"),
                         settings=settings, max_tokens=400)
    except (llm.LLMUnavailable, llm.LLMQuotaExhausted, llm.LLMBadAnswer, llm.LLMRequestTooLarge):
        return _fallback(note)


def describe(edit: Edit) -> str:
    bits = []
    if edit.trim_start:
        bits.append(f"{'cutting' if edit.trim_start > 0 else 'adding'} {abs(edit.trim_start):g} s "
                    f"{'from' if edit.trim_start > 0 else 'before'} the start")
    if edit.trim_end:
        bits.append(f"{'cutting' if edit.trim_end > 0 else 'adding'} {abs(edit.trim_end):g} s "
                    f"{'from' if edit.trim_end > 0 else 'after'} the end")
    if edit.hook is not None:
        bits.append(f"hook -> {edit.hook!r}" if edit.hook else "removing the hook")
    return ", ".join(bits)


def apply(conn: sqlite3.Connection, settings: Settings, clip_id: int, edit: Edit, *,
          replace_posted: bool = False) -> int:
    """Close the clip and queue a remake with the edit. Returns the moment id.

    `replace_posted` is for a clip already out that Yaish will delete and repost: the new
    version then counts as a fresh clip of that moment.
    """
    row = review.get(conn, clip_id)
    if row["status"] not in EDITABLE + (("posted", "submitted") if replace_posted else ()):
        raise RedoError(f"clip {clip_id} is {row['status']}; only clips not yet posted can be remade")
    if not (edit.trim_start or edit.trim_end or edit.hook is not None):
        raise RedoError(edit.unsupported or "I could not find an edit in that note")
    meta = review.meta_of(row)
    start = float(meta.get("source_start") or 0) + edit.trim_start
    end = float(meta.get("source_end") or 0) - edit.trim_end
    rules = briefs.load_rules(conn, row["campaign_id"])
    lo = max(MIN_SECONDS, (rules.clip_min_seconds if rules and rules.clip_min_seconds else 0))
    if end - start < lo or end - start > MAX_SECONDS or start < 0:
        raise RedoError(f"that would make the clip {end - start:.1f} s; it must be {lo:g}-{MAX_SECONDS:g} s")

    mid = row["moment_id"]
    meta["review_note"] = "remade from a note: " + describe(edit)
    conn.execute("UPDATE clips SET status='rejected', package_path=NULL, meta_json=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False), clip_id))
    sets, args = ["start=?", '"end"=?', "status='candidate'"], [start, end]
    if edit.hook is not None:
        sets.append("hook=?")
        args.append(edit.hook.strip()[:60])
    conn.execute(f"UPDATE moments SET {', '.join(sets)} WHERE id=?", (*args, mid))

    work = settings.data_dir / "work" / f"clip-{mid}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    (work / "override.json").write_text(json.dumps({"start": start, "end": end}), encoding="utf-8")
    queue.enqueue(conn, "make_clip", ref_id=mid)
    return mid
