"""Adapter lookup: map an ``hciN`` name *or a controller MAC* to a controller
index and BD_ADDR.

The controller index drives the mgmt-socket scan; the BD_ADDR binds the L2CAP
source socket to a specific controller (so ``adapter='hci1'`` actually uses
hci1). We read the address from sysfs, falling back to the ``HCIGETDEVINFO``
ioctl because ``/sys/class/bluetooth/hciN/address`` is absent on some kernels
(e.g. the Raspberry Pi). The same ioctl lets us resolve a controller MAC to its
current index, which is robust to USB re-enumeration changing the index.
"""

from __future__ import annotations

import fcntl
import os
import re
import socket
import struct
import sys
from typing import List, Optional

_HCI_RE = re.compile(r"hci(\d+)$")
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_BT_SYSFS = "/sys/class/bluetooth"
_HCIGETDEVINFO = 0x800448D3  # _IOR('H', 211, int)


def _ensure_bluetooth_socket_constants() -> None:
    """Inject Linux Bluetooth socket constants if the runtime lacks them.

    python-build-standalone CPython (uv/pyenv) is built without
    ``socket.AF_BLUETOOTH`` even though the kernel supports it. These are fixed
    Linux ABI values, so we add them when missing.
    """
    if not sys.platform.startswith("linux"):
        return
    if not hasattr(socket, "AF_BLUETOOTH"):
        socket.AF_BLUETOOTH = 31  # type: ignore[attr-defined]
    if not hasattr(socket, "BTPROTO_HCI"):
        socket.BTPROTO_HCI = 1  # type: ignore[attr-defined]


def _hci_addr_for_index(dev_id: int) -> Optional[str]:
    """Return the uppercase MAC of controller ``dev_id`` via the HCIGETDEVINFO
    ioctl, or ``None`` if it doesn't exist / can't be queried.

    Used instead of (well, in addition to) sysfs because
    ``/sys/class/bluetooth/hciN/address`` isn't present on all kernels. Needs
    CAP_NET_RAW to open the HCI socket.
    """
    _ensure_bluetooth_socket_constants()
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    except OSError:
        return None
    try:
        buf = bytearray(96)  # struct hci_dev_info (~90 bytes); bdaddr@offset 10
        struct.pack_into("H", buf, 0, dev_id)
        try:
            fcntl.ioctl(sock.fileno(), _HCIGETDEVINFO, buf)
        except OSError:
            return None
        return ":".join("%02X" % b for b in reversed(buf[10:16]))
    finally:
        sock.close()


def _candidate_hci_indices() -> List[int]:
    """Controller indices to probe: those listed in sysfs (handles re-enumerated
    high indices) plus a small fallback range. Connection child nodes
    (``hci0:16``) are skipped."""
    indices = set(range(8))
    try:
        for name in os.listdir(_BT_SYSFS):
            m = _HCI_RE.fullmatch(name)
            if m:
                indices.add(int(m.group(1)))
    except OSError:
        pass
    return sorted(indices)


def _index_for_mac(mac: str) -> Optional[int]:
    """Resolve a controller MAC to its current hci index via the ioctl, or
    ``None`` if no controller has that address."""
    target = mac.upper()
    for dev_id in _candidate_hci_indices():
        if _hci_addr_for_index(dev_id) == target:
            return dev_id
    return None


def adapter_index(adapter: Optional[str]) -> int:
    """Map an adapter spec to a controller index.

    * ``None`` / ``'default'``      -> 0
    * ``'hci0'`` / ``'hciN'``       -> N
    * a controller MAC (``2C:CF:67:5F:4A:6D``) -> its current hci index,
      re-resolved each call so config survives USB re-enumeration
    """
    if not adapter or adapter == "default":
        return 0
    m = _HCI_RE.fullmatch(adapter)
    if m:
        return int(m.group(1))
    if _MAC_RE.match(adapter):
        index = _index_for_mac(adapter)
        if index is None:
            raise ValueError(
                f"no Bluetooth controller with address {adapter} found"
            )
        return index
    raise ValueError(f"unsupported adapter name {adapter!r} (expected 'hciN' or a MAC)")


def adapter_address(index: int) -> Optional[str]:
    """Return the controller's public BD_ADDR (uppercase), or ``None`` if
    unknown. Tries sysfs first, then the HCIGETDEVINFO ioctl (sysfs is absent on
    some kernels, e.g. the Raspberry Pi)."""
    try:
        with open(f"{_BT_SYSFS}/hci{index}/address") as f:
            addr = f.read().strip().upper()
        if addr:
            return addr
    except OSError:
        pass
    return _hci_addr_for_index(index)
