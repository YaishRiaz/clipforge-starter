"""The brief check that runs last (PRD section 7).

FAIL holds the clip with the reason shown. WARN is for Yaish to look at and
does not hold it. Nothing here guesses at things a machine cannot see: other
streamers' footage and "low quality" are for a human watching the clip.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..ingest.rules import BriefRules
from .layout import OUT_H, OUT_W, Layout
from .subs import has_profanity

DEFAULT_MAX_SECONDS = 60.0


@dataclass(frozen=True)
class Finding:
    level: str             # FAIL | WARN
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.level} {self.code}: {self.message}"


def check(*, rules: BriefRules | None, layout: Layout, meta: dict, duration: float,
          width: int | None, height: int | None, has_audio: bool, overlay_burned: bool,
          hook: str, spoken: str, cam_blank: bool = False) -> list[Finding]:
    out: list[Finding] = []

    def fail(code: str, msg: str) -> None:
        out.append(Finding("FAIL", code, msg))

    def warn(code: str, msg: str) -> None:
        out.append(Finding("WARN", code, msg))

    if (width, height) != (OUT_W, OUT_H):
        fail("size", f"clip is {width}x{height}, expected {OUT_W}x{OUT_H}")
    if not has_audio:
        fail("audio", "the clip has no audio track")

    if rules:
        ok, why = rules.clip_length_ok(duration)
        if not ok:
            fail("length", why)
        if rules.clip_max_seconds is None and duration > DEFAULT_MAX_SECONDS:
            fail("length", f"clip is {duration:.1f}s; the default ceiling is "
                           f"{DEFAULT_MAX_SECONDS:.0f}s")
        if rules.required_aspect_ratio and rules.required_aspect_ratio.replace(" ", "") != "9:16":
            warn("aspect", f"the brief asks for {rules.required_aspect_ratio}; clips are 9:16")
        text = ((meta.get("description") or "") + " " + (meta.get("title") or "")).lower()
        for tag in rules.required_hashtags:
            if tag.lstrip("#").lower() not in text:
                fail("hashtag", f"required hashtag {tag} is missing")
        for mention in rules.required_mentions:
            if mention.lstrip("@").lower() not in text:
                fail("mention", f"required mention {mention} is missing")
        if rules.disclosure_text and rules.disclosure_text.lower() not in text:
            fail("disclosure", "the required disclosure text is missing")
        if rules.required_names_any and not any(n.lower() in text for n in rules.required_names_any):
            fail("caption", "the caption must mention one of: " + ", ".join(rules.required_names_any))
        if rules.required_caption and rules.required_caption.lower() not in text:
            fail("caption", f"the required caption '{rules.required_caption}' is missing")
        handle = meta.get("handle")
        if rules.caption_requirements and handle and \
                handle.lower() not in (meta.get("description") or "").lower():
            fail("caption", f"the caption must include {handle}")
        if rules.required_overlay and not overlay_burned:
            fail("overlay", "the brief requires an overlay and none was burned in")
        if rules.min_creator_frame_share and layout.cam_share < rules.min_creator_frame_share:
            fail("creator_share", f"the creator fills {layout.cam_share:.0%} of the frame; "
                                  f"the brief wants {rules.min_creator_frame_share:.0%}")
        if rules.pre_approval:
            warn("pre_approval",
                 "the brief wants the campaign team to approve a draft before posting")
        if rules.banned_edits:
            warn("banned_edits", "check by eye, the machine cannot: " + "; ".join(rules.banned_edits))

    if layout.facecam_required and layout.facecam is None and not layout.cam_full:
        fail("facecam", layout.cam_note or
             "the brief requires the facecam visible and no facecam layout is configured")
    if cam_blank:
        fail("facecam", "the facecam band is black; the layout's facecam box misses the camera in this stream")
    if has_profanity(hook):
        warn("hook_profanity", "the hook contains profanity; it is masked in the burned-in text")
    if has_profanity(spoken):
        warn("spoken_profanity", "the clip's audio contains profanity, which can limit monetisation")
    return out


def summary(findings: list[Finding]) -> tuple[bool, str]:
    ok = not any(f.level == "FAIL" for f in findings)
    return ok, "\n".join(str(f) for f in findings)
