"""Bluetoothd coordination helpers.

Two needs in the bench:

1. Before a Layer-1/2 bluek GATT scenario, make sure bluetoothd isn't
   holding an open connection to the test peripheral (the kernel allows only
   one L2CAP ATT channel per peer-pair).
2. Optionally pause bluetoothd on the central adapter via btmgmt, so the
   "raw" path runs in true isolation. By default we don't — keeping a
   realistic environment makes the lib_bleak numbers honest.

All helpers are best-effort; failures are non-fatal because some tooling
(``btmgmt``, ``bluetoothctl``) may not be installed everywhere.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def disconnect_peripheral(address: str) -> bool:
    """Best-effort: ``bluetoothctl disconnect <addr>``. Returns True on apparent
    success."""
    if not _have("bluetoothctl"):
        return False
    try:
        out = subprocess.run(
            ["bluetoothctl", "disconnect", address],
            capture_output=True, text=True, timeout=5.0,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def hci_index_int(adapter: str) -> Optional[int]:
    if adapter.startswith("hci") and adapter[3:].isdigit():
        return int(adapter[3:])
    return None


def pause_bluetoothd_on(adapter: str) -> bool:
    """btmgmt --index N power off."""
    idx = hci_index_int(adapter)
    if idx is None or not _have("btmgmt"):
        return False
    try:
        out = subprocess.run(
            ["btmgmt", "--index", str(idx), "power", "off"],
            capture_output=True, text=True, timeout=5.0,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resume_bluetoothd_on(adapter: str) -> bool:
    idx = hci_index_int(adapter)
    if idx is None or not _have("btmgmt"):
        return False
    try:
        out = subprocess.run(
            ["btmgmt", "--index", str(idx), "power", "on"],
            capture_output=True, text=True, timeout=5.0,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def adapter_down(adapter: str) -> bool:
    """``hciconfig <adapter> down`` — used to release the adapter for bumble."""
    if not _have("hciconfig"):
        return False
    try:
        out = subprocess.run(
            ["hciconfig", adapter, "down"],
            capture_output=True, text=True, timeout=5.0,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def adapter_up(adapter: str) -> bool:
    if not _have("hciconfig"):
        return False
    try:
        out = subprocess.run(
            ["hciconfig", adapter, "up"],
            capture_output=True, text=True, timeout=5.0,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
