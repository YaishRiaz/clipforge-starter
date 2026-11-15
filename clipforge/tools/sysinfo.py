"""Describe the machine: OS, CPU, RAM, free disk, ffmpeg. Stdlib only."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _ram_gb() -> float | None:
    try:
        import psutil  # type: ignore

        return round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:
        pass
    try:  # Linux
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 2**20, 1)
    except OSError:
        pass
    try:  # macOS
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return round(int(out.stdout.strip()) / 2**30, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:  # Windows
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return round(stat.ullTotalPhys / 2**30, 1)
    except Exception:
        return None


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or "unknown"


def _ffmpeg_version() -> str | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
        return out.stdout.splitlines()[0] if out.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


def collect(root: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(root)
    info: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "cpu_model": _cpu_model(),
        "cpu_threads": os.cpu_count(),
        "ram_gb": _ram_gb(),
        "disk_free_gb": round(usage.free / 2**30, 1),
        "disk_total_gb": round(usage.total / 2**30, 1),
        "ffmpeg": _ffmpeg_version(),
    }
    for mod in ("faster_whisper", "cv2", "psutil", "fastapi"):
        try:
            __import__(mod)
            info[f"has_{mod}"] = True
        except Exception:
            info[f"has_{mod}"] = False
    warnings: list[str] = []
    if sys.version_info < (3, 11):
        warnings.append("Python 3.11 or newer is required.")
    if info["ffmpeg"] is None:
        warnings.append("ffmpeg is not on PATH; install it before Phase 3.")
    if info["ram_gb"] is not None and info["ram_gb"] < 7:
        warnings.append("Less than 8 GB RAM: keep one heavy job at a time and close other apps.")
    if info["disk_free_gb"] < 30:
        warnings.append("Under 30 GB free disk: source videos will fill it quickly.")
    info["warnings"] = warnings
    return info
