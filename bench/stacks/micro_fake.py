"""Micro-mode stack: drives ATTClient against an in-memory FakePeripheral.

Validates the scenario+measurement plumbing without any kernel sockets.
Numbers here are not directly comparable to real-hardware runs; the run's
JSON records ``mode: "micro"`` so cross-mode comparisons can't happen by
accident.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Optional

from bluek import _att

from bench.config import BenchConfig
from bench.fixtures.fake_peripheral import FakePeripheral
from bench.measure.clock import perf_ns
from .base import AdvertEvent, NotifyEvent, Stack


NAME = "micro_fake"


class MicroFakeStack:
    NAME = NAME

    def __init__(self, cfg: BenchConfig):
        self._cfg = cfg

    # -- scan: synthesize adverts at a configurable rate -------------------
    async def scan_iter(self, duration_s: float) -> AsyncIterator[AdvertEvent]:
        # Emit synthetic adverts at ~50 Hz from one fake device so the
        # advert scenario plumbing has something to measure.
        deadline_ns = perf_ns() + int(duration_s * 1_000_000_000)
        interval = 0.02
        while perf_ns() < deadline_ns:
            yield AdvertEvent(t_ns=perf_ns(), address="00:00:00:00:00:01", rssi=-50, name="fake")
            await asyncio.sleep(interval)

    async def setup_gatt(self, target_mac: str, address_type: int = 1) -> Any:
        peripheral = FakePeripheral(payload_size=self._cfg.payload_size)
        peripheral.notify_payload = bytes((i % 256 for i in range(self._cfg.payload_size)))
        att = _att.ATTClient(peripheral)
        await att.exchange_mtu()
        services = await att.discover()
        chars: Dict[str, Dict[str, Optional[int]]] = {}
        for svc in services:
            for ch in svc.characteristics:
                cccd_handle: Optional[int] = None
                for desc in ch.descriptors:
                    if desc.uuid.endswith("2902-0000-1000-8000-00805f9b34fb"):
                        cccd_handle = desc.handle
                        break
                chars[ch.uuid] = {"value_handle": ch.value_handle, "cccd_handle": cccd_handle}
        # Pre-arm the notify pump (only fires while CCCD is enabled).
        peripheral.start_notify_pump(self._cfg.notify_rate_hz)
        return {"peripheral": peripheral, "att": att, "chars": chars}

    async def teardown(self, handle: Any) -> None:
        handle["peripheral"].close()

    def _vh(self, handle: Any, uuid: str) -> int:
        ch = handle["chars"][uuid]
        return ch["value_handle"]

    def _cccd(self, handle: Any, uuid: str) -> Optional[int]:
        return handle["chars"][uuid]["cccd_handle"]

    async def read(self, handle: Any, char_uuid: str) -> bytes:
        return await handle["att"].read(self._vh(handle, char_uuid))

    async def write(self, handle: Any, char_uuid: str, data: bytes) -> None:
        await handle["att"].write(self._vh(handle, char_uuid), data)

    async def notify_iter(
        self, handle: Any, char_uuid: str, duration_s: float
    ) -> AsyncIterator[NotifyEvent]:
        att = handle["att"]
        cccd = self._cccd(handle, char_uuid)
        if cccd is None:
            raise RuntimeError(f"micro_fake: no CCCD for {char_uuid}")
        vh = self._vh(handle, char_uuid)
        queue: asyncio.Queue[NotifyEvent] = asyncio.Queue()

        def cb(data: bytearray) -> None:
            queue.put_nowait(NotifyEvent(t_ns=perf_ns(), value=bytes(data)))

        att.set_notify_handler(vh, cb)
        await att.write(cccd, b"\x01\x00")
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
                await att.write(cccd, b"\x00\x00")
            except Exception:
                pass
            att.remove_notify_handler(vh)


def stack(cfg: BenchConfig) -> Stack:
    return MicroFakeStack(cfg)
