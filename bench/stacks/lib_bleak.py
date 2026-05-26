"""Layer 2 stack: real bleak public API.

Guards at import time that the bluek shadow is NOT active. If you've imported
``bluek.shadow`` anywhere earlier in this process, ``import bleak`` resolves
to the bundled shim — which would make this stack indistinguishable from
``lib_bluek``. We refuse to load in that case.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

# Guard against shadow activation. `bluek.shadow` import-hook prepends a path
# to sys.path; we detect that by checking whether ``bluek.shadow`` is loaded
# AND active, OR whether ``bleak.__file__`` lives under the shadow directory.
import sys as _sys

if "bluek.shadow" in _sys.modules:
    from bluek.shadow import is_active as _shadow_active
    if _shadow_active():
        raise RuntimeError(
            "bluek.shadow is active; refusing to load lib_bleak stack. "
            "Run this stack in a fresh process that does NOT import bluek.shadow."
        )

import bleak  # noqa: E402
from bleak import BleakClient, BleakScanner  # noqa: E402

_bleak_file = getattr(bleak, "__file__", "") or ""
if "/_shadow/" in _bleak_file or "\\_shadow\\" in _bleak_file:
    raise RuntimeError(
        f"`import bleak` resolved to the bluek shadow ({_bleak_file}). "
        "Ensure bluek/_shadow is not on sys.path before running lib_bleak."
    )

from bench.measure.clock import perf_ns  # noqa: E402
from .base import AdvertEvent, NotifyEvent, Stack  # noqa: E402


NAME = "lib_bleak"


class BleakStack:
    NAME = NAME

    def __init__(self, adapter: str = "hci0"):
        self._adapter = adapter

    async def scan_iter(self, duration_s: float) -> AsyncIterator[AdvertEvent]:
        queue: asyncio.Queue[AdvertEvent] = asyncio.Queue()

        def on_detect(dev, adv) -> None:
            queue.put_nowait(
                AdvertEvent(
                    t_ns=perf_ns(),
                    address=dev.address,
                    rssi=getattr(adv, "rssi", None) or getattr(dev, "rssi", None),
                    name=getattr(adv, "local_name", None) or getattr(dev, "name", None),
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
    return BleakStack(adapter=cfg.adapter)
