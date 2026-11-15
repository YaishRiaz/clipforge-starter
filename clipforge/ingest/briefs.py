"""Turn a campaign brief into a BriefRules record (PRD section 5 step 1).

The brief text itself comes from the database: Yaish pastes it, or campaign
discovery stores it. Fetching a brief page over the network is kept behind
ALLOW_BRIEF_FETCH, off by default, because `VERIFY` item 10 (whether the
platforms' terms permit automated reading) is still open in
`docs/verification.md`.

The parsed rules are shown on the campaign page for Yaish to correct, so the
parser is told to leave a field empty rather than invent a value.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from ..core import llm
from ..core.config import Settings, get_settings
from .rules import BriefRules

log = logging.getLogger("clipforge.ingest.briefs")

SYSTEM = """You read paid-clipping campaign briefs and turn them into a strict rule set \
that an automated clipping pipeline must obey.

Rules for your answer:
- Extract only what the brief actually states. If the brief is silent on a field, leave it \
empty or null. Never infer a plausible default, and never copy an example from these \
instructions.
- approved_source_urls: every link the brief names as footage a clipper may use. Include \
video, playlist, channel and shared-folder links. Do not include links to the brief itself, \
to the brand's website, to a Discord, or to anything that is not footage to clip.
- approved_source_notes: footage the brief describes but does not link ("the VODs in the \
pinned Drive folder"), quoted closely enough that a human can go find it.
- disclosure_text: the exact wording the brief demands, character for character, not a \
paraphrase.
- pre_approval: true only if the brief requires someone to approve the clip or a draft \
BEFORE it is posted. A brief that merely reviews clips after posting is false.
- banned_content and banned_edits: one short phrase per item, in the brief's own terms.
- required_overlay: anything the brief says must appear ON every clip, such as the creator's \
channel URL as an overlay. Do not confuse it with the ban on third-party tool watermarks: one \
is something to add, the other something to avoid.
- required_post_settings: requirements about the post rather than the video, such as \
"comments and likes ON". One short phrase per item.
- The payout fields decide what a clip is predicted to earn, so read them twice. \
min_views is the view count below which a clip earns nothing; briefs state it in many \
shapes ("clips under 2,000 views do not earn", "minimum 2k views to qualify", "must reach \
2,000 views"). Record the number. Do the same for rate_per_1k and clip_cap_usd.
- uncertain: list every field where the brief is ambiguous, self-contradictory, or where you \
had to make a judgement call. Say what is unclear. This list is read by a human, so an \
honest "clip length stated as 'short' with no number" is worth more than a guess.

