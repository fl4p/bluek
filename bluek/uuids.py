"""UUID helpers, bleak-compatible.

bleak represents every characteristic/service UUID as the canonical lowercase
128-bit string (e.g. ``0000180a-0000-1000-8000-00805f9b34fb``). On the wire ATT
gives us 16-, 32- or 128-bit UUIDs, so we normalise to that canonical form.
"""

from __future__ import annotations

import uuid as _uuid

# The Bluetooth Base UUID: 16/32-bit UUIDs are this with the high bits replaced.
_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"
BASE_UUID = _uuid.UUID(f"00000000{_BASE_SUFFIX}")


def normalize_uuid_str(uuid: str) -> str:
    """Return the canonical lowercase 128-bit string for a 16/32/128-bit UUID.

    Mirrors ``bleak.uuids.normalize_uuid_str``.
    """
    s = uuid.strip().lower().replace("0x", "")
    if len(s) == 4:  # 16-bit
        s = f"0000{s}{_BASE_SUFFIX}"
    elif len(s) == 8:  # 32-bit
        s = f"{s}{_BASE_SUFFIX}"
    return str(_uuid.UUID(s))


def normalize_uuid_16(value: int) -> str:
    return normalize_uuid_str(f"{value:04x}")


def uuid_from_bytes(data: bytes) -> str:
    """Build a canonical UUID string from a little-endian on-the-wire UUID.

    ATT carries 2-, 4- or 16-byte UUIDs, least-significant byte first.
    """
    if len(data) == 2:
        return normalize_uuid_16(int.from_bytes(data, "little"))
    if len(data) == 4:
        return normalize_uuid_str(f"{int.from_bytes(data, 'little'):08x}")
    if len(data) == 16:
        return str(_uuid.UUID(bytes=bytes(reversed(data))))
    raise ValueError(f"invalid UUID length {len(data)}")


def uuid_to_bytes(uuid: str) -> bytes:
    """Encode a UUID string as on-the-wire bytes (2 if it maps to a 16-bit, else 16).

    Least-significant byte first, matching the ATT representation.
    """
    u = _uuid.UUID(normalize_uuid_str(uuid))
    # If only the 16-bit slot differs from the base UUID, send the short form.
    if (u.int & ~(0xFFFF << 96)) == BASE_UUID.int:
        short = (u.int >> 96) & 0xFFFF
        return short.to_bytes(2, "little")
    return bytes(reversed(u.bytes))
