"""``BLEDevice`` and ``AdvertisementData`` — bleak-compatible value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ._mgmt import eir_name


@dataclass
class BLEDevice:
    """Mirror of ``bleak.backends.device.BLEDevice``.

    ``address_type`` retains the LE address type (1=public, 2=random) reported by
    the scan so a later L2CAP connect uses the correct type instead of guessing.
    """

    address: str
    name: Optional[str] = None
    details: Any = None
    rssi: int = 0  # deprecated in bleak but still read by batmon-ha
    address_type: Optional[int] = field(default=None, repr=False, compare=False)

    def __hash__(self):
        return hash(self.address)

    def __str__(self):
        return f"{self.address}: {self.name}"


class AdvertisementData:
    """Subset of ``bleak``'s AdvertisementData built from EIR/AD fields."""

    __slots__ = (
        "local_name",
        "rssi",
        "service_uuids",
        "manufacturer_data",
        "service_data",
        "tx_power",
        "platform_data",
    )

    # AD types
    _UUID16_INCOMPLETE = 0x02
    _UUID16_COMPLETE = 0x03
    _UUID128_INCOMPLETE = 0x06
    _UUID128_COMPLETE = 0x07
    _TX_POWER = 0x0A
    _MANUFACTURER = 0xFF
    _SERVICE_DATA_16 = 0x16

    def __init__(self, eir: Dict[int, bytes], rssi: int):
        from .uuids import normalize_uuid_16, uuid_from_bytes

        self.rssi = rssi
        self.platform_data = (eir,)
        self.local_name = eir_name(eir)

        uuids: List[str] = []
        for ad_type in (self._UUID16_INCOMPLETE, self._UUID16_COMPLETE):
            blob = eir.get(ad_type)
            if blob:
                uuids += [normalize_uuid_16(int.from_bytes(blob[i : i + 2], "little")) for i in range(0, len(blob), 2)]
        for ad_type in (self._UUID128_INCOMPLETE, self._UUID128_COMPLETE):
            blob = eir.get(ad_type)
            if blob:
                uuids += [uuid_from_bytes(blob[i : i + 16]) for i in range(0, len(blob), 16)]
        self.service_uuids = uuids

        self.manufacturer_data = {}
        md = eir.get(self._MANUFACTURER)
        if md and len(md) >= 2:
            self.manufacturer_data[int.from_bytes(md[:2], "little")] = bytes(md[2:])

        self.service_data = {}
        sd = eir.get(self._SERVICE_DATA_16)
        if sd and len(sd) >= 2:
            self.service_data[normalize_uuid_16(int.from_bytes(sd[:2], "little"))] = bytes(sd[2:])

        tx = eir.get(self._TX_POWER)
        self.tx_power = int.from_bytes(tx, "little", signed=True) if tx else None
