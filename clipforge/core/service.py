"""Start ClipForge at logon and stop the laptop sleeping (Windows).

A true boot service needs administrator rights and a stored password, which
this project does not ask for. A scheduled task that runs at logon, with Windows
auto-login switched on, gives the same result after a reboot.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK = "ClipForge"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>ClipForge supervisor: dashboard, bot and worker</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{command}</Command><Arguments>-m clipforge supervise</Arguments>
    <WorkingDirectory>{workdir}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""


def current_user() -> str:
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")


def windowless_python() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.is_file() else exe)


def task_xml(command: str, workdir: str, user: str) -> str:
    return TASK_XML.format(command=escape(command), workdir=escape(workdir), user=escape(user))


def power_commands() -> list[list[str]]:
    """Never sleep, hibernate or react to the lid or the power and sleep buttons on mains power."""
    return [
        ["powercfg", "/change", "standby-timeout-ac", "0"],
        ["powercfg", "/change", "hibernate-timeout-ac", "0"],
        ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"],
        ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "PBUTTONACTION", "0"],
        ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "SBUTTONACTION", "0"],
        ["powercfg", "/setactive", "SCHEME_CURRENT"],
    ]


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def install(workdir: Path) -> tuple[bool, str]:
    xml = task_xml(windowless_python(), str(workdir), current_user())
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as fh:
        fh.write(xml)
        path = fh.name
    try:
        code, out = _run(["schtasks", "/create", "/tn", TASK, "/xml", path, "/f"])
    finally:
        os.unlink(path)
    return code == 0, out


def uninstall() -> tuple[bool, str]:
    code, out = _run(["schtasks", "/delete", "/tn", TASK, "/f"])
    return code == 0, out


def status() -> str:
    code, out = _run(["schtasks", "/query", "/tn", TASK, "/fo", "LIST"])
    return out if code == 0 else "not installed"


def power_setup() -> list[tuple[list[str], int]]:
    return [(c, _run(c)[0]) for c in power_commands()]
