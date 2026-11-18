"""The machine-checkable rule set a campaign brief is turned into.

PRD section 5 step 1 lists what has to come out of a brief. This module holds
the shape, the URL-approval test that hard limit 1 depends on, and nothing that
talks to a network or a model, so it can be tested on its own.

`docs/verification.md` design change 6 adds `pre_approval`: some campaigns want
a draft approved before posting, and those clips must be held.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from pydantic import Field

from ..core.schema import LLMModel

# Query parameters that identify nothing about WHICH video this is, so two links
# that differ only in these are the same footage. `t`, `filter` and `sort` are
# here because a Twitch VOD is routinely pasted with a timestamp or the viewer's
# own list filter attached, and approving the same VOD twice is pointless.
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
             "si", "feature", "app", "pp", "ab_channel", "fbclid", "gclid",
             "t", "filter", "sort"}
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
             "www.youtu.be", "music.youtube.com"}


class SourceWindow(LLMModel):
    """A stretch of an approved video that the brief limits clipping to."""

    url: str
    start: float = 0.0
    end: float | None = Field(None, description="Seconds; null means to the end of the video.")


class BriefRules(LLMModel):
    """Everything the pipeline needs to obey one campaign's brief."""

    # --- what footage may be ingested (hard limit 1 and 2) ---
    approved_source_urls: list[str] = Field(
        default_factory=list,
        description="Every link the brief names as approved footage: videos, playlists, "
                    "channels, or shared folders. Only what the brief actually names.",
    )
    approved_channels: list[str] = Field(
        default_factory=list,
        description="LEAVE EMPTY. Set only by Yaish, when the brief approves every stream "
                    "or video on a channel (e.g. https://twitch.tv/name). Anything the "
                    "parser puts here is discarded.",
    )
    approved_source_notes: str = Field(
        "",
        description="Approved footage the brief describes but does not link, in the "
                    "brief's own words, for Yaish to resolve by hand.",
    )

    # --- clip shape ---
    clip_min_seconds: float | None = None
    clip_max_seconds: float | None = None
    allowed_platforms: list[str] = Field(
        default_factory=list,
        description="Lowercase platform names the brief allows, e.g. "
                    "['youtube_shorts', 'tiktok', 'instagram_reels'].",
    )

    # --- what every clip must carry ---
    required_hashtags: list[str] = Field(default_factory=list)
    required_mentions: list[str] = Field(default_factory=list)
    disclosure_text: str | None = Field(
        None, description="Exact wording the brief requires in the description, if any.")
    caption_requirements: str | None = None
    required_names_any: list[str] = Field(
        default_factory=list,
        description="Names of which the caption or on-screen text must mention at least one, "
                    "e.g. 'must mention Sypher, Maddynf or Replays'. Empty if the brief has no such rule.")
    source_windows: list[SourceWindow] = Field(
        default_factory=list,
        description="Leave empty. Time limits on approved videos are entered by hand, never guessed.")
    required_caption: str | None = Field(
        None, description="A caption the brief says every post must carry word for word, e.g. the "
                          "quoted text in 'Use the required caption: \"Jake Paul On YouTube\"'. "
                          "Just that text, no quotes. Null if the brief gives none.")
    min_creator_frame_share: float | None = Field(
        None, description="Smallest share of the frame that must show the creator, 0 to 1.")
    required_overlay: str | None = Field(
        None,
        description="A graphic, handle or watermark the brief requires on every clip, in "
                    "the brief's own words, e.g. the creator's channel URL as an overlay. "
                    "This is a requirement to ADD something, not the ban on third-party "
                    "tool watermarks.",
    )
    required_aspect_ratio: str | None = Field(
        None, description="Aspect ratio the brief demands, e.g. '9:16'.")
    required_post_settings: list[str] = Field(
        default_factory=list,
        description="Requirements about the post itself rather than the video, in the "
                    "brief's own words, e.g. 'comments on', 'likes on'. These are applied "
                    "when the clip is published.",
    )

    # --- what no clip may contain ---
    banned_content: list[str] = Field(default_factory=list)
    banned_edits: list[str] = Field(default_factory=list)

    # --- when footage counts, for briefs that approve one day's stream of a channel ---
    footage_after: str | None = Field(
        None,
        description="ISO date-time (UTC) when eligible footage starts, if the brief names a "
                    "date or event ('only the November 19 stream'). Footage recorded before "
                    "it is never clipped. Null if the brief has no such limit.")
    footage_games: list[str] = Field(
        default_factory=list,
        description="Games the footage must show, if the brief limits it ('GTA 6 moments "
                    "only'), as the streaming site names them. Empty means any.")

    # --- posting workflow ---
    pre_approval: bool = Field(
        False,
        description="True when the brief requires the campaign team to approve a draft "
                    "before it is posted. Those clips are held, never auto-posted.",
    )
    post_from: str | None = Field(
        None,
        description="ISO date-time (UTC) before which nothing may be posted, when the brief "
                    "says the campaign opens later ('submissions begin when it opens').")
    paid_promotion: bool = Field(
        False,
        description="True when the brief asks to disclose paid content through the "
                    "platform's own paid-promotion setting.")

    # --- money, as the brief states it; Yaish confirms against the campaign page ---
    rate_per_1k: float | None = None
    clip_cap_usd: float | None = None
    min_views: int | None = None
    country_restrictions: list[str] = Field(
        default_factory=list, description="Countries the brief names as eligible or excluded.")
    deadline: str | None = Field(None, description="ISO date if the brief states one.")
    min_tier1_share: float | None = Field(
        None,
        description="Smallest share of a clip's VIEWERS that must come from the countries "
                    "the brief calls Tier 1 (or names, e.g. US/UK/CA/AU), as 0 to 1: "
                    "'at least 30% Tier 1 audience' is 0.3. Null if the brief has no "
                    "such number. This is about viewers, not where the clipper lives.")
    audience_requirements: str | None = Field(
        None,
        description="Any requirement about who watches (countries, tiers, language, real "
                    "engagement), in the brief's own words. Null if the brief has none.")

    # --- parser honesty ---
    uncertain: list[str] = Field(
        default_factory=list,
        description="Anything the brief leaves ambiguous or that was guessed. Shown to "
                    "Yaish on the campaign page so he can correct it.",
    )

    @classmethod
    def for_own_footage(cls) -> "BriefRules":
        """Yaish's own recordings carry no brief (PRD section 5)."""
        return cls(allowed_platforms=["youtube_shorts"])

    def approves_url(self, url: str) -> bool:
        """Does the brief name this footage as approved?

        True when the URL is one of the approved entries, or sits under an
        approved folder, playlist or channel. Everything else is refused: hard
        limit 1 means an unapproved link is never downloaded.
        """
        if not url or not self.approved_source_urls:
            return False
        target = normalize_url(url)
        target_id = youtube_id(url)
        for entry in self.approved_source_urls:
            approved = normalize_url(entry)
            if not approved:
                continue
            if target == approved:
                return True
            approved_id = youtube_id(entry)
            if target_id and approved_id and target_id == approved_id:
                return True
            # A folder, playlist or channel approves what is under it. Compare on
            # a path boundary so /videos-private does not match /videos.
            if not approved_id and (target.startswith(approved.rstrip("/") + "/")
                                    or target.startswith(approved + "?")):
                return True
        return False

    def window_for(self, url: str | None) -> tuple[float, float | None] | None:
        """The (start, end) the brief limits this video to, or None when the whole video is approved."""
        target = normalize_url(url or "")
        for w in self.source_windows:
            if normalize_url(w.url) == target:
                return w.start, w.end
        return None

    def inside_window(self, url: str | None, start: float, end: float) -> bool:
        win = self.window_for(url)
        return win is None or (start >= win[0] and (win[1] is None or end <= win[1]))

    def approves_channel(self, channel_url: str) -> bool:
        """Has Yaish approved everything on this channel for the campaign?"""
        target = channel_root(channel_url)
        return bool(target) and any(channel_root(c) == target for c in self.approved_channels)

    def clip_length_ok(self, seconds: float) -> tuple[bool, str]:
        if self.clip_min_seconds is not None and seconds < self.clip_min_seconds:
            return False, f"clip is {seconds:.1f}s, brief requires at least {self.clip_min_seconds}s"
        if self.clip_max_seconds is not None and seconds > self.clip_max_seconds:
            return False, f"clip is {seconds:.1f}s, brief allows at most {self.clip_max_seconds}s"
        return True, ""

    def footage_ok(self, at: float | None, games=()) -> tuple[bool, str]:
        """May footage recorded at `at` (epoch seconds), showing `games`, be clipped?"""
        if self.footage_after and at is not None and at < _epoch(self.footage_after):
            return False, f"recorded before {self.footage_after}, when the brief's footage starts"
        if self.footage_games and games is not None:
            wanted = {_game(g) for g in self.footage_games}
            if not any(_game(g) in wanted for g in games):
                return False, f"shows {', '.join(games) or 'no game'}, not {', '.join(self.footage_games)}"
        return True, ""

    def may_post(self, now: float) -> bool:
        return not self.post_from or now >= _epoch(self.post_from)

    def allows_platform(self, platform: str) -> bool:
        """An empty list means the brief did not say; the campaign page decides."""
        if not self.allowed_platforms:
            return True
        return platform.lower() in {p.lower() for p in self.allowed_platforms}


