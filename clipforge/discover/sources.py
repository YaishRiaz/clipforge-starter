"""Campaign listings from the platforms' public pages.

Only pages the site's robots.txt allows are fetched, once per refresh, with an
honest User-Agent. Nothing here logs in, calls a private API, or touches an
account: it reads the same public list a visitor sees.
"""
from __future__ import annotations

import logging
import re
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from . import flight

log = logging.getLogger("clipforge.discover")

USER_AGENT = "ClipForge/1.0 (personal campaign finder; one request per page, no login)"

GAMING_CATEGORIES = {"gaming", "roblox", "minecraft", "esports", "streamer", "streamers"}
GAMING_WORDS = re.compile(
    r"\b(gta|call of duty|warzone|fortnite|valorant|minecraft|roblox|apex|csgo|cs2|"
    r"league of legends|twitch|stream(er)?|gameplay|gaming|zonewars|uefn|"
    r"elden ring|pokemon|fifa|nba 2k|madden)\b", re.I)


@dataclass
class Found:
    source: str
    external_id: str
    title: str
    brand: str = ""
    category: str = ""
    platforms: list[str] = field(default_factory=list)
    rate_min: float | None = None            # dollars per 1,000 views
    rate_max: float | None = None
    budget_total: float | None = None
    budget_left: float | None = None
    requires_application: bool = False
    locked: bool = False
    ended: bool = False
    url: str = ""
    image: str = ""
    ends_at: str | None = None
    min_views: int | None = None

    @property
    def gaming(self) -> bool:
        return (self.category or "").lower() in GAMING_CATEGORIES or bool(
            GAMING_WORDS.search(f"{self.title} {self.brand}"))


class Blocked(RuntimeError):
    """robots.txt does not allow this page."""


def _allowed(client: httpx.Client, url: str) -> bool:
    parts = urlparse(url)
    robots = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    try:
        resp = client.get(robots)
        if resp.status_code >= 400:
            return True                      # no robots.txt: nothing is disallowed
        parser.parse(resp.text.splitlines())
    except httpx.HTTPError:
        return False                         # cannot check, so do not fetch
    return parser.can_fetch(USER_AGENT, url)


def fetch_page(url: str, client: httpx.Client | None = None) -> str:
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30,
                                    follow_redirects=True)
    try:
        if not _allowed(client, url):
            raise Blocked(f"robots.txt does not allow {url}")
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text
    finally:
        if own:
            client.close()


def _clean(text: object) -> str:
    """The page's data escapes a literal dollar sign as two."""
    return str(text or "").replace("$$", "$").strip()


def _dollars(text: str) -> list[float]:
    return [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)", (text or "").replace("$$", "$"))]


# --- Content Rewards -------------------------------------------------------

CR_BASE = "https://contentrewards.com"
CR_PAGES = ("/c/discover/clipping", "/c/discover")


def parse_contentrewards(html: str) -> list[Found]:
    text = flight.decode(html)
    raw: dict[str, dict] = {}
    for wrapped in flight.objects_after(text, '{"campaign":'):
        if isinstance(wrapped, dict) and wrapped.get("id"):
            raw[wrapped["id"]] = wrapped
    for items in flight.objects_after(text, '"campaigns":'):
        if isinstance(items, list):
            for c in items:
                if isinstance(c, dict) and c.get("id") and c.get("title"):
                    raw.setdefault(c["id"], c)
    out = []
    for c in raw.values():
        rates = _dollars(c.get("ratePer1kLabel", "")) or (
            [float(c["payoutSortRaw"])] if c.get("payoutSortRaw") is not None else [])
        out.append(Found(
            source="contentrewards", external_id=str(c["id"]),
            title=_clean(c.get("title", "")), brand=_clean(c.get("brand", "")), category=str(c.get("category") or ""),
            platforms=[str(p).lower() for p in c.get("platforms") or []],
            rate_min=min(rates) or None if rates else None,
            rate_max=max(rates) or None if rates else None,
            budget_total=c.get("budgetTotalRaw"), budget_left=c.get("availableBudgetRaw"),
            requires_application=bool(c.get("requiresApplication")),
            ended=(c.get("availableBudgetRaw") or 0) <= 0 or bool(c.get("opensAt")),
            url=f"{CR_BASE}/c/campaigns/{c['id']}", image=str(c.get("thumbnail") or ""),
        ))
    return out


# --- Vyro ------------------------------------------------------------------

VYRO_BASE = "https://vyro.com"


def parse_vyro(html: str) -> list[Found]:
    text = flight.decode(html)
    out = []
    for page in flight.objects_after(text, '"initialPage":'):
        for c in (page.get("data") or []) if isinstance(page, dict) else []:
            if not isinstance(c, dict) or not c.get("slug"):
                continue
            payout = c.get("payoutModel") or {}
            rate = payout.get("payRate")
            budget = c.get("budget") or {}
            status = (c.get("status") or {}).get("id", "")
            out.append(Found(
                source="vyro", external_id=str(c["id"]), title=str(c.get("name", "")),
                brand=str((c.get("workspace") or {}).get("name", "")),
                category=str(c.get("type") or ""),
                platforms=[str(p).lower() for p in (c.get("targeting") or {}).get("platforms") or []],
                rate_min=rate if payout.get("model") == "CPM" else None,
                rate_max=c.get("headlineRate") if payout.get("model") == "CPM" else None,
                budget_total=budget.get("value"), budget_left=budget.get("remaining"),
                locked=bool(c.get("isLocked")),
                ended=status != "active" or (budget.get("remaining") or 0) <= 0,
                url=f"{VYRO_BASE}/campaigns/{c['slug']}",
                image=str((c.get("workspace") or {}).get("image") or ""),
                ends_at=c.get("endDate"),
                min_views=(c.get("requirements") or {}).get("minViewsPerClip"),
            ))
    return out


CONNECTORS = {
    "contentrewards": ([CR_BASE + p for p in CR_PAGES], parse_contentrewards),
    "vyro": ([VYRO_BASE + "/campaigns"], parse_vyro),
}
