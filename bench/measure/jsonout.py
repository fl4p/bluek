"""Run-result JSON writer.

A single JSON per invocation captures every scenario × stack result plus
the host environment so cross-machine diffs are possible.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def _safe_run(cmd: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _pkg_version(name: str) -> Optional[str]:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def capture_env(extra: Optional[dict] = None) -> dict:
    env = {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "kernel": _safe_run(["uname", "-r"]),
        "python": sys.version.split()[0],
        "bluez_version": _safe_run(["bluetoothctl", "--version"]),
        "pkg_versions": {
            name: _pkg_version(name)
            for name in ("bluek", "bleak", "dbus-fast", "bumble", "psutil")
        },
    }
    if extra:
        env.update(extra)
    return env


def make_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + "_" + socket.gethostname()


def write_run(results_dir: str | os.PathLike, run_id: str, payload: dict) -> Path:
    out_dir = Path(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.json"
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return path
