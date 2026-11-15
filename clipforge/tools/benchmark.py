"""Phase 0 benchmark: how fast is this machine at the two heavy steps?

1. Encode a 45-second 1080x1920 clip (x264 veryfast, CRF 23).
2. Transcribe audio with faster-whisper (base, int8, CPU), if it is installed
   and a real speech file is supplied with --media.

The synthetic test pattern is easier to encode than gameplay, so treat the
encode number as a lower bound and re-run with --media on real footage.
Results go to docs/benchmark.json and feed the daily budget on the dashboard.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .sysinfo import collect

CLIP_SECONDS = 45
WORK_HOURS_PER_DAY = 20          # leave headroom for the laptop's owner
CLIPS_PER_SOURCE_HOUR = 6        # planning assumption, tune after week one
SIGNALS_OVERHEAD_RTF = 0.10      # audio/scene analysis, as a share of source time
ASSUMED_TRANSCRIBE_RTF = 1.0     # used only when transcription was not measured

# A sample that is mostly silence transcribes almost instantly and would report
# an RTF near zero, which turns the daily budget into a fantasy number. Long
# videos often open with a silent title card, so refuse the measurement instead
# of believing it. Ordinary speech runs 120-160 words per minute.
MIN_WORDS_PER_MINUTE = 30


def _run(cmd: list[str]) -> float:
    start = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd[:6])} ...\n{proc.stderr[-1500:]}")
    return time.perf_counter() - start


def _make_synthetic(path: Path) -> None:
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "60", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ])


def bench_encode(source: Path, out: Path, start: float = 0.0) -> dict[str, Any]:
    seconds = _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start or 5), "-i", str(source), "-t", str(CLIP_SECONDS),   # seek before decode
        "-vf", "crop=ih*9/16:ih,scale=1080:1920", "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
    ])
    return {
        "clip_seconds": CLIP_SECONDS,
        "encode_seconds": round(seconds, 1),
        "encode_rtf": round(seconds / CLIP_SECONDS, 2),   # 1.0 = real time
        "output_mb": round(out.stat().st_size / 2**20, 1),
    }


def bench_transcribe(media: Path, minutes: float, start: float = 0.0) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return {"measured": False, "reason": "faster-whisper is not installed"}
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(media),
            "-t", str(int(minutes * 60)), "-vn", "-ac", "1", "-ar", "16000", str(wav),
        ])
        load_start = time.perf_counter()
        model = WhisperModel("base", device="cpu", compute_type="int8")
        load_seconds = time.perf_counter() - load_start
        t0 = time.perf_counter()   # not `start`: that is the seek offset above
        segments, info = model.transcribe(str(wav), vad_filter=True)
        words = sum(len(s.text.split()) for s in segments)   # generator: forces the work
        seconds = time.perf_counter() - t0
    audio = float(info.duration) or minutes * 60
    result = {
        "measured": True,
        "model": "base/int8/cpu",
        "audio_seconds": round(audio, 1),
        "transcribe_seconds": round(seconds, 1),
        "transcribe_rtf": round(seconds / audio, 2),
        "model_load_seconds": round(load_seconds, 1),
        "words": words,
        "words_per_minute": round(words / (audio / 60), 1) if audio else 0.0,
        "sample_start_seconds": start,
    }
    if result["words_per_minute"] < MIN_WORDS_PER_MINUTE:
        result["measured"] = False
        result["reason"] = (
            f"only {words} words in {audio / 60:.1f} min of audio from {start:.0f}s "
            f"({result['words_per_minute']} words/min): this sample is near-silent, so the "
            f"timing says nothing about real footage. Re-run with --start at a section "
            f"that has speech."
        )
    return result


def daily_budget(encode: dict[str, Any], transcribe: dict[str, Any]) -> dict[str, Any]:
    rtf = transcribe.get("transcribe_rtf") if transcribe.get("measured") else ASSUMED_TRANSCRIBE_RTF
    encode_min_per_source_min = (CLIPS_PER_SOURCE_HOUR / 60) * (encode["encode_seconds"] / 60)
    cost = rtf + SIGNALS_OVERHEAD_RTF + encode_min_per_source_min   # machine-min per source-min
    source_minutes = WORK_HOURS_PER_DAY * 60 / cost
    return {
        "work_hours_per_day": WORK_HOURS_PER_DAY,
        "machine_minutes_per_source_minute": round(cost, 2),
        "source_minutes_per_day": int(source_minutes),
        "clips_per_day_capacity": int(source_minutes / 60 * CLIPS_PER_SOURCE_HOUR),
        "transcribe_rtf_used": rtf,
        "transcribe_rtf_is_assumed": not transcribe.get("measured", False),
        "note": "Captions supplied with footage skip transcription entirely, which raises this budget.",
    }


def run(root: Path, media: Path | None = None, minutes: float = 10.0,
        start: float = 0.0) -> dict[str, Any]:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed or not on PATH.")
    result: dict[str, Any] = {"system": collect(root), "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if media is None:
            source = tmpdir / "synthetic.mp4"
            _make_synthetic(source)
            result["source"] = "synthetic test pattern (lower bound; re-run with --media)"
        else:
            source = media
            result["source"] = "supplied media"
        result["encode"] = bench_encode(source, tmpdir / "clip.mp4", start)
    if media is not None:
        result["transcribe"] = bench_transcribe(media, minutes, start)
    else:
        result["transcribe"] = {"measured": False, "reason": "needs real speech; pass --media FILE"}
    result["budget"] = daily_budget(result["encode"], result["transcribe"])
    out = root / "docs" / "benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
