"""Burned-in text: hook, captions, and the creator-handle overlay, as one ASS file.

Word times come from `words.py` (real word timestamps for the clip). If that is
unavailable, `words_in_window` spreads each transcript phrase evenly, which runs
about a second late.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

from .layout import OUT_H, OUT_W

HOOK_SECONDS = 1.8
WORDS_PER_CAPTION = 2

# Masked in burned-in text. The spoken audio is left alone: the cut is the
# creator's own, and bleeping it would be an edit the brief did not ask for.
# Any word containing one of these is masked ("bullshit", "fuckin", "motherfucker").
PROFANE_PARTS = ("fuck", "shit")
# Any word starting with one of these ("bitches", "retarded"), so "Scunthorpe" is left alone.
PROFANE_STARTS = ("bitch", "cunt", "nigg", "fagg", "retard")
# These only as whole words, so "Dickens" or "cockpit" are left alone.
PROFANITY = {
    "dick", "dicks", "dickhead", "asshole", "assholes", "bastard", "bastards", "pussy", "whore",
    "slut", "cock", "cocks", "twat", "wanker", "prick",
}


def is_profane(word: str) -> bool:
    w = word.lower().strip("'")
    return (w in PROFANITY or any(p in w for p in PROFANE_PARTS)
            or w.startswith(PROFANE_STARTS))


def mask(text: str) -> str:
    """Burned-in and posted text with swear words starred: 'shit' -> 's**t', 'fuck' -> 'f**k'.

    YouTube can age-restrict a Short whose on-screen text swears. The first and last letters
    stay so the word still reads.
    """
    def repl(m: re.Match) -> str:
        word = m.group(0)
        if not is_profane(word):
            return word
        core = word.strip("'")
        if len(core) <= 3:
            starred = core[0] + "*" * (len(core) - 1)
        else:
            starred = core[0] + "*" * (len(core) - 2) + core[-1]
        return word.replace(core, starred)

    return re.sub(r"[A-Za-z']+", repl, text)


def has_profanity(text: str) -> bool:
    return any(is_profane(w) for w in re.findall(r"[A-Za-z']+", text))


def words_in_window(segments, start: float, end: float) -> list[tuple[float, float, str]]:
    """(start, end, word) relative to `start`, spread evenly across each phrase."""
    out: list[tuple[float, float, str]] = []
    for seg in segments:
        s, e = float(seg.start), float(seg.end)
        words = str(seg.text).split()
        if not words or e <= start or s >= end:
            continue
        step = max(e - s, 0.01) / len(words)
        for i, word in enumerate(words):
            ws = s + i * step
            if start <= ws < end:
                out.append((ws - start, min(ws + step, end) - start, word))
    return out


MIN_STEP = 0.08


def no_overlap(events: list[tuple[float, float, str]], min_step: float = MIN_STEP) -> list[tuple[float, float, str]]:
    """Make caption events follow one another: never two on screen at once.

    Word times from the transcriber can touch or cross, and the hold added after a caption can run
    into the next one. Starts are kept in order and at least `min_step` apart; each caption ends
    where the next begins.
    """
    ordered = sorted(events, key=lambda e: e[0])
    starts: list[float] = []
    for s, _e, _x in ordered:
        starts.append(max(s, starts[-1] + min_step) if starts else s)
    out = []
    for i, (_s, e, text) in enumerate(ordered):
        end = min(e, starts[i + 1]) if i + 1 < len(ordered) else e
        out.append((starts[i], max(end, starts[i] + 0.04), text))
    return out


def caption_events(words, per_event: int = WORDS_PER_CAPTION) -> list[tuple[float, float, str]]:
    events = []
    for i in range(0, len(words), per_event):
        chunk = words[i:i + per_event]
        text = " ".join(w[2] for w in chunk)
        events.append((chunk[0][0], chunk[-1][1], text))
    # Hold a caption just past its last word, or up to the next one when they nearly touch.
    fixed = []
    for i, (s, e, text) in enumerate(events):
        end = max(e, s + 0.3)
        nxt = events[i + 1][0] if i + 1 < len(events) else None
        end = nxt if nxt is not None and nxt - end < 0.5 else end + 0.2
        fixed.append((s, end, text))
    return no_overlap(fixed)


def _t(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def ass_colour(hex_rgb: str, default: str = "FFFFFF") -> str:
    """'#RRGGBB' -> ASS '&H00BBGGRR'."""
    h = (hex_rgb or "").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = default
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _caption_text(text: str, upper: bool) -> str:
    text = _esc(mask(text))
    return text.upper() if upper else text


def highlight_events(words, per_event: int, base: str, highlight: str, *, upper: bool = True,
                     pop: bool = False) -> list[tuple[float, float, str]]:
    """One caption group shown word by word, the spoken word in `highlight` colour.

    Each event carries ASS override tags, so it is written as-is (see build_ass).
    """
    out: list[tuple[float, float, str]] = []
    for i in range(0, len(words), per_event):
        chunk = words[i:i + per_event]
        group_end = chunk[-1][1] + 0.2
        for j, (ws, we, _w) in enumerate(chunk):
            nxt = chunk[j + 1][0] if j + 1 < len(chunk) else group_end
            end = max(nxt, ws + 0.12)
            parts = []
            for k, (_s, _e, word) in enumerate(chunk):
                colour = highlight if k == j else base
                parts.append(f"{{\\c{colour}}}{_caption_text(word, upper)}")
            tags = "{\\fscx118\\fscy118\\t(0,110,\\fscx100\\fscy100)}" if pop and j == 0 else ""
            out.append((ws, end, tags + " ".join(parts)))
    return no_overlap(out)


def hook_y(cam_height: int, fit: bool, game_bottom: int | None = None) -> int:
    """Where the hook sits: on the seam under a facecam band, in the empty top band of a
    picture shown whole, and otherwise near the top, clear of a face in the middle."""
    if game_bottom:
        return game_bottom + 110               # just under the game, above the captions
    if cam_height:
        return OUT_H // 2 + 60
    if fit:
        return (OUT_H - OUT_W * 9 // 16) // 4
    return 300


def build_ass(*, hook: str, handle: str | None, events, duration: float,
              cam_height: int, style=None, words=None, fit: bool = False,
              game_bottom: int | None = None) -> str:
    """`style` (a taste.style.Style) and `words` are optional; without them the
    output is the plain default look."""
    cap = style.captions if style is not None else None
    hk = style.hook if style is not None else None
    cap_font = cap.font if cap else "Arial"
    cap_size = cap.size if cap else 66
    cap_col = ass_colour(cap.color) if cap else "&H00FFFFFF"
    cap_outline = cap.outline if cap else 6
    cap_margin = cap.margin_v if cap else 330
    upper = cap.uppercase if cap else True
    hook_size = hk.size if hk else 92
    hook_col = ass_colour(hk.color, "FFF000") if hk else "&H0000F0FF"
    hook_secs = hk.seconds if hk else HOOK_SECONDS
    hook_font = hk.font if hk else "Arial"
    banner = bool(hk and hk.banner)
    # A boxed title is ASS border style 3: the outline colour fills a box behind the text.
    hook_border = "3,14,0" if hk and hk.box else "1,8,3"
    hook_back = "&H30000000" if hk and hk.box else "&H00000000"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{cap_font},{cap_size},{cap_col},{cap_col},&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{cap_outline},2,2,90,150,{cap_margin},1
Style: Hook,{hook_font},{hook_size},{hook_col},{hook_col},{hook_back},&H80000000,1,0,0,0,100,100,0,0,{hook_border},5,70,70,0,1
Style: Handle,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,1,9,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook and (hk is None or hk.enabled):
        wrapped = "\\N".join(textwrap.wrap(_esc(mask(hook)).upper(), 16)[:3])
        lines.append(f"Dialogue: 1,{_t(0)},{_t(duration if banner else min(hook_secs, duration))},Hook,,0,0,0,,"
                     f"{{\\an5\\pos({OUT_W // 2},{hook_y(cam_height, fit, game_bottom)})\\fad(0,250)}}{wrapped}")
    if handle:
        lines.append(f"Dialogue: 2,{_t(0)},{_t(duration)},Handle,,0,0,0,,"
                     f"{{\\an9\\pos({OUT_W - 30},{cam_height + 18})\\alpha&H30&}}{_esc(handle)}")
    if cap is not None and not cap.enabled:
        pass
    elif cap is not None and cap.highlight and words:
        for s, e, text in highlight_events(words, cap.words_per_event, cap_col,
                                           ass_colour(cap.highlight), upper=upper, pop=cap.pop):
            lines.append(f"Dialogue: 0,{_t(s)},{_t(e)},Caption,,0,0,0,,{text}")
    else:
        pop_tag = "{\\fscx118\\fscy118\\t(0,110,\\fscx100\\fscy100)}" if cap is not None and cap.pop else ""
        for s, e, text in events:
            lines.append(f"Dialogue: 0,{_t(s)},{_t(e)},Caption,,0,0,0,,{pop_tag}{_caption_text(text, upper)}")
    return header + "\n".join(lines) + "\n"


def has_burned_subs(footage) -> bool:
    """Official trailers supplied as a ..._SUBS file already carry subtitles; ours would double them."""
    return re.search(r"(?<![a-z])subs(?![a-z])", Path(str(footage)).stem.lower()) is not None
