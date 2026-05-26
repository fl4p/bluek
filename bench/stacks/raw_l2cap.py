"""Layer 1 stack: raw L2CAP + mgmt sockets via bluek's internal modules.

Skips the public BleakScanner/BleakClient wrappers so we measure transport
cost, not the (very thin) public-API layer.

The wrappers in ``bluek.client.BleakClient`` add ~70 lines of glue
(characteristic resolution, retry, lifecycle); on the hot read/write path
the overhead is negligible compared to L2CAP + ATT round-trip, so this stack
should land within a few percent of ``lib_bluek``. The contrast you actually
want lies between this stack and ``raw_dbus``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

from bluek import _att, _hci
from bluek._l2cap import BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM, L2CAPSocket
from bluek._mgmt import DeviceFound, MgmtSocket, eir_name
from bluek.uuids import normalize_uuid_str

from bench.measure.clock import perf_ns
from .base import AdvertEvent, NotifyEvent, Stack


NAME = "raw_l2cap"

_CONNECT_RETRY_DELAY = 0.3


@dataclass
class _ResolvedChar:
    value_handle: int
    cccd_handle: Optional[int]


@dataclass
class _GattHandle:
    l2: L2CAPSocket
    att: _att.ATTClient
    chars_by_uuid: Dict[str, _ResolvedChar]


class RawL2capStack:
    NAME = NAME

    def __init__(self, adapter: str = "hci0"):
        self._adapter = adapter
        self._index = _hci.adapter_index(adapter)

    # -- scan --------------------------------------------------------------
    async def scan_iter(self, duration_s: float) -> AsyncIterator[AdvertEvent]:
        queue: asyncio.Queue[AdvertEvent] = asyncio.Queue()

        def on_found(index: int, df: DeviceFound) -> None:
            if index != self._index:
                return
            if df.address_type not in (1, 2):
                return
            queue.put_nowait(
                AdvertEvent(
                    t_ns=perf_ns(),
                    address=df.address,
                    rssi=df.rssi,
                    name=eir_name(df.eir),
                )
            )

        mgmt = MgmtSocket.open()
        mgmt.add_device_found_handler(on_found)
        await mgmt.start_discovery(self._index)
        try:
            deadline = perf_ns() + int(duration_s * 1_000_000_000)
            while perf_ns() < deadline:
                try:
                    timeout = max(0.001, (deadline - perf_ns()) / 1_000_000_000)
                    yield await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
        finally:
            try:
                await mgmt.stop_discovery(self._index)
            finally:
                mgmt.close()

    # -- connect + discover ------------------------------------------------
    async def setup_gatt(self, target_mac: str, address_type: int = 1) -> Any:
        src = _hci.adapter_address(self._index)
        loop = asyncio.get_event_loop()
        timeout = 10.0

        # Linux kernel L2CAP LE connect needs a recent advert observation to
        # know the peer's address type and route. Without a prior scan,
        # connect() either hangs (with bluetoothd active) or returns
        # EHOSTUNREACH. Real bluek users hit this implicitly via
        # BleakScanner-before-BleakClient. Do an explicit short scan here so
        # the bench works regardless of caller order.
        seen_type: Optional[int] = None
        m = MgmtSocket.open()

        def _on_seen(index: int, df: DeviceFound) -> None:
            nonlocal seen_type
            if index == self._index and df.address.upper() == target_mac.upper():
                seen_type = df.address_type

        m.add_device_found_handler(_on_seen)
        try:
            await m.start_discovery(self._index)
            for _ in range(25):  # up to 5 s
                await asyncio.sleep(0.2)
                if seen_type is not None:
                    break
        finally:
            try:
                await m.stop_discovery(self._index)
            finally:
                m.close()

        deadline = loop.time() + timeout

        candidate_types: List[int]
        if seen_type in (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM):
            candidate_types = [seen_type]
        elif address_type in (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM):
            candidate_types = [address_type]
        else:
            candidate_types = [BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM]

        l2: Optional[L2CAPSocket] = None
        last_exc: Optional[BaseException] = None
        while True:
            transient = False
            for peer_type in candidate_types:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    l2 = await L2CAPSocket.connect(
                        dst=target_mac, dst_type=peer_type, src=src, timeout=remaining
                    )
                    break
                except asyncio.TimeoutError as e:
                    last_exc = e
                except OSError as e:
                    last_exc = e
                    transient = True
            if l2 is not None:
                break
            if not transient or (deadline - loop.time()) <= _CONNECT_RETRY_DELAY:
                break
            await asyncio.sleep(_CONNECT_RETRY_DELAY)

        if l2 is None:
            raise RuntimeError(f"raw_l2cap: could not connect to {target_mac}: {last_exc}")

        att = _att.ATTClient(l2)
        await att.exchange_mtu()
        services = await att.discover()

        chars: Dict[str, _ResolvedChar] = {}
        for svc in services:
            for ch in svc.characteristics:
                cccd_handle: Optional[int] = None
                for desc in ch.descriptors:
                    if normalize_uuid_str(desc.uuid) == normalize_uuid_str(_att.CCCD_UUID):
                        cccd_handle = desc.handle
                        break
                chars[normalize_uuid_str(ch.uuid)] = _ResolvedChar(
                    value_handle=ch.value_handle, cccd_handle=cccd_handle
                )

        return _GattHandle(l2=l2, att=att, chars_by_uuid=chars)

    async def teardown(self, handle: Any) -> None:
        h: _GattHandle = handle
        try:
            h.att.close()
        finally:
            pass  # L2CAP socket is closed by ATT close path

    # -- char access -------------------------------------------------------
    def _resolve(self, handle: _GattHandle, char_uuid: str) -> _ResolvedChar:
        norm = normalize_uuid_str(char_uuid)
        ch = handle.chars_by_uuid.get(norm)
        if ch is None:
            raise KeyError(f"raw_l2cap: characteristic {char_uuid} not found")
        return ch

    async def read(self, handle: Any, char_uuid: str) -> bytes:
        h: _GattHandle = handle
        ch = self._resolve(h, char_uuid)
        return await h.att.read(ch.value_handle)

    async def write(self, handle: Any, char_uuid: str, data: bytes) -> None:
        h: _GattHandle = handle
        ch = self._resolve(h, char_uuid)
        await h.att.write(ch.value_handle, data)

    # -- notifications -----------------------------------------------------
    async def notify_iter(
        self, handle: Any, char_uuid: str, duration_s: float
    ) -> AsyncIterator[NotifyEvent]:
        h: _GattHandle = handle
        ch = self._resolve(h, char_uuid)
        if ch.cccd_handle is None:
            raise RuntimeError(f"raw_l2cap: char {char_uuid} has no CCCD")

        queue: asyncio.Queue[NotifyEvent] = asyncio.Queue()

        def cb(data: bytearray) -> None:
            queue.put_nowait(NotifyEvent(t_ns=perf_ns(), value=bytes(data)))

        h.att.set_notify_handler(ch.value_handle, cb)
        await h.att.write(ch.cccd_handle, b"\x01\x00")
        try:
            deadline = perf_ns() + int(duration_s * 1_000_000_000)
            while perf_ns() < deadline:
                try:
                    timeout = max(0.001, (deadline - perf_ns()) / 1_000_000_000)
                    yield await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
        finally:
            try:
                await h.att.write(ch.cccd_handle, b"\x00\x00")
            except Exception:
                pass
            h.att.remove_notify_handler(ch.value_handle)


def stack(cfg) -> Stack:
    return RawL2capStack(adapter=cfg.adapter)
