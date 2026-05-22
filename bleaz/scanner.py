"""bleak-compatible ``BleakScanner`` over the mgmt (management) socket."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import _hci
from ._mgmt import DeviceFound, MgmtSocket
from .device import AdvertisementData, BLEDevice


class BleakScanner:
    def __init__(self, detection_callback=None, service_uuids=None, adapter=None, **kwargs):
        self._adapter = adapter
        self._index = _hci.adapter_index(adapter)
        self._detection_callback = detection_callback
        self._service_uuids = service_uuids
        self._mgmt: Optional[MgmtSocket] = None
        self._running = False
        # address -> (BLEDevice, AdvertisementData)
        self._found: Dict[str, Tuple[BLEDevice, AdvertisementData]] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._mgmt = MgmtSocket.open()
        self._mgmt.add_device_found_handler(self._on_device_found)
        await self._mgmt.start_discovery(self._index)
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            await self._mgmt.stop_discovery(self._index)
        finally:
            self._mgmt.close()
            self._mgmt = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    def _on_device_found(self, index: int, df: DeviceFound) -> None:
        if index != self._index:
            return  # event for a different controller
        if df.address_type not in (1, 2):
            return  # not an LE device
        device = BLEDevice(
            address=df.address,
            name=self._merge_name(df),
            rssi=df.rssi,
            address_type=df.address_type,
        )
        adv = AdvertisementData(df.eir, rssi=df.rssi)
        self._found[df.address] = (device, adv)
        if self._detection_callback is not None:
            self._detection_callback(device, adv)

    def _merge_name(self, df: DeviceFound) -> Optional[str]:
        from ._mgmt import eir_name

        name = eir_name(df.eir)
        if name:
            return name
        # Keep a previously-learned name (adv and scan-response arrive separately).
        prev = self._found.get(df.address)
        return prev[0].name if prev else None

    @property
    def discovered_devices(self) -> List[BLEDevice]:
        return [device for device, _ in self._found.values()]

    @property
    def discovered_devices_and_advertisement_data(self) -> Dict[str, Tuple[BLEDevice, AdvertisementData]]:
        return dict(self._found)
