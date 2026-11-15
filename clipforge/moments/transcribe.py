"""Transcribing a source that came without captions (PRD section 6 step 1).

Two backends, chosen with TRANSCRIBE_BACKEND:

- `groq` - hosted whisper-large-v3-turbo. Far better than the local `base`
  model and far faster than this laptop. Yaish chose this on 12 Nov 2026.
- `local` - faster-whisper base/int8 on the CPU. Nothing leaves the machine.
  Measured here at 0.22x real time, so a 5-hour VOD takes about 66 minutes.

There is deliberately no automatic failover between them: which one runs is a
decision about where licensed campaign audio is allowed to go, not something to
decide at runtime.

Chunking is not optional for the hosted path. Groq's limit is 25 MB a file on
the free tier, and a 5h45 Twitch VOD is 519 MB even as audio. Groq downsamples
everything to 16 kHz mono before transcribing, so converting to exactly that
first costs nothing in accuracy and shrinks the upload enormously.

The output is the same `{start, end, text}` segment list the caption reader
produces, so everything downstream reads one shape.
"""
from __future__ import annotations

import json
import logging
import math
import tempfile
from pathlib import Path

from ..core import media
from ..core.config import Settings, get_settings
from ..ingest import captions as caps

log = logging.getLogger("clipforge.moments.transcribe")

GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"

# Groq's free tier refuses a file over 25 MB. 16 kHz mono FLAC of speech runs
# roughly 15-20 kB/s, so ten minutes is about 10 MB: comfortably inside the cap
# with room for a denser passage. A chunk boundary can split one word, which is
# immaterial for finding moments.
CHUNK_SECONDS = 600
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

# ffmpeg's segment muxer rounds up, so a 20.02 s input split at 10 s leaves a
# 0.02 s third file. Groq bills a 10 second minimum per request, so uploading
# slivers wastes both a request and the free tier's allowance.
MIN_CHUNK_SECONDS = 1.0


class TranscriptionUnavailable(RuntimeError):
    """No key or no backend. The caller should hold the job for a human."""


class TranscriptionFailed(RuntimeError):
    """The backend was reachable but would not transcribe this audio."""


