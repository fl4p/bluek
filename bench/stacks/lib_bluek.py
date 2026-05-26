"""Layer 2 stack: bluek public API."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional

from bluek import BleakClient, BleakScanner
from bluek.device import AdvertisementData, BLEDevice

from bench.measure.clock import perf_ns
from .base import AdvertEvent, NotifyEvent, Stack


NAME = "lib_bluek"


class BluekStack:
    NAME = NAME

    def __init__(self, adapter: str = "hci0"):
        self._adapter = adapter

    async def scan_iter(self, duration_s: float) -> AsyncIterator[AdvertEvent]:
        queue: asyncio.Queue[AdvertEvent] = asyncio.Queue()

        def on_detect(dev: BLEDevice, adv: AdvertisementData) -> None:
            queue.put_nowait(
                AdvertEvent(
                    t_ns=perf_ns(),
                    address=dev.address,
                    rssi=adv.rssi if adv.rssi is not None else dev.rssi,
                    name=adv.local_name or dev.name,
                )
            )

        scanner = BleakScanner(detection_callback=on_detect, adapter=self._adapter)
        await scanner.start()
        try:
            deadline = perf_ns() + int(duration_s * 1_000_000_000)
            while perf_ns() < deadline:
                try:
                    timeout = max(0.001, (deadline - perf_ns()) / 1_000_000_000)
                    yield await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
        finally:
            await scanner.stop()

    async def setup_gatt(self, target_mac: str, address_type: int = 1) -> Any:
        # BleakClient.connect() now does its own short mgmt-level pre-scan so
        # the kernel L2CAP LE connect has a recent advert observation. No
        # explicit BleakScanner needed here.
        client = BleakClient(target_mac, adapter=self._adapter)
        await client.connect()
        return client

    async def teardown(self, handle: Any) -> None:
        client: BleakClient = handle
        await client.disconnect()

    async def read(self, handle: Any, char_uuid: str) -> bytes:
        client: BleakClient = handle
        return bytes(await client.read_gatt_char(char_uuid))

    async def write(self, handle: Any, char_uuid: str, data: bytes) -> None:
        client: BleakClient = handle
        await client.write_gatt_char(char_uuid, data, response=True)

    async def notify_iter(
        self, handle: Any, char_uuid: str, duration_s: float
    ) -> AsyncIterator[NotifyEvent]:
        client: BleakClient = handle
        queue: asyncio.Queue[NotifyEvent] = asyncio.Queue()

        def cb(_char, value: bytearray) -> None:
            queue.put_nowait(NotifyEvent(t_ns=perf_ns(), value=bytes(value)))

        await client.start_notify(char_uuid, cb)
        try:
            deadline = perf_ns() + int(duration_s * 1_000_000_000)
            while perf_ns() < deadline:
                try:
                    timeout = max(0.001, (deadline - perf_ns()) / 1_000_000_000)
                    yield await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
        finally:
            await client.stop_notify(char_uuid)


def stack(cfg) -> Stack:
    return BluekStack(adapter=cfg.adapter)