You are not deciding whether to join the campaign, and you are not writing copy. You are \
only recording what the brief requires."""


class BriefUnavailable(RuntimeError):
    """There is no brief text to parse yet; a human has to supply it."""


# The payout fields decide predicted earnings (PRD section 10: estimated earnings
# are zero below the campaign's minimum views), so a silent miss over-reports
# money. gpt-oss-120b read "Clips under 2,000 views do not earn" and left
# min_views null without flagging it, on 12 Nov 2026. These patterns are a
# deterministic second opinion: they never overwrite the model's answer, they
# only add a note to `uncertain` so a human looks. A regex is a worse extractor
# than the model and a better alarm.
_MONEY_PATTERNS: dict[str, tuple[str, ...]] = {
    "min_views": (
        r"under\s+([\d,]+)\s*k?\s+views",
        r"(?:minimum|min\.?|at least)\s+(?:of\s+)?([\d,]+)\s*k?\s+views",
        r"([\d,]+)\s*k?\+?\s+views\s+(?:minimum|to\s+(?:earn|qualify|count))",
        r"(?:must|need(?:s)?\s+to)\s+(?:reach|hit)\s+([\d,]+)\s*k?\s+views",
        r"fewer\s+than\s+([\d,]+)\s*k?\s+views",
    ),
    "rate_per_1k": (
        r"\$\s*([\d.]+)\s*(?:per|/|for\s+every)\s*(?:1[,.]?000|1\s*k\b)",
        r"([\d.]+)\s*(?:usd|dollars)\s*(?:per|/)\s*(?:1[,.]?000|1\s*k\b)",
    ),
    "clip_cap_usd": (
        r"(?:max(?:imum)?|cap(?:ped)?)\s*(?:at\s*|of\s*|payout\s*)?\$\s*([\d,]+)",
        r"\$\s*([\d,]+)\s*(?:max(?:imum)?|cap)\b",
        r"up\s+to\s+\$\s*([\d,]+)\s+per\s+clip",
    ),
}


def audit_against_brief(rules: BriefRules, text: str) -> list[str]:
    """Notes for fields the brief seems to state but the rules left empty.

    Only the payout fields, because those are the ones where silence costs money
    rather than just quality. Returns notes to add to `uncertain`; it never
    changes a value.
    """
    notes: list[str] = []
    lowered = text.lower()
    for field, patterns in _MONEY_PATTERNS.items():
        if getattr(rules, field) is not None:
            continue
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match is None:
                continue
            quote = text[max(0, match.start() - 40):match.end() + 40].strip()
            quote = re.sub(r"\s+", " ", quote)
            notes.append(
                f"{field} was left empty, but the brief seems to state it: "
                f"\"...{quote}...\" - check and set it by hand"
            )
            break
    return notes


def brief_text_for(conn: sqlite3.Connection, campaign_id: int) -> str:
    row = conn.execute(
        "SELECT name, brief_text, brief_url FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    if row is None:
        raise BriefUnavailable(f"no campaign {campaign_id}")
    text = (row["brief_text"] or "").strip()
    if text:
        return text
    where = row["brief_url"] or "the campaign page"
    raise BriefUnavailable(
        f"campaign {campaign_id} ({row['name']}) has no brief text saved. "
        f"Open {where}, copy the brief, and paste it in on the campaign page. "
        f"Automated fetching stays off until the platforms' terms are checked "
        f"(docs/verification.md, VERIFY item 10)."
    )


def parse_brief_text(
    text: str,
    *,
    campaign_name: str = "",
    call: llm.Caller | None = None,
    settings: Settings | None = None,
) -> BriefRules:
    """Ask the model for the rule set. Pure apart from the one call."""
    settings = settings or get_settings()
    call = call or llm.parse
    if not text.strip():
        raise BriefUnavailable("brief text is empty")
    header = f"Campaign: {campaign_name}\n\n" if campaign_name else ""
    rules = call(
        BriefRules,
        SYSTEM,
        f"{header}Brief follows between the markers.\n\n"
        f"<<<BRIEF\n{text.strip()}\nBRIEF>>>",
        model=llm.model_for(settings, "brief"),
        settings=settings,
    )
    if not isinstance(rules, BriefRules):  # a fake caller in a test, or a bad SDK answer
        raise llm.LLMBadAnswer(f"expected BriefRules, got {type(rules).__name__}")
    return rules


def parse_brief(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    call: llm.Caller | None = None,
    settings: Settings | None = None,
) -> BriefRules:
    """Parse the stored brief and save the rules on the campaign."""
    row = conn.execute("SELECT name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    text = brief_text_for(conn, campaign_id)
    rules = parse_brief_text(
        text, campaign_name=row["name"] if row else "", call=call, settings=settings
    )
    # The model never widens what is approved: channels are Yaish's call alone, so
    # whatever it returned is dropped and his earlier choice survives a re-parse.
    before = load_rules(conn, campaign_id)
    rules.approved_channels = list(before.approved_channels) if before else []
    for note in audit_against_brief(rules, text):
        if note not in rules.uncertain:
            rules.uncertain.append(note)
    save_rules(conn, campaign_id, rules)
    log.info(
        "campaign %s: %d approved source link(s), pre_approval=%s, %d uncertain note(s)",
        campaign_id, len(rules.approved_source_urls), rules.pre_approval, len(rules.uncertain),
    )
    return rules


def save_rules(conn: sqlite3.Connection, campaign_id: int, rules: BriefRules) -> None:
    conn.execute(
        "UPDATE campaigns SET rules_json=? WHERE id=?",
        (rules.model_dump_json(), campaign_id),
    )


def load_rules(conn: sqlite3.Connection, campaign_id: int) -> BriefRules | None:
    """The saved rules, or None when the brief has not been parsed yet."""
    row = conn.execute(
        "SELECT platform, rules_json FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    if row is None:
        return None
    if row["platform"] == "own":
        return BriefRules.for_own_footage()
    if not row["rules_json"]:
        return None
    try:
        return BriefRules.model_validate(json.loads(row["rules_json"]))
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("campaign %s has unreadable rules_json: %s", campaign_id, exc)
        return None
