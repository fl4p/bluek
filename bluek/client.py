"""bleak-compatible ``BleakClient`` over a kernel L2CAP ATT connection."""

from __future__ import annotations

import asyncio
from typing import Callable, List, Optional, Union

from . import _att, _hci
from ._l2cap import BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM, L2CAPSocket
from ._mgmt import DeviceFound, MgmtSocket
from .characteristic import BleakGATTCharacteristic, BleakGATTService, BleakGATTServiceCollection
from .device import BLEDevice
from .exc import BleakCharacteristicNotFoundError, BleakDeviceNotFoundError, BleakError
from .uuids import normalize_uuid_str

CharSpec = Union[str, int, BleakGATTCharacteristic]

# A cold L2CAP connect to a weak/flaky LE peer can fail at the link layer with
# HCI status 0x3E ("connection failed to be established"). The kernel has no
# errno for 0x3E, so it surfaces via SO_ERROR as ENOSYS ("Function not
# implemented") -- it is *transient*, not a missing syscall. Retry such fast
# failures within the caller's timeout budget instead of bubbling up a hard
# "device not found" (which forces the slow scanner fallback). A genuine
# asyncio.TimeoutError (peer truly absent / out of range) is NOT retried here.
_CONNECT_RETRY_DELAY = 0.3

# The kernel's L2CAP LE connect path needs a recent advert observation for the
# peer; without it, ``connect()`` either silently hangs (bluetoothd holding the
# scan slot) or returns EHOSTUNREACH. Real users hit this implicitly by running
# BleakScanner before BleakClient, but a bare ``BleakClient(mac).connect()``
# would otherwise time out for no obvious reason. We open a short mgmt-level
# discovery before each connect to populate the kernel cache; the helper bails
# as soon as the target advert arrives, so the cost is one advertising interval
# (~100–500 ms for most peripherals) when the user *did* pre-scan, and a small
# fixed budget when they didn't. Patchable for tests.
_CONNECT_PRESCAN_BUDGET_S = 2.0


async def _ensure_kernel_knows_peer(
    index: int, target_mac: str, budget_s: float = _CONNECT_PRESCAN_BUDGET_S
) -> Optional[int]:
    """Run a brief LE discovery on ``index`` until ``target_mac`` is seen.

    Returns the observed ``address_type`` (``BDADDR_LE_PUBLIC`` or
    ``BDADDR_LE_RANDOM``), or ``None`` if no advert arrived within the budget
    — in which case the caller should proceed anyway, because the kernel may
    already have the peer cached from a prior scan that we can't observe.
    """
    if budget_s <= 0:
        return None
    target = target_mac.upper()
    seen_type: Optional[int] = None
    try:
        mgmt = MgmtSocket.open()
    except OSError:
        # No CAP_NET_ADMIN, or kernel issue. Let L2CAPSocket.connect surface
        # the real error.
        return None

    def on_found(_idx: int, df: DeviceFound) -> None:
        nonlocal seen_type
        if _idx == index and df.address.upper() == target:
            seen_type = df.address_type

    mgmt.add_device_found_handler(on_found)
    try:
        await mgmt.start_discovery(index)
        steps = max(1, int(budget_s / 0.05))
        for _ in range(steps):
            await asyncio.sleep(0.05)
            if seen_type is not None:
                return seen_type
    finally:
        try:
            await mgmt.stop_discovery(index)
        finally:
            mgmt.close()
    return None


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
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        # Prime the kernel's LE cache for this peer (short, abortable on first
        # advert). Skip when we already know the peer's address type AND have a
        # plausible scenario where the kernel already knows about it — caller
        # owns the cost trade-off via ``timeout``. The budget is capped so a
        # genuinely-absent peer still fails fast within the caller's window.
        prescan_budget = min(_CONNECT_PRESCAN_BUDGET_S, max(0.0, timeout - 1.0))
        if prescan_budget > 0:
            observed = await _ensure_kernel_knows_peer(
                self._index, self.address, budget_s=prescan_budget
            )
            if observed in (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM):
                # Lock in the observed address type so we don't waste a probe
                # on the wrong one. Overrides any stale BLEDevice hint.
                self._peer_type = observed

        last_exc: Optional[BaseException] = None
        l2: Optional[L2CAPSocket] = None
        transient = False

        while True:
            transient = False
            for peer_type in self._candidate_types():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    l2 = await L2CAPSocket.connect(
                        dst=self.address, dst_type=peer_type, src=src, timeout=remaining
                    )
                    self._peer_type = peer_type
                    break
                except asyncio.TimeoutError as e:
                    # peer didn't respond within budget: absent / out of range.
                    last_exc = e
                except OSError as e:
                    # fast link-layer failure (e.g. 0x3E -> ENOSYS): transient.
                    last_exc = e
                    transient = True
            if l2 is not None:
                break
            # Only the transient OSError path is worth retrying, and only while
            # the caller's timeout budget allows another attempt.
            if not transient or (deadline - loop.time()) <= _CONNECT_RETRY_DELAY:
                break
            await asyncio.sleep(_CONNECT_RETRY_DELAY)

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
