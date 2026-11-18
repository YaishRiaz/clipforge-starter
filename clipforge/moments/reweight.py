"""Re-weight a niche rubric from how our own clips actually performed (PRD section 6).

The shipped weights are a prior taken from big channels' Shorts. Once our own
posted clips have views, each moment type is nudged towards what we measured.
Nothing here is automatic: `propose` only computes, `apply` writes, and a
person triggers it (Learn page or `rubric-reweight`).

Rules that keep a few lucky clips from rewriting the rubric:
- only posts older than SETTLE_HOURS count, so views have had time to arrive;
- a type needs MIN_POSTS posts before it moves, and at least two types must
  qualify, because a single type has nothing to be compared against;
- the move is shrunk towards the prior by n / (n + PRIOR_STRENGTH);
- weights stay inside [FLOOR, CEILING] times the prior.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

from . import rubric as rubric_mod

MIN_POSTS = 3
PRIOR_STRENGTH = 10
SETTLE_HOURS = 48
FLOOR = 0.6
CEILING = 1.4


@dataclass(frozen=True)
class Change:
    id: str
    n: int
    median_views: int | None
    old: float
    new: float


def own_views(conn: sqlite3.Connection, *, niche: str | None = None, rubrics_dir: Path | None = None,
              settle_hours: float = SETTLE_HOURS) -> dict[str, list[int]]:
    """Latest view count of each settled post, grouped by the moment's rubric category.

    Without `niche` this pools every campaign together (the old behaviour). With it, only
    posts whose campaign resolves to that rubric file count -- so a GTA clip's "big_reaction"
    never gets mixed into the general rubric's median just because the category id matches.
    """
    out: dict[str, list[int]] = {}
    for r in conn.execute(
        "SELECT m.rubric_category cat, k.category camp_cat, "
        "(SELECT views FROM stats st WHERE st.post_id=p.id ORDER BY captured_at DESC LIMIT 1) views "
        "FROM posts p JOIN clips cl ON cl.id=p.clip_id JOIN moments m ON m.id=cl.moment_id "
        "JOIN sources s ON s.id=m.source_id JOIN campaigns k ON k.id=s.campaign_id "
        "WHERE p.posted_at IS NOT NULL AND p.posted_at <= datetime('now', ?)",
        (f"-{settle_hours} hours",),
    ):
        if r["views"] is None or not r["cat"]:
            continue
        if niche is not None and rubric_mod.niche_of(rubrics_dir, r["camp_cat"]) != niche:
            continue
        out.setdefault(r["cat"], []).append(int(r["views"]))
    return out


def propose(rub: rubric_mod.Rubric, perf: dict[str, list[int]], *,
            min_posts: int = MIN_POSTS) -> list[Change]:
    """New weight for every category; unchanged where there is too little evidence."""
    medians = {c.id: statistics.median(perf[c.id]) for c in rub.categories
               if len(perf.get(c.id, [])) >= min_posts}
    overall = statistics.median(medians.values()) if len(medians) >= 2 else 0
    qualifying_prior = [c.weight for c in rub.categories if c.id in medians]
    anchor = statistics.mean(qualifying_prior) if qualifying_prior else 0.0
    changes: list[Change] = []
    for c in rub.categories:
        views = perf.get(c.id, [])
        median = int(medians[c.id]) if c.id in medians else (
            int(statistics.median(views)) if views else None)
        new = c.weight
        if overall > 0 and c.id in medians:
            target = anchor * (medians[c.id] / overall)
            n = len(views)
            blended = c.weight + (target - c.weight) * n / (n + PRIOR_STRENGTH)
            new = round(min(c.weight * CEILING, max(c.weight * FLOOR, blended)), 2)
        changes.append(Change(c.id, len(views), median, c.weight, new))
    return changes


def apply(path: Path, changes: list[Change], *, write: bool = True) -> dict:
    """Write the proposal into the rubric file, keeping the old file as <name>.v<N>.json."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    by_id = {c.id: c for c in changes}
    for cat in raw["categories"]:
        if cat["id"] in by_id:
            cat["weight"] = by_id[cat["id"]].new
    raw["own_evidence"] = {c.id: {"n": c.n, "median_views": c.median_views}
                           for c in changes if c.n}
    old_version = int(raw.get("version", 1))
    raw["version"] = old_version + 1
    if write:
        shutil.copyfile(path, path.with_name(f"{path.stem}.v{old_version}.json"))
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        rubric_mod.for_campaign.cache_clear()
    return raw
