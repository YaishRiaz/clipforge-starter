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
PROFANITY = {
    "fuck", "fucking", "fucked", "fucker", "motherfucker", "shit", "shitty", "bitch",
    "asshole", "bastard", "dick", "cunt", "nigga", "nigger", "faggot", "retard",
}


def mask(text: str) -> str:
    def repl(m: re.Match) -> str:
        word = m.group(0)
        return word[0] + "*" * (len(word) - 1) if word.lower() in PROFANITY else word

    return re.sub(r"[A-Za-z']+", repl, text)


def has_profanity(text: str) -> bool:
    return any(w.lower() in PROFANITY for w in re.findall(r"[A-Za-z']+", text))


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
    return fixed


def _t(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(*, hook: str, handle: str | None, events, duration: float,
              cam_height: int) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,66,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,2,2,90,150,330,1
Style: Hook,Arial,92,&H0000F0FF,&H0000F0FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,5,70,70,0,1
Style: Handle,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,1,9,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        wrapped = "\\N".join(textwrap.wrap(_esc(mask(hook)).upper(), 16)[:3])
        lines.append(f"Dialogue: 1,{_t(0)},{_t(min(HOOK_SECONDS, duration))},Hook,,0,0,0,,"
                     f"{{\\an5\\pos({OUT_W // 2},{OUT_H // 2 + 60})\\fad(0,250)}}{wrapped}")
    if handle:
        lines.append(f"Dialogue: 2,{_t(0)},{_t(duration)},Handle,,0,0,0,,"
                     f"{{\\an9\\pos({OUT_W - 30},{cam_height + 18})\\alpha&H30&}}{_esc(handle)}")
    for s, e, text in events:
        lines.append(f"Dialogue: 0,{_t(s)},{_t(e)},Caption,,0,0,0,,{_esc(mask(text)).upper()}")
    return header + "\n".join(lines) + "\n"


def has_burned_subs(footage) -> bool:
    """Official trailers supplied as a ..._SUBS file already carry subtitles; ours would double them."""
    return re.search(r"(?<![a-z])subs(?![a-z])", Path(str(footage)).stem.lower()) is not None
