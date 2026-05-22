"""Small shared helpers (address conversion)."""

from __future__ import annotations


def str_to_bdaddr(addr: str) -> bytes:
    """``"AA:BB:CC:DD:EE:FF"`` -> 6 bytes in BlueZ wire order (least-significant first)."""
    parts = addr.strip().split(":")
    if len(parts) != 6:
        raise ValueError(f"invalid BD_ADDR {addr!r}")
    return bytes(int(p, 16) for p in reversed(parts))


def bdaddr_to_str(data) -> str:
    """6 wire-order bytes -> canonical uppercase ``"AA:BB:CC:DD:EE:FF"``."""
    b = bytes(data)
    if len(b) != 6:
        raise ValueError(f"invalid BD_ADDR bytes {b!r}")
    return ":".join("%02X" % x for x in reversed(b))