def prepare_chunks(src: Path, work_dir: Path, *, chunk_seconds: int = CHUNK_SECONDS,
                   duration: float | None = None) -> list[tuple[Path, float]]:
    """Convert to 16 kHz mono FLAC and split it. Returns (path, start_seconds).

    One ffmpeg pass does both, so a five-hour stream is only decoded once.

    Each chunk's span is worked out from the source's duration and the chunk
    index, never by probing the chunk: the segment muxer writes FLAC without a
    total-sample count, so ffprobe reports no duration for every chunk but the
    last, and the last one's header claims the whole source's length.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    for stale in work_dir.glob("chunk*.flac"):
        stale.unlink()
    if duration is None:
        duration = media.probe(src).duration
    media.run([
        media._tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac",
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        str(work_dir / "chunk%05d.flac"),
    ], timeout=14400)
    found = sorted(work_dir.glob("chunk*.flac"))
    if not found:
        raise TranscriptionFailed(f"ffmpeg produced no audio chunks from {src.name}")

    chunks: list[tuple[Path, float]] = []
    for index, path in enumerate(found):
        start = index * float(chunk_seconds)
        span = min(float(chunk_seconds), max(0.0, duration - start))
        if span < MIN_CHUNK_SECONDS:
            log.info("dropping %s: only %.2fs of audio", path.name, span)
            path.unlink(missing_ok=True)
            continue
        chunks.append((path, start))
    if not chunks:
        raise TranscriptionFailed(
            f"{src.name} is only {duration:.2f}s; too short to transcribe"
        )
    return chunks


def log_rate_limits(headers) -> dict[str, str]:
    """Record what the provider says is left.

    Groq does not document the free tier's audio allowance, and a five-hour VOD
    is 35 requests, so the only way to know whether a run will finish is to read
    what it reports as it goes.
    """
    try:
        found = {k.lower(): v for k, v in headers.items()
                 if k.lower().startswith(("x-ratelimit", "retry-after"))}
    except Exception:
        return {}
    if found:
        log.info("provider limits: %s",
                 ", ".join(f"{k.removeprefix('x-ratelimit-')}={v}"
                           for k, v in sorted(found.items())))
    return found


def _groq_key(settings: Settings) -> str:
    # A dedicated transcription key wins; otherwise the Groq key already in use.
    key = settings.transcription_api_key or settings.groq_api_key
    if not key:
        raise TranscriptionUnavailable(
            "neither TRANSCRIPTION_API_KEY nor GROQ_API_KEY is set in .env, so hosted "
            "transcription cannot run; set one, or set TRANSCRIBE_BACKEND=local"
        )
    return key


def transcribe_chunk_groq(path: Path, settings: Settings, *, timeout: float = 600.0) -> list[caps.Segment]:
    """One chunk, as segments with times relative to the chunk's own start."""
    import httpx

    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise TranscriptionFailed(
            f"{path.name} is {size / 2**20:.1f} MB, over the {MAX_UPLOAD_BYTES / 2**20:.0f} MB "
            f"upload limit; lower TRANSCRIBE_CHUNK_SECONDS"
        )
    with path.open("rb") as fh:
        try:
            response = httpx.post(
                GROQ_AUDIO_URL,
                headers={"Authorization": f"Bearer {_groq_key(settings)}"},
                files={"file": (path.name, fh, "audio/flac")},
                data={
                    "model": GROQ_MODEL,
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": "segment",
                },
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise TranscriptionFailed(f"could not reach {GROQ_AUDIO_URL}: {exc}") from exc

    if response.status_code == 401:
        raise TranscriptionUnavailable("the transcription key was rejected (401)")
    if response.status_code == 413:
        raise TranscriptionFailed(
            f"{path.name} was refused as too large (413); lower TRANSCRIBE_CHUNK_SECONDS"
        )
    if response.status_code == 429:
        raise TranscriptionFailed(
            "rate limited by the provider (429); the free tier has a daily audio allowance"
        )
    if response.status_code >= 400:
        raise TranscriptionFailed(f"HTTP {response.status_code}: {response.text[:400]}")

    log_rate_limits(response.headers)
    try:
        data = response.json()
    except ValueError as exc:
        raise TranscriptionFailed(f"{path.name}: reply was not JSON") from exc

    segments: list[caps.Segment] = []
    for item in data.get("segments") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        segments.append(caps.Segment(start, end, text))
    if not segments:
        # A silent chunk is normal in a long stream, so this is not a failure.
        whole = (data.get("text") or "").strip()
        if whole:
            log.warning("chunk %s returned text but no timed segments", path.name)
    return segments


def _checkpoint_for(chunk: Path) -> Path:
    return chunk.with_suffix(".json")


def _load_checkpoint(chunk: Path) -> list[caps.Segment] | None:
    path = _checkpoint_for(chunk)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [caps.Segment(float(s["start"]), float(s["end"]), s["text"]) for s in data]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        log.warning("checkpoint %s is unreadable; redoing that chunk", path.name)
        return None


def _save_checkpoint(chunk: Path, segments: list[caps.Segment]) -> None:
    _checkpoint_for(chunk).write_text(
        json.dumps([{"start": s.start, "end": s.end, "text": s.text} for s in segments]),
        encoding="utf-8",
    )


def transcribe_groq(src: Path, settings: Settings, *, work_dir: Path | None = None,
                    beat=None) -> list[caps.Segment]:
    """Hosted transcription of a whole file, chunk by chunk.

    Each chunk's result is written beside it before the next is sent, so a rate
    limit at chunk 30 of 35 does not throw away the first 29 and spend the
    free tier's allowance on them twice. Pass a `work_dir` that survives the
    process for that to be worth anything.
    """
    chunk_seconds = settings.transcribe_chunk_seconds
    with tempfile.TemporaryDirectory() as tmp:
        target = work_dir or Path(tmp)
        chunks = prepare_chunks(src, target, chunk_seconds=chunk_seconds)
        log.info("transcribing %s in %d chunk(s) of %ds with %s",
                 src.name, len(chunks), chunk_seconds, GROQ_MODEL)
        out: list[caps.Segment] = []
        for index, (chunk, offset) in enumerate(chunks, start=1):
            if beat:
                beat()
            done = _load_checkpoint(chunk)
            if done is None:
                got = transcribe_chunk_groq(chunk, settings)
                _save_checkpoint(chunk, got)
                log.info("  chunk %d/%d: %d segment(s)", index, len(chunks), len(got))
            else:
                got = done
                log.info("  chunk %d/%d: %d segment(s) from an earlier run",
                         index, len(chunks), len(got))
            for segment in got:
                out.append(caps.Segment(segment.start + offset, segment.end + offset,
                                        segment.text))
            chunk.unlink(missing_ok=True)      # keep the disk flat as we go
        # Only once the whole file is through: until then they are the restart.
        for leftover in sorted(target.glob("chunk*.json")):
            leftover.unlink(missing_ok=True)
    return out


def transcribe_local(src: Path, settings: Settings, *, beat=None) -> list[caps.Segment]:
    """faster-whisper on the CPU. Nothing leaves the laptop."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise TranscriptionUnavailable("faster-whisper is not installed") from exc
    with tempfile.TemporaryDirectory() as tmp:
        wav = media.extract_audio_wav(src, Path(tmp) / "a.wav")
        model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
        log.info("transcribing %s locally with %s/int8", src.name, settings.whisper_model)
        segments, _info = model.transcribe(str(wav), vad_filter=True)
        out: list[caps.Segment] = []
        for segment in segments:               # a generator: this is the work
            text = (segment.text or "").strip()
            if text:
                out.append(caps.Segment(float(segment.start), float(segment.end), text))
            if beat and len(out) % 25 == 0:
                beat()
    return out


def transcribe(src: Path, settings: Settings | None = None, *, work_dir: Path | None = None,
               beat=None) -> tuple[list[caps.Segment], str]:
    """Transcribe with the configured backend. Returns (segments, backend name)."""
    settings = settings or get_settings()
    backend = settings.transcribe_backend
    if backend == "groq":
        return transcribe_groq(src, settings, work_dir=work_dir, beat=beat), f"groq {GROQ_MODEL}"
    if backend == "local":
        return (transcribe_local(src, settings, beat=beat),
                f"local faster-whisper {settings.whisper_model}")
    raise TranscriptionUnavailable(
        f"TRANSCRIBE_BACKEND={backend!r} is not 'groq' or 'local'"
    )


def estimate_chunks(duration_seconds: float, chunk_seconds: int = CHUNK_SECONDS) -> int:
    return max(1, math.ceil(duration_seconds / chunk_seconds))
