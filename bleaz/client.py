"""bleak-compatible ``BleakClient`` over a kernel L2CAP ATT connection."""

from __future__ import annotations

import asyncio
from typing import Callable, List, Optional, Union

from . import _att, _hci
from ._l2cap import BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM, L2CAPSocket
from .characteristic import BleakGATTCharacteristic, BleakGATTService, BleakGATTServiceCollection
from .device import BLEDevice
from .exc import BleakCharacteristicNotFoundError, BleakDeviceNotFoundError, BleakError
from .uuids import normalize_uuid_str

CharSpec = Union[str, int, BleakGATTCharacteristic]


class BleakClient:
    def __init__(
        self,
        address_or_device: Union[str, BLEDevice],
        disconnected_callback: Optional[Callable[["BleakClient"], None]] = None,
        adapter: Optional[str] = None,
        handle_pairing: bool = False,
        **kwargs,
    ):
        if isinstance(address_or_device, BLEDevice):
            self.address = address_or_device.address
            self._peer_type = address_or_device.address_type
        else:
            self.address = str(address_or_device)
            self._peer_type = None

        self._adapter = adapter
        self._index = _hci.adapter_index(adapter)
        self._disconnected_callback = disconnected_callback
        self._handle_pairing = handle_pairing

        self._l2: Optional[L2CAPSocket] = None
        self._att: Optional[_att.ATTClient] = None
        self._services = BleakGATTServiceCollection([])
        self._connected = False
        # value_handle -> CCCD handle, for stop_notify
        self._cccd_handles = {}

    # -- connection lifecycle ---------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    def _candidate_types(self) -> List[int]:
        if self._peer_type in (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM):
            return [self._peer_type]
        # Unknown (connecting by bare address): try public, then random.
        return [BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM]

    async def connect(self, timeout: float = 10.0, **kwargs) -> bool:
        src = _hci.adapter_address(self._index)
        last_exc: Optional[BaseException] = None
        l2: Optional[L2CAPSocket] = None
        for peer_type in self._candidate_types():
            try:
                l2 = await L2CAPSocket.connect(
                    dst=self.address, dst_type=peer_type, src=src, timeout=timeout
                )
                self._peer_type = peer_type
                break
            except asyncio.TimeoutError as e:
                last_exc = e
            except OSError as e:
                last_exc = e
        if l2 is None:
            raise BleakDeviceNotFoundError(
                self.address, f"could not connect to {self.address}: {last_exc}"
            )

        self._l2 = l2
        self._att = _att.ATTClient(l2, on_disconnect=self._on_disconnect)
        self._connected = True
        try:
            await self._att.exchange_mtu()
            await self._discover_services()
        except BaseException:
            await self.disconnect()
            raise
        return True

    async def disconnect(self) -> bool:
        if self._att is not None:
            self._att.close()
        await self._teardown()
        return True

    def _on_disconnect(self, _exc) -> None:
        was_connected = self._connected
        self._connected = False
        if was_connected and self._disconnected_callback is not None:
            self._disconnected_callback(self)

    async def _teardown(self) -> None:
        self._connected = False
        self._att = None
        self._l2 = None
        self._cccd_handles.clear()
        self._services = BleakGATTServiceCollection([])

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    # -- GATT services -----------------------------------------------------
    async def _discover_services(self) -> None:
        gatt_services = await self._att.discover()
        self._services = BleakGATTServiceCollection([BleakGATTService(s) for s in gatt_services])

    @property
    def services(self) -> BleakGATTServiceCollection:
        return self._services

    async def get_services(self) -> BleakGATTServiceCollection:
        if not self._services and self._att is not None:
            await self._discover_services()
        return self._services

    # -- characteristic access --------------------------------------------
    def _resolve_char(self, spec: CharSpec) -> BleakGATTCharacteristic:
        if isinstance(spec, BleakGATTCharacteristic):
            return spec
        if isinstance(spec, int):
            char = self._services.get_characteristic(spec)
        else:
            char = self._services.get_characteristic(normalize_uuid_str(str(spec)))
        if char is None:
            raise BleakCharacteristicNotFoundError(spec)
        return char

    def _require_att(self) -> "_att.ATTClient":
        if self._att is None or not self._connected:
            raise BleakError("not connected")
        return self._att

    async def read_gatt_char(self, char_specifier: CharSpec) -> bytearray:
        att = self._require_att()
        char = self._resolve_char(char_specifier)
        return bytearray(await att.read(char.value_handle))

    async def write_gatt_char(self, char_specifier: CharSpec, data, response: bool = False) -> None:
        att = self._require_att()
        char = self._resolve_char(char_specifier)
        payload = bytes(data)
        if response:
            await att.write(char.value_handle, payload)
        else:
            await att.write_command(char.value_handle, payload)

    async def read_gatt_descriptor(self, handle: int) -> bytearray:
        att = self._require_att()
        return bytearray(await att.read(handle))

    async def write_gatt_descriptor(self, handle: int, data) -> None:
        att = self._require_att()
        await att.write(handle, bytes(data))

    # -- notifications -----------------------------------------------------
    async def start_notify(self, char_specifier: CharSpec, callback: Callable, **kwargs) -> None:
        att = self._require_att()
        char = self._resolve_char(char_specifier)

        def handler(data: bytearray, _char=char):
            callback(_char, data)

        att.set_notify_handler(char.value_handle, handler)

        cccd = char.get_descriptor(_att.CCCD_UUID)
        if cccd is None:
            raise BleakError(f"characteristic {char.uuid} has no CCCD; cannot subscribe")
        # 0x0001 = notifications, 0x0002 = indications
        value = b"\x02\x00" if "indicate" in char.properties and "notify" not in char.properties else b"\x01\x00"
        await att.write(cccd.handle, value)
        self._cccd_handles[char.value_handle] = cccd.handle

    async def stop_notify(self, char_specifier: CharSpec) -> None:
        att = self._require_att()
        char = self._resolve_char(char_specifier)
        cccd_handle = self._cccd_handles.pop(char.value_handle, None)
        if cccd_handle is not None:
            try:
                await att.write(cccd_handle, b"\x00\x00")
            except BleakError:
                pass
        att.remove_notify_handler(char.value_handle)

    # -- pairing -----------------------------------------------------------
    async def pair(self, callback: Optional[Callable] = None, **kwargs) -> bool:
        """Pair/bond via ``bluetoothctl`` (the kernel keeps the keys)."""
        from .pairing import pair_with_bluetoothctl

        return await pair_with_bluetoothctl(self.address, self._adapter, callback)

    async def unpair(self) -> bool:
        from .pairing import remove_with_bluetoothctl

        return await remove_with_bluetoothctl(self.address, self._adapter)
