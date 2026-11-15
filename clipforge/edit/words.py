"""Word-level timing for one clip's audio.

The stream transcript is phrase-level, and Whisper phrase starts run late, so
captions spread across a phrase appeared 0.5-1 s after the words were spoken
(Yaish's first review of clip 1). Re-transcribing only the clip's ~30 s with word
timestamps costs one small request and removes the guess.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..core import media
from ..core.config import Settings
from ..moments import transcribe as tr

log = logging.getLogger("clipforge.words")

LEAD = 0.05          # show a word a touch early rather than a touch late


def extract_audio(footage: Path, *, ss: float, duration: float, dest: Path) -> Path:
    media.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{ss:.3f}", "-i", str(footage), "-t", f"{duration:.3f}",
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(dest)])
    return dest


def parse_words(data: dict, duration: float) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for w in data.get("words") or []:
        text = str(w.get("word") or "").strip()
        try:
            s, e = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not text or s >= duration:
            continue
        out.append((max(0.0, s - LEAD), min(max(e, s + 0.05), duration), text))
    return out


def transcribe_words(audio: Path, settings: Settings) -> list[tuple[float, float, str]]:
    import httpx

    with audio.open("rb") as fh:
        try:
            r = httpx.post(
                tr.GROQ_AUDIO_URL,
                headers={"Authorization": f"Bearer {tr._groq_key(settings)}"},
                files={"file": (audio.name, fh, "audio/flac")},
                data={"model": tr.GROQ_MODEL, "response_format": "verbose_json",
                      "timestamp_granularities[]": "word"},
                timeout=300.0)
        except httpx.HTTPError as exc:
            raise tr.TranscriptionFailed(f"could not reach the transcription service: {exc}") from exc
    if r.status_code >= 400:
        raise tr.TranscriptionFailed(f"HTTP {r.status_code}: {r.text[:300]}")
    return parse_words(r.json(), duration=1e9)


def for_clip(footage: Path, *, ss: float, duration: float, settings: Settings,
             work: Path) -> list[tuple[float, float, str]] | None:
    """Words relative to the clip's start, or None when it cannot be done."""
    cache = work / "words.json"
    if cache.is_file():
        return [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
    try:
        audio = extract_audio(footage, ss=ss, duration=duration, dest=work / "clip-audio.flac")
        found = [w for w in transcribe_words(audio, settings) if w[0] < duration]
    except (tr.TranscriptionUnavailable, tr.TranscriptionFailed, media.MediaError) as exc:
        log.warning("word timing unavailable, using phrase-level estimates: %s", exc)
        return None
    if not found:
        return None
    cache.write_text(json.dumps(found), encoding="utf-8")
    return found