def youtube_id(url: str) -> str | None:
    """The 11-character video id, from any of YouTube's URL shapes."""
    try:
        parts = urlparse(url if "//" in url else "//" + url, scheme="https")
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host not in _YT_HOSTS:
        return None
    if host.endswith("youtu.be"):
        candidate = parts.path.lstrip("/").split("/")[0]
    elif parts.path in ("/watch", "/watch/"):
        candidate = (parse_qs(parts.query).get("v") or [""])[0]
    else:
        m = re.match(r"^/(?:shorts|embed|v|live)/([^/?#]+)", parts.path)
        candidate = m.group(1) if m else ""
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate or "") else None


def _epoch(iso: str) -> float:
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


_GAME_ALIASES = {"gta6": "grandtheftautovi", "gtavi": "grandtheftautovi", "gta5": "grandtheftautov",
                 "gtav": "grandtheftautov"}


def _game(name: str) -> str:
    """One spelling per game: 'GTA 6', 'GTA VI' and 'Grand Theft Auto VI' are the same."""
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return _GAME_ALIASES.get(key, key)


def channel_root(url: str) -> str:
    """A channel link without a /videos, /clips or /streams tail, normalized."""
    base = normalize_url(url)
    root = re.sub(r"/(videos|clips|streams|about|schedule|featured)$", "", base)
    return root.lower() if ("twitch.tv/" in root or "kick.com/" in root) else root   # names ignore case


def normalize_url(url: str) -> str:
    """Lowercase host, no tracking parameters, no trailing slash, no fragment.

    Two links to the same footage must normalize to the same string, or the
    approval test would let a reworded duplicate through.
    """
    url = (url or "").strip()
    if not url:
        return ""
    vid = youtube_id(url)
    if vid:
        return f"https://youtube.com/watch?v={vid}"
    try:
        parts = urlparse(url if "//" in url else "//" + url, scheme="https")
    except ValueError:
        return url.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    kept = sorted(
        (k, v) for k, vs in parse_qs(parts.query).items()
        if k.lower() not in _TRACKING for v in vs
    )
    query = "&".join(f"{k}={v}" for k, v in kept)
    path = parts.path.rstrip("/")
    out = f"{parts.scheme or 'https'}://{host}{path}"
    return f"{out}?{query}" if query else out
