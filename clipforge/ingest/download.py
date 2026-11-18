"""Downloading approved source footage.

PRD section 5 step 2: only what the brief names, capped at 1080p, and anything
longer than MAX_SOURCE_HOURS is held until Yaish approves it. The approval test
itself lives in `sources.register`; by the time a job reaches this module the
footage is already approved.

yt-dlp is called as a subprocess rather than imported so a download cannot take
the worker down with it, and so the job stays resumable: a part-finished
download is just a file that is not there yet.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.config import Settings

log = logging.getLogger("clipforge.ingest.download")


class DownloadError(RuntimeError):
    pass


class TooLong(RuntimeError):
    """Longer than the configured cap; hold it for Yaish rather than spend the disk."""


@dataclass
class Downloaded:
    path: Path
    title: str | None
    creator: str | None
    duration: float | None
    published_at: str | None
    language: str | None


def _ytdlp() -> list[str]:
    """yt-dlp as an executable, else through this interpreter's module."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    import sys
    try:
        import yt_dlp  # noqa: F401
    except ImportError as exc:
        raise DownloadError(
            "yt-dlp is not installed; run pip install -r requirements.txt"
        ) from exc
    return [sys.executable, "-m", "yt_dlp"]


BEAT_EVERY = 30.0


def _run(args: list[str], *, timeout: float, beat=None, beat_every: float = BEAT_EVERY) -> str:
    """Run a command; call `beat` while waiting so a long download is not mistaken for a stall."""
    if beat is None:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    else:
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        waited = 0.0
        while True:
            try:
                out, err = child.communicate(timeout=beat_every)
                break
            except subprocess.TimeoutExpired:
                waited += beat_every
                if waited >= timeout:
                    child.kill()
                    child.communicate()
                    raise
                beat()
        code = child.returncode
    if code != 0:
        raise DownloadError((err or out or "").strip()[-1500:])
    return out


def metadata(url: str, *, timeout: float = 120) -> dict:
    """Read a video's metadata without downloading it."""
    out = _run(_ytdlp() + ["--dump-single-json", "--no-playlist", "--skip-download", url],
               timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"yt-dlp returned no metadata for {url}") from exc


def _published(info: dict) -> str | None:
    raw = info.get("upload_date")       # YYYYMMDD
    if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return None


def fetch(url: str, dest_dir: Path, settings: Settings, *,
          timeout: float = 10800, allow_long: bool = False,
          audio_only: bool = False, beat=None) -> Downloaded:
    """Download one video plus any captions it publishes.

    Raises TooLong before spending any bandwidth when the video is over the cap,
    unless Yaish has already approved this source's length.
    """
    from . import kick

    if kick.is_kick(url):
        try:
            return kick.fetch(url, dest_dir, settings, audio_only=audio_only, allow_long=allow_long,
                              beat=beat)
        except kick.KickError as exc:
            raise DownloadError(str(exc)) from None
    dest_dir.mkdir(parents=True, exist_ok=True)
    info = metadata(url)

    duration = info.get("duration")
    cap_seconds = settings.max_source_hours * 3600
    if not allow_long and isinstance(duration, (int, float)) and duration > cap_seconds:
        raise TooLong(
            f"{duration / 3600:.1f} h is over the {settings.max_source_hours:g} h cap "
            f"(MAX_SOURCE_HOURS); approve it with: python -m clipforge source-release <id>"
        )

    video_id = info.get("id") or "source"
    template = str(dest_dir / f"{video_id}.%(ext)s")
    if audio_only:
        # Everything up to moment selection needs the audio only, and for a
        # long stream that is a thirtieth of the bytes. Nothing is merged, so
        # --merge-output-format must not be passed: it only takes video
        # containers and yt-dlp rejects an audio one outright.
        fmt = "Audio_Only/bestaudio/best"
        merge: list[str] = []
    else:
        # Cap the height rather than take the best format: a 4K source would
        # cost disk and decode time for a 1080x1920 clip that cannot use it.
        fmt = (f"bestvideo[height<={settings.max_height}]+bestaudio/"
               f"best[height<={settings.max_height}]/best")
        merge = ["--merge-output-format", "mp4"]
    args = _ytdlp() + [
        "--no-playlist", "--no-progress", "--newline",
        "-f", fmt, *merge,
        "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
        "--convert-subs", "vtt",
        "--retries", "5", "--fragment-retries", "10",
        "-o", template, url,
    ]
    log.info("downloading %s (%s) into %s", url,
             "audio only" if audio_only else f"video up to {settings.max_height}p", dest_dir)
    try:
        _run(args, timeout=timeout, beat=beat)
    except DownloadError as exc:
        # Captions are optional (we transcribe anyway); a rate-limited caption fetch must not sink the video.
        if "subtitles" not in str(exc).lower():
            raise
        log.warning("caption download failed, retrying without captions: %s", str(exc)[-200:])
        plain = [a for a in args if a not in ("--write-subs", "--write-auto-subs")]
        i = plain.index("--sub-langs")
        del plain[i:i + 2]
        i = plain.index("--convert-subs")
        del plain[i:i + 2]
        _run(plain, timeout=timeout, beat=beat)

    produced = [
        p for p in dest_dir.glob(f"{video_id}.*")
        if p.suffix.lower() not in (".vtt", ".srt", ".part", ".ytdl", ".json")
    ]
    if not produced:
        raise DownloadError(f"yt-dlp wrote no video file for {url}")
    path = max(produced, key=lambda p: p.stat().st_size)
    return Downloaded(
        path=path,
        title=info.get("title"),
        creator=info.get("uploader") or info.get("channel"),
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        published_at=_published(info),
        language=info.get("language"),
    )
