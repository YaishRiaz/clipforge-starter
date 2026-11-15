from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.core import service, supervisor as sv
from clipforge.core.config import get_settings


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, names=("serve", "worker"), hb=None):
    spawned = []

    def spawn(argv, log_path):
        p = FakeProc()
        spawned.append((argv, p))
        return p

    children = [sv.Child(n, ["x", n], tmp_path / f"{n}.log", spawn=spawn) for n in names]
    alerts = []
    clock = Clock()
    beat = {"age": hb}
    s = sv.Supervisor(get_settings(), children, notify=alerts.append,
                      heartbeat_age=lambda: beat["age"], clock=clock)
    return s, children, spawned, alerts, clock, beat


def test_stalled():
    assert not sv.stalled(10, 5000, 1200)
    assert sv.stalled(1300, 5000, 1200)
    assert sv.stalled(None, 1300, 1200)
    assert not sv.stalled(None, 100, 1200)
    # a heartbeat older than the process belongs to a previous run
    assert not sv.stalled(99999, 100, 1200)


def test_step_starts_children(tmp_path):
    s, children, spawned, *_ = make(tmp_path)
    s.step()
    assert len(spawned) == 2
    assert all(c.alive() for c in children)
    s.step()
    assert len(spawned) == 2


def test_restart_with_backoff_and_alert(tmp_path):
    s, children, spawned, alerts, clock, _ = make(tmp_path, names=("serve",))
    s.step()
    spawned[0][1].returncode = 1
    clock.t += 30
    s.step()
    assert not children[0].alive()
    assert children[0].failures == 1
    assert len(alerts) == 1 and "serve" in alerts[0]
    clock.t += 1
    s.step()
    assert len(spawned) == 1                       # still waiting out the pause
    clock.t += sv.BACKOFF[0]
    s.step()
    assert len(spawned) == 2


def test_backoff_grows_and_caps_then_resets(tmp_path):
    s, children, spawned, alerts, clock, _ = make(tmp_path, names=("serve",))
    c = children[0]
    s.step()
    for i in range(6):
        spawned[-1][1].returncode = 1
        clock.t += 1
        s.step()
        assert c.next_start - clock.t == pytest.approx(sv.BACKOFF[min(i, len(sv.BACKOFF) - 1)])
        clock.t = c.next_start
        s.step()
    spawned[-1][1].returncode = 1
    clock.t += sv.STABLE_AFTER + 1
    s.step()
    assert c.failures == 0
    assert c.next_start - clock.t == pytest.approx(sv.BACKOFF[0])


def test_stalled_worker_restarted_and_alert_throttled(tmp_path):
    s, children, spawned, alerts, clock, beat = make(tmp_path, names=("worker",))
    s.step()
    first = spawned[0][1]
    limit = s.settings.stall_minutes * 60
    beat["age"] = 5
    clock.t += limit + 100
    s.step()
    assert first.terminated is False               # heartbeat is fresh
    beat["age"] = limit + 50
    s.step()
    assert first.terminated and not children[0].alive()
    assert len(alerts) == 1 and "restarted" in alerts[0]
    s.step()
    assert len(spawned) == 2                       # restarted at once
    beat["age"] = limit + 50
    clock.t += limit + 100
    s.step()
    assert len(alerts) == 1                        # inside the alert window


def test_serve_is_not_stall_checked(tmp_path):
    s, children, spawned, alerts, clock, beat = make(tmp_path, names=("serve",))
    s.step()
    beat["age"] = 10 ** 6
    clock.t += 10 ** 6
    s.step()
    assert children[0].alive()


def test_notify_failure_swallowed(tmp_path):
    s, children, spawned, alerts, clock, _ = make(tmp_path, names=("serve",))

    def boom(text):
        raise RuntimeError("telegram down")

    s.notify = boom
    s.step()
    spawned[0][1].returncode = 3
    s.step()
    assert not children[0].alive()


def test_stop_terminates(tmp_path):
    s, children, spawned, *_ = make(tmp_path, names=("serve",))
    s.step()
    children[0].stop()
    assert spawned[0][1].terminated and children[0].proc is None


def test_acquire_lock_is_exclusive(monkeypatch):
    monkeypatch.setattr(sv, "LOCK_PORT", 47999)
    first = sv.acquire_lock()
    assert first is not None
    try:
        assert sv.acquire_lock() is None
    finally:
        first.close()
    again = sv.acquire_lock()
    assert again is not None
    again.close()


def test_trim_log(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "LOG_LIMIT", 100)
    p = tmp_path / "a.log"
    p.write_bytes(b"x" * 150 + b"END")
    sv.trim_log(p)
    data = p.read_bytes()
    assert len(data) < 100 and data.endswith(b"END")
    sv.trim_log(tmp_path / "missing.log")


def test_default_children(tmp_path):
    kids = sv.default_children(get_settings())
    assert [c.name for c in kids] == ["serve", "worker"]
    assert kids[0].argv[-1] == "serve" and kids[1].argv[-1] == "worker"
    assert all(c.log_path.parent.name == "logs" for c in kids)


def test_task_xml_contents_and_escaping():
    xml = service.task_xml(r"C:\a & b\pythonw.exe", r"D:\clip<forge>", r"PC\Yaish")
    assert "a &amp; b" in xml and "clip&lt;forge&gt;" in xml
    assert "<LogonTrigger>" in xml and "LeastPrivilege" in xml
    assert "-m clipforge supervise" in xml
    assert "<DisallowStartIfOnBatteries>false" in xml
    import xml.dom.minidom as md
    md.parseString(xml.split("?>", 1)[1])


def test_power_commands():
    cmds = service.power_commands()
    assert ["powercfg", "/change", "standby-timeout-ac", "0"] in cmds
    assert ["powercfg", "/change", "hibernate-timeout-ac", "0"] in cmds
    assert any("LIDACTION" in c for c in cmds)
    assert cmds[-1][:2] == ["powercfg", "/setactive"]
    assert all(not any(w in ("-dc", "standby-timeout-dc") for w in c) for c in cmds)


def test_install_calls_schtasks(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd):
        seen["cmd"] = cmd
        seen["xml"] = Path(cmd[cmd.index("/xml") + 1]).read_text(encoding="utf-16")
        return 0, "SUCCESS"

    monkeypatch.setattr(service, "_run", fake_run)
    ok, out = service.install(tmp_path)
    assert ok and seen["cmd"][:3] == ["schtasks", "/create", "/tn"]
    assert str(tmp_path) in seen["xml"]
    assert not Path(seen["cmd"][seen["cmd"].index("/xml") + 1]).exists()
