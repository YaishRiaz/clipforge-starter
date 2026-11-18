"""Cheap local signals that say where something happened (PRD section 6 step 2).

Three of them:

- **Loudness** over time: shouting, laughter, crashes, a crowd reacting.
- **Speech rate** over time, from the transcript: a streamer talking fast is
  usually mid-event.
- **Scene-change density**, for video sources only. An audio-only source has
  none, which is normal rather than an error.

All of it runs on the laptop with no model and no network. The audio is read as
raw PCM streamed through a pipe and reduced a block at a time: a five-hour VOD
is 663 MB as 16-bit mono PCM, which must never be held in memory at once
(PRD section 9: stream files, peak under 4 GB).
"""
from __future__ import annotations

import logging
import math
import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core import media
from ..ingest import captions as caps

log = logging.getLogger("clipforge.moments.signals")

SAMPLE_RATE = 16000
BLOCK_SECONDS = 60          # 1.92 MB of int16 per read


@dataclass
class Signal:
    """A value per time window, in order."""

    name: str
    window: float
    times: list[float]
    values: list[float]

    def __len__(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0

    @property
    def stdev(self) -> float:
        if len(self.values) < 2:
            return 0.0
        mu = self.mean
        return math.sqrt(sum((v - mu) ** 2 for v in self.values) / (len(self.values) - 1))

    @property
    def median(self) -> float:
        if not self.values:
            return 0.0
        return statistics.median(self.values)

    @property
    def mad(self) -> float:
        """Median absolute deviation, scaled to compare with a st. dev.

        Loudness needs this rather than a standard deviation. Measured on a real
        5h45 Twitch VOD: silence floors at -109 dBFS while speech sits near -26,
        which gives mean -30 and st. dev 15, so a mean+1.8sd threshold lands at
        -3 dBFS -- above the loudest window in the whole stream (-15), and
        nothing could ever qualify. A MAD measured from the upper half only (about
        7.7 here) puts the cutoff near -14 dBFS, where the shouting actually is.
        """
        if len(self.values) < 2:
            return 0.0
        mid = self.median
        # Upper half only: the question is how far above normal is "loud", and a
        # floor of silence below the median says nothing about that. Measured on
        # the same VOD, the two-sided MAD came to 10.2 (the silence/speech gap)
        # and put a 1.8 threshold above the loudest window; the upper half is
        # ~7.7 and puts it at the 99th percentile. 1.4826 makes the result
        # comparable to a standard deviation for normal data.
        above = [v - mid for v in self.values if v >= mid]
        return statistics.median(above) * 1.4826

    def value_at(self, seconds: float) -> float:
        if not self.values:
            return 0.0
        index = min(len(self.values) - 1, max(0, int(seconds / self.window)))
        return self.values[index]


def loudness_series(src: Path, *, window: float = 1.0,
                    sample_rate: int = SAMPLE_RATE, beat=None) -> Signal:
    """RMS loudness in dBFS per window, streamed so memory stays flat."""
    import numpy as np

    samples_per_window = max(1, int(sample_rate * window))
    args = [
        media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "-",
    ]
    times: list[float] = []
    values: list[float] = []
    carry = np.empty(0, dtype=np.int16)
    block_bytes = sample_rate * 2 * BLOCK_SECONDS

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        while True:
            raw = proc.stdout.read(block_bytes)
            if not raw:
                break
            block = np.frombuffer(raw, dtype=np.int16)
            if carry.size:
                block = np.concatenate((carry, block))
            whole = block.size // samples_per_window
            if whole:
                usable = block[:whole * samples_per_window].astype(np.float32)
                frames = usable.reshape(whole, samples_per_window)
                # RMS in dBFS, floored so silence does not become -inf.
                rms = np.sqrt(np.mean(np.square(frames / 32768.0), axis=1))
                db = 20.0 * np.log10(np.maximum(rms, 1e-6))
                start = len(values)
                values.extend(float(x) for x in db)
                times.extend((start + i) * window for i in range(whole))
            carry = block[whole * samples_per_window:]
            if beat:
                beat()
        proc.stdout.close()
        code = proc.wait(timeout=60)
        if code != 0:
            err = (proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "")
            raise media.MediaError(f"ffmpeg failed reading {src.name}: {err[-500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stderr:
            proc.stderr.close()
    log.info("loudness: %d window(s) of %.1fs from %s", len(values), window, src.name)
    return Signal("loudness_dbfs", window, times, values)


def speech_rate_series(segments: list[caps.Segment], *, window: float = 10.0,
                       duration: float | None = None) -> Signal:
    """Words per minute per window, from the transcript."""
    if duration is None:
        duration = max((s.end for s in segments), default=0.0)
    count = max(1, int(math.ceil(duration / window)))
    words = [0.0] * count
    for segment in segments:
        span = max(segment.end - segment.start, 1e-6)
        n = len(segment.text.split())
        if not n:
            continue
        # Spread a segment's words over the windows it actually covers, so a
        # long sentence does not spike the window it happens to start in.
        per_second = n / span
        position = segment.start
        while position < segment.end:
            index = int(position / window)
            if index >= count:
                break
            edge = min(segment.end, (index + 1) * window)
            words[index] += per_second * (edge - position)
            position = edge
    values = [w * (60.0 / window) for w in words]
    times = [i * window for i in range(count)]
    return Signal("words_per_minute", window, times, values)


_SHOWINFO_TIME = re.compile(r"pts_time:([0-9.]+)")


def scene_change_times(src: Path, *, threshold: float = 0.4,
                       timeout: float = 14400, beat=None) -> list[float]:
    """Seconds at which the picture changes sharply. Video sources only.

    The picture is shrunk to 320 px first (a cut is just as visible, and a 4K hour would take
    far longer than the worker may go without a heartbeat); `beat` is called while ffmpeg runs.
    """
    probe = media.probe(src)
    if not probe.has_video:
        log.info("%s has no video track, so no scene changes", src.name)
        return []
    import tempfile
    import time

    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([
            media._tool("ffmpeg"), "-hide_banner", "-nostats",
        "-i", str(src),
            "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-",
        ], stdout=subprocess.DEVNULL, stderr=err)
        t0 = time.monotonic()
        while True:
            try:
                proc.wait(timeout=20)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - t0 > timeout:
                    proc.kill()
                    raise
                if beat:
                    beat()
        err.seek(0)
        text = err.read().decode("utf-8", "replace")
    # showinfo writes to stderr; a non-zero exit with output is still usable.
    found = [float(m) for m in _SHOWINFO_TIME.findall(text)]
    log.info("scene changes: %d above %.2f in %s", len(found), threshold, src.name)
    return sorted(found)


def density(times: list[float], *, window: float, duration: float) -> Signal:
    """Events per window, for turning scene changes into a comparable signal."""
    count = max(1, int(math.ceil(duration / window)))
    values = [0.0] * count
    for t in times:
        index = int(t / window)
        if 0 <= index < count:
            values[index] += 1.0
    return Signal("events_per_window", window, [i * window for i in range(count)], values)


@dataclass
class Peak:
    start: float
    end: float
    value: float
    z: float
    signal: str
    note: str = ""


def threshold_for(signal: Signal, z: float) -> float | None:
    """Where "stands out" begins for this signal, or None if nothing can.

    Robust by default: centre on the median and spread by the scaled MAD, so a
    long tail of silence cannot drag the threshold out of reach. Falls back to
    mean and standard deviation when the MAD is zero, which happens for a
    signal that is mostly one value.
    """
    spread = signal.mad
    centre = signal.median
    if spread <= 0:
        spread = signal.stdev
        centre = signal.mean
    if spread <= 0:
        return None
    return centre + z * spread


def peaks(signal: Signal, *, z: float = 1.5, min_gap: float = 5.0,
          top_n: int | None = None) -> list[Peak]:
    """Windows that stand out, merged when adjacent and spaced by min_gap.

    `z` is in robust deviations above this signal's own centre, so the same
    threshold works on a quiet stream and a loud one.
    """
    if len(signal) < 3:
        return []
    cutoff = threshold_for(signal, z)
    if cutoff is None:
        return []
    centre = signal.median if signal.mad > 0 else signal.mean
    spread = signal.mad if signal.mad > 0 else signal.stdev
    runs: list[Peak] = []
    index = 0
    while index < len(signal.values):
        if signal.values[index] < cutoff:
            index += 1
            continue
        start = index
        best = signal.values[index]
        while index < len(signal.values) and signal.values[index] >= cutoff:
            best = max(best, signal.values[index])
            index += 1
        runs.append(Peak(
            start=signal.times[start],
            end=signal.times[index - 1] + signal.window,
            value=best,
            z=(best - centre) / spread,
            signal=signal.name,
        ))

    # Merge peaks closer together than min_gap: two shouts a second apart are
    # one moment, not two.
    merged: list[Peak] = []
    for peak in runs:
        if merged and peak.start - merged[-1].end < min_gap:
            last = merged[-1]
            merged[-1] = Peak(last.start, peak.end, max(last.value, peak.value),
                              max(last.z, peak.z), last.signal)
        else:
            merged.append(peak)

    merged.sort(key=lambda p: p.z, reverse=True)
    if top_n is not None:
        merged = merged[:top_n]
    merged.sort(key=lambda p: p.start)
    return merged
