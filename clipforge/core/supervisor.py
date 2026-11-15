"""Keep `serve` and `worker` running (PRD section 9, Always on).

One supervisor process starts both as children, restarts any that exit (with a
growing pause so a crash loop does not spin), kills and restarts a worker whose
heartbeat has been silent for `stall_minutes`, and tells Yaish on Telegram.
The supervisor itself is started at logon by a Windows scheduled task.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings

log = logging.getLogger("clipforge.supervisor")

LOCK_PORT = 47831               # bound for the lifetime of the supervisor: one at a time
BACKOFF = (5.0, 15.0, 60.0, 300.0)
STABLE_AFTER = 600.0            # a child that ran this long starts its backoff over
ALERT_EVERY = 1800.0            # at most one alert per child per half hour
LOG_LIMIT = 5 * 1024 * 1024


def stalled(heartbeat_age: float | None, started_age: float, limit_seconds: float) -> bool:
    """True when the worker has shown no sign of life for `limit_seconds`.

    A heartbeat older than the process itself belongs to a previous run, so the
    silence is measured from the start in that case.
    """
    silent = started_age if heartbeat_age is None else min(heartbeat_age, started_age)
    return silent > limit_seconds


def acquire_lock() -> socket.socket | None:
    """Hold a loopback port as a mutex. The OS frees it if we die."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


def trim_log(path: Path) -> None:
    """Keep the newest half of a log that has grown past the limit."""
    try:
        if path.stat().st_size > LOG_LIMIT:
            data = path.read_bytes()
            path.write_bytes(data[len(data) // 2:])
    except OSError:
        pass


def spawn_process(argv: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    trim_log(log_path)
    out = open(log_path, "ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags)
    finally:
        out.close()


@dataclass
class Child:
    name: str
    argv: list[str]
    log_path: Path
    spawn: Callable[[list[str], Path], "subprocess.Popen"] = spawn_process
    proc: "subprocess.Popen | None" = None
    started_at: float = 0.0
    failures: int = 0
    next_start: float = 0.0
    last_alert: float = field(default=-1e18)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, now: float) -> None:
        self.proc = self.spawn(self.argv, self.log_path)
        self.started_at = now

    def _end_tree(self) -> None:
        # the venv python.exe is a launcher with the real interpreter as its child
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=30)
                return
            except Exception:
                pass
        self.proc.terminate()

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self._end_tree()
            try:
                self.proc.wait(timeout=20)
            except Exception:
                self.proc.kill()
        self.proc = None


class Supervisor:
    def __init__(self, settings: Settings, children: list[Child], *,
                 notify: Callable[[str], None] = lambda text: None,
                 heartbeat_age: Callable[[], float | None] = lambda: None,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.children = children
        self.notify = notify
        self.heartbeat_age = heartbeat_age
        self.clock = clock

    def _alert(self, child: Child, text: str, now: float) -> None:
        if now - child.last_alert < ALERT_EVERY:
            return
        child.last_alert = now
        try:
            self.notify(text)
        except Exception as exc:                    # an alert must never stop the supervisor
            log.warning("alert failed: %s", type(exc).__name__)

    def step(self) -> None:
        now = self.clock()
        for c in self.children:
            if c.proc is None:
                if now >= c.next_start:
                    c.start(now)
                    log.info("started %s", c.name)
                continue
            if c.alive():
                if c.name == "worker" and stalled(self.heartbeat_age(), now - c.started_at,
                                                  self.settings.stall_minutes * 60):
                    hb = self.heartbeat_age()
                    log.warning("worker stalled (heartbeat %s old, process %.0fs old); restarting",
                                "missing" if hb is None else f"{hb:.0f}s", now - c.started_at)
                    c.stop()
                    c.failures = 0
                    c.next_start = now
                    self._alert(c, f"The worker was silent for {self.settings.stall_minutes} "
                                   "minutes and was restarted.", now)
                continue
            code = c.proc.returncode
            ran = now - c.started_at
            c.proc = None
            c.failures = 0 if ran >= STABLE_AFTER else c.failures + 1
            pause = BACKOFF[min(c.failures, len(BACKOFF)) - 1] if c.failures else BACKOFF[0]
            c.next_start = now + pause
            log.warning("%s exited with code %s after %.0fs; restarting in %.0fs",
                        c.name, code, ran, pause)
            self._alert(c, f"{c.name} stopped (exit code {code}) and is being restarted.", now)

    def run(self, interval: float = 15.0) -> None:
        try:
            while True:
                self.step()
                time.sleep(interval)
        finally:
            for c in self.children:
                c.stop()


def default_children(settings: Settings) -> list[Child]:
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("pythonw.exe"):
        py = py[:-len("pythonw.exe")] + "python.exe"       # children log to files, no console shown
    logs = settings.data_dir / "logs"
    return [Child("serve", [py, "-m", "clipforge", "serve"], logs / "serve.log"),
            Child("worker", [py, "-m", "clipforge", "worker"], logs / "worker.log")]
