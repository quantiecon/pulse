from __future__ import annotations

import platform
import subprocess
from typing import List


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(subjects: List[str]) -> None:
    if not subjects or platform.system() != "Darwin":
        return
    body = subjects[0]
    if len(subjects) > 1:
        body = "%s and %d more" % (subjects[0], len(subjects) - 1)
    script = "display notification %s with title %s" % (_quote(body[:180]), _quote("Pulse"))
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
