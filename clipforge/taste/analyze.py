"""Measure one reference Short, and look at its frames.

Two kinds of finding are kept apart, as the taste-book guide asks:
  * measured: cut times, loudness peaks, word pace (numbers from ffmpeg and Whisper);
  * looked at: what a vision model reports on a few frames, with each frame's second.
What cannot be measured here (zoom, music, sound effects) is not guessed at.
Results are cached per video so an interrupted build resumes.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import statistics
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, Field

from ..core import media
from ..core.config import Settings
from ..edit import words as words_mod
from ..moments import signals

log = logging.getLogger("clipforge.taste.analyze")

SCENE_THRESHOLD = 0.30
VISION_MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-flash-lite-latest")
VISION_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
FRAME_POINTS = (0.4, 0.25, 0.5, 0.75)        # first is a fixed second, the rest fractions of the video


class Captions(BaseModel):
    present: bool = False
    position: str = Field("none", description="top | middle | lower_third | bottom | none")
    colour: str = Field("", description="main caption colour, one word")
    highlight_word: bool = Field(False, description="the spoken word is coloured or sized differently from its neighbours")
    uppercase: bool = False
    words_on_screen: int = 0
    outline: str = Field("none", description="outline | box | none")
    font_style: str = Field("unknown", description="heavy_block (very thick, like Arial Black) | "
                            "condensed (tall and narrow, like Impact) | rounded (soft, like Comic Sans) | "
                            "standard_bold (an ordinary bold sans) | handwritten | serif | unknown")
    size: str = Field("unknown", description="small | medium | large: how much of the frame width "
                      "one caption line covers (small under a third, large over two thirds)")


class Graphic(BaseModel):
    kind: str = Field(description="emoji | arrow | sticker | progress_bar | meme_image | logo | text_banner | other")
    at_seconds: float = 0.0


class FrameReport(BaseModel):
    layout: str = Field("other", description="full_gameplay | facecam_top_game_bottom | facecam_corner | "
                                             "split_with_other_video | talking_head | other")
    captions: Captions = Captions()
    hook_title: bool = Field(False, description="a large title or hook text sits on screen in the first frame")
    hook_position: str = ""
    title_stays: bool = Field(False, description="the big title from the first frame is still on screen "
                              "in the later frames")
    title_box: bool = Field(False, description="that title sits on a solid coloured box or banner")
    graphics: list[Graphic] = Field(default_factory=list)
    note: str = Field("", description="one short sentence on how the edit looks")


def cut_times(video: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Seconds where the picture changes sharply (scene-change score above `threshold`)."""
    proc = subprocess.run(
        [media._tool("ffmpeg"), "-hide_banner", "-loglevel", "info", "-i", str(video), "-an",
         "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300)
    times = [float(t) for t in re.findall(r"pts_time:([0-9.]+)", proc.stderr or "")]
    return [round(t, 2) for t in times if t > 0.2]


def frame_times(duration: float) -> list[float]:
    first, *rest = FRAME_POINTS
    return [round(min(first, duration / 4), 2)] + [round(duration * f, 2) for f in rest]


def extract_frames(video: Path, times: list[float], dest: Path) -> list[tuple[float, Path]]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for t in times:
        path = dest / f"f_{t:.2f}.jpg"
        media.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                   "-i", str(video), "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(path)])
        if path.is_file():
            out.append((t, path))
    return out


PROMPT = (
    "These are frames from ONE vertical YouTube Short, each labelled with its second. Describe only what "
    "you can see in the frames. Do not guess about anything not visible. Report the editing, not the "
    "content: the layout of the picture, the on-screen captions (position, colour, whether the spoken word "
    "is highlighted, how many words are on screen at once, the caption font style and size), any big hook "
    "title (whether it stays on screen in later frames and whether it sits on a solid box), and added "
    "graphics such as "
    "emoji, arrows, stickers, progress bars, meme images or logos with the second they appear. "
    "Answer with JSON only, matching this schema:\n"
)


def ask(frames: list[tuple[float, Path]], prompt: str, model_cls, settings: Settings, *, post=None):
    """One vision call over labelled frames; the answer is validated as `model_cls`.

    `post` is injectable for tests. Tries each model in VISION_MODELS, retrying busy ones.
    """
    import httpx

    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is empty in .env; frames cannot be inspected")
    content: list[dict] = [{"type": "text", "text": prompt + json.dumps(model_cls.model_json_schema())}]
    for t, path in frames:
        content.append({"type": "text", "text": f"Frame at {t:.2f} s:"})
        b64 = base64.b64encode(path.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    last = "no vision model answered"
    for model in VISION_MODELS:
        body = {"model": model, "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_object"}}
        send = post or (lambda b: httpx.post(VISION_URL, json=b, timeout=60.0,
                                             headers={"Authorization": f"Bearer {settings.gemini_api_key}"}))
        for attempt in range(2):
            try:
                r = send(body)
            except httpx.HTTPError as exc:
                last = f"{model}: {type(exc).__name__}"
                break
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{model}: HTTP {r.status_code}"
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                last = f"{model}: HTTP {r.status_code}"
                break
            try:
                return model_cls.model_validate(json.loads(r.json()["choices"][0]["message"]["content"]))
            except (ValueError, KeyError, IndexError) as exc:
                last = f"{model}: unreadable answer ({type(exc).__name__})"
                break
    raise RuntimeError(f"vision model unavailable: {last}")


def look(frames: list[tuple[float, Path]], settings: Settings, *, post=None) -> FrameReport:
    """What the editing of a reference Short looks like, from a few frames."""
    return ask(frames, PROMPT, FrameReport, settings, post=post)


def measure(video: Path, settings: Settings, *, work: Path) -> dict:
    """Everything measurable about one video, as plain data."""
    info = media.probe(video)
    duration = info.duration
    cuts = cut_times(video)
    gaps = [b - a for a, b in zip([0.0, *cuts], [*cuts, duration])]
    series = signals.loudness_series(video, window=0.25)
    loud = signals.peaks(series, z=2.0, min_gap=1.5)
    out = {"seconds": round(duration, 2), "cuts": cuts,
           "cuts_per_10s": round(len(cuts) / duration * 10, 2) if duration else 0.0,
           "median_shot_s": round(statistics.median(gaps), 2) if gaps else round(duration, 2),
           "first_cut_s": cuts[0] if cuts else None,
           "loud_peaks": [round(p.start, 2) for p in loud]}
    try:
        audio = words_mod.extract_audio(video, ss=0.0, duration=duration, dest=work / "audio.flac")
        found = [w for w in words_mod.transcribe_words(audio, settings) if w[0] < duration]
        if found:
            out["words_per_s"] = round(len(found) / max(found[-1][1] - found[0][0], 1.0), 2)
            out["first_word_s"] = round(found[0][0], 2)
        audio.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("no word timing for %s: %s", video.name, exc)
    return out


def analyze(video: Path, settings: Settings, *, cache: Path, vision=look) -> dict:
    """Measure and look at one video; the result is cached as JSON."""
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    work = cache.parent / f"{cache.stem}.tmp"
    work.mkdir(parents=True, exist_ok=True)
    data = measure(video, settings, work=work)
    frames = extract_frames(video, frame_times(data["seconds"]), work)
    data["frame_times"] = [t for t, _ in frames]
    try:
        data["looked"] = vision(frames, settings).model_dump()
    except Exception as exc:
        log.warning("frames of %s not inspected: %s", video.name, exc)
        data["looked"] = None
    for f in work.glob("*"):
        f.unlink(missing_ok=True)
    work.rmdir()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data
