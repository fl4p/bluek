"""Adapter lookup: map an ``hciN`` name to a controller index and BD_ADDR.

The local adapter address is needed to bind the L2CAP source socket to a
specific controller (so ``adapter='hci1'`` actually uses hci1). We read it from
sysfs; the management socket also reports it via READ_CONTROLLER_INFO as a
fallback when sysfs is unavailable.
"""

from __future__ import annotations

import re
from typing import Optional

_HCI_RE = re.compile(r"hci(\d+)$")


def adapter_index(adapter: Optional[str]) -> int:
    """``'hci0'`` -> 0, ``None``/``'default'`` -> 0."""
    if not adapter or adapter == "default":
        return 0
    m = _HCI_RE.fullmatch(adapter)
    if not m:
        raise ValueError(f"unsupported adapter name {adapter!r} (expected 'hciN')")
    return int(m.group(1))


def adapter_address(index: int) -> Optional[str]:
    """Read the controller's public BD_ADDR from sysfs, or ``None`` if unknown."""
    try:
        with open(f"/sys/class/bluetooth/hci{index}/address") as f:
            addr = f.read().strip().upper()
        return addr or None
    except OSError:
        return None
