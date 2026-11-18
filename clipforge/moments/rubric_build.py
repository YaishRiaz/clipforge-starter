"""Derive rubrics/gta.json weights from how real GTA Shorts performed (PRD section 6 step 4).

Metadata only: titles, view counts and durations from the read-only YouTube Data
API. The model sorts titles into moment types; each type is weighted by the median
views of its members. Types that a title cannot reveal (`setup_payoff`,
`stakes_early`) keep their hand-set weights.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import Field

from ..core import llm
from ..core.config import Settings
from ..core.schema import LLMModel
from . import rubric as rb

log = logging.getLogger("clipforge.rubric_build")

API = "https://www.googleapis.com/youtube/v3"
QUERIES = ("gta 5 funny moments", "gta 5 roleplay funny", "gta rp clips",
           "gta 5 police chase", "gta 5 heist", "gta 5 glitch")
MAX_SECONDS = 90
KEEP = 150
MIN_MEMBERS = 5
WEIGHT_FLOOR, WEIGHT_SPAN = 0.6, 0.6      # weights land between 0.6 and 1.2
STRUCTURAL = {"setup_payoff", "stakes_early"}


class BuildError(RuntimeError):
    pass


class Assignment(LLMModel):
    i: int
    category: str


class NewType(LLMModel):
    id: str = Field(description="snake_case id")
    description: str


class Clusters(LLMModel):
    new_types: list[NewType] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)


def parse_duration(iso: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0.0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def _get(path: str, params: dict, settings: Settings) -> dict:
    import httpx

    if not settings.youtube_api_key:
        raise BuildError("YOUTUBE_API_KEY is empty in .env")
    try:
        r = httpx.get(f"{API}/{path}", params=params, timeout=60.0,
                      headers={"X-Goog-Api-Key": settings.youtube_api_key})
    except httpx.HTTPError as exc:
        raise BuildError(f"could not reach the YouTube API: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise BuildError(f"YouTube API HTTP {r.status_code}: {msg}")
    return r.json()


def fetch_shorts(settings: Settings, *, queries=QUERIES, days: int = 365) -> list[dict]:
    """[{id, title, views, seconds}], most-viewed first, deduplicated."""
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for q in queries:
        data = _get("search", {"part": "id", "q": q, "type": "video", "videoDuration": "short",
                              "order": "viewCount", "maxResults": 50, "publishedAfter": after,
                              "relevanceLanguage": "en"}, settings)
        ids += [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
    ids = list(dict.fromkeys(ids))
    out = []
    for n in range(0, len(ids), 50):
        data = _get("videos", {"part": "snippet,statistics,contentDetails",
                               "id": ",".join(ids[n:n + 50])}, settings)
        for v in data.get("items", []):
            secs = parse_duration(v.get("contentDetails", {}).get("duration", ""))
            views = int(v.get("statistics", {}).get("viewCount", 0) or 0)
            if 0 < secs <= MAX_SECONDS and views > 0:
                out.append({"id": v["id"], "title": v["snippet"]["title"],
                            "views": views, "seconds": secs})
    out.sort(key=lambda v: v["views"], reverse=True)
    return out[:KEEP]


def cluster(shorts: list[dict], current: rb.Rubric, settings: Settings, *,
            subject: str = "GTA") -> Clusters:
    known = [c for c in current.categories if c.id not in STRUCTURAL]
    system = (f"You sort titles of {subject} Shorts into moment types. "
              "Use only the type ids given, or "
              "'other'. You may propose at most 3 new types (snake_case id plus one-line "
              "description) if many titles share a type that none of the given ones covers; "
              "then assign to them too. Assign each title once, by its index.")
    user = ("Moment types:\n" + "\n".join(f"- {c.id}: {c.description}" for c in known)
            + "\n\nTitles:\n" + "\n".join(f"{i}: {s['title']}" for i, s in enumerate(shorts)))
    return llm.parse(Clusters, system, user, model=llm.model_for(settings, "scoring"),
                     settings=settings)


def weights(shorts: list[dict], clusters: Clusters, current: rb.Rubric) -> dict[str, dict]:
    """{category: {weight, n, median_views}} for the types with enough members."""
    allowed = {c.id for c in current.categories if c.id not in STRUCTURAL}
    allowed |= {t.id for t in clusters.new_types}
    views: dict[str, list[int]] = {}
    for a in clusters.assignments:
        if a.category in allowed and 0 <= a.i < len(shorts):
            views.setdefault(a.category, []).append(shorts[a.i]["views"])
    med = {c: statistics.median(v) for c, v in views.items() if len(v) >= MIN_MEMBERS}
    if not med:
        return {}
    top = max(med.values())
    return {c: {"weight": round(WEIGHT_FLOOR + WEIGHT_SPAN * m / top, 2),
                "n": len(views[c]), "median_views": int(m)} for c, m in med.items()}


def assemble(current: rb.Rubric, derived: dict[str, dict], clusters: Clusters, *,
             sample: int, today: str, subject: str = "GTA", name: str | None = None) -> dict:
    new_desc = {t.id: t.description for t in clusters.new_types}
    cats = []
    for c in current.categories:
        d = derived.get(c.id)
        cats.append({"id": c.id, "weight": d["weight"] if d else c.weight,
                     "description": c.description})
    for ident, d in derived.items():
        if ident not in current.category_ids:
            cats.append({"id": ident, "weight": d["weight"], "description": new_desc.get(ident, "")})
    return {
        "name": name or current.name, "version": current.version + 1 if name is None else 1,
        "note": (f"Weights for types seen in titles come from median views of {sample} top {subject} "
                 f"Shorts (YouTube Data API, {today}); a type with fewer than {MIN_MEMBERS} "
                 "matches keeps its earlier weight, and setup_payoff/stakes_early are hand-set. "
                 "Views favour big channels, so treat this as a prior and re-weight from our "
                 "own clips' views."),
        "evidence": {k: {"n": v["n"], "median_views": v["median_views"]}
                     for k, v in sorted(derived.items())},
        "categories": cats,
        "penalties": [{"id": p.id, "weight": p.weight, "description": p.description}
                      for p in current.penalties],
    }


def build(path: Path, settings: Settings, *, write: bool = True, queries=QUERIES,
          subject: str = "GTA", base: Path | None = None, name: str | None = None) -> dict:
    """Re-weight `base` (default: `path` itself) from top Shorts and write it to `path`."""
    current = rb.load(base or path)
    shorts = fetch_shorts(settings, queries=queries)
    if len(shorts) < 30:
        raise BuildError(f"only {len(shorts)} usable Shorts found; not enough to derive weights")
    clusters = cluster(shorts, current, settings, subject=subject)
    derived = weights(shorts, clusters, current)
    if not derived:
        raise BuildError("no moment type had enough matching titles")
    out = assemble(current, derived, clusters, sample=len(shorts),
                   today=datetime.now(timezone.utc).strftime("%d %b %Y"), subject=subject,
                   name=name)
    if write:
        Path(path).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        rb.for_campaign.cache_clear()
    return out
