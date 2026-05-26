"""In-memory GATT peripheral for the micro-mode smoke run.

Adapts the scripted-server pattern from ``tests/test_unit.py:FakeL2CAP``
into something configurable (read payload size, notify rate) so the scenario
plumbing can be exercised without real BLE hardware.

This is NOT a transport substitute for ``raw_l2cap`` / ``lib_bluek``
benchmarks against real peripherals. It exists purely to validate the
measurement pipeline.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable, List, Optional, Tuple

from bluek import _att


# 16-bit UUIDs used for the fake peripheral's GATT db. We mirror the structure
# of a real bumble peripheral (one service, three chars) but at 16-bit UUIDs
# so the wire encoding stays in the simple/2-byte path.
SVC_UUID16 = 0xFBE0
CHR_READ_UUID16 = 0xFBE1
CHR_WRITE_UUID16 = 0xFBE2
CHR_NOTIFY_UUID16 = 0xFBE3
CCCD_UUID16 = 0x2902


def _uuid128_from_uuid16(uuid16: int) -> str:
    return f"0000{uuid16:04x}-0000-1000-8000-00805f9b34fb"


class FakePeripheral:
    """Configurable in-memory ATT server."""

    # GATT layout (handles):
    # 0x0001 = primary service decl
    # 0x0002 = read char decl
    # 0x0003 = read value
    # 0x0004 = write char decl
    # 0x0005 = write value
    # 0x0006 = notify char decl
    # 0x0007 = notify value
    # 0x0008 = notify CCCD

    H_SVC = 0x0001
    H_READ_DECL = 0x0002
    H_READ_VAL = 0x0003
    H_WRITE_DECL = 0x0004
    H_WRITE_VAL = 0x0005
    H_NOTIFY_DECL = 0x0006
    H_NOTIFY_VAL = 0x0007
    H_NOTIFY_CCCD = 0x0008
    H_END = 0x0008

    SVC_UUID = _uuid128_from_uuid16(SVC_UUID16)
    READ_UUID = _uuid128_from_uuid16(CHR_READ_UUID16)
    WRITE_UUID = _uuid128_from_uuid16(CHR_WRITE_UUID16)
    NOTIFY_UUID = _uuid128_from_uuid16(CHR_NOTIFY_UUID16)

    def __init__(self, payload_size: int = 20):
        self.payload_size = payload_size
        self.read_value = bytes((i % 256 for i in range(payload_size)))
        self.last_written: Optional[bytes] = None
        self.writes: List[Tuple[int, bytes]] = []
        self._notify_enabled = False
        self._notify_task: Optional[asyncio.Task] = None
        self._on_data: Optional[Callable[[bytes], None]] = None
        self._loop = asyncio.get_event_loop()
        self.notify_payload = bytes((i % 256 for i in range(payload_size)))
        self.notify_rate_hz = 0  # set by start_notify_pump

    # --- transport hooks (compatible with L2CAPSocket-like API) ---
    def start_reader(self, on_data, on_close=None):
        self._on_data = on_data

    def close(self):
        if self._notify_task is not None:
            self._notify_task.cancel()
            self._notify_task = None

    async def send(self, data: bytes):
        rsp = self._handle(bytes(data))
        if rsp is not None and self._on_data is not None:
            self._loop.call_soon(self._on_data, rsp)

    # --- pump driving ----------------------------------------------------
    def start_notify_pump(self, rate_hz: int) -> None:
        self.notify_rate_hz = rate_hz
        if self._notify_task is None:
            self._notify_task = self._loop.create_task(self._pump())

    async def _pump(self):
        try:
            while True:
                if self._notify_enabled and self.notify_rate_hz > 0 and self._on_data is not None:
                    interval = 1.0 / self.notify_rate_hz
                    pkt = (
                        bytes([_att.HANDLE_VALUE_NTF])
                        + self.H_NOTIFY_VAL.to_bytes(2, "little")
                        + self.notify_payload
                    )
                    self._on_data(pkt)
                    await asyncio.sleep(interval)
                else:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            return

    # --- ATT handler -----------------------------------------------------
    @staticmethod
    def _err(req_op, handle, code):
        return struct.pack("<BBHB", _att.ERROR_RSP, req_op, handle, code)

    def _handle(self, req: bytes) -> Optional[bytes]:
        op = req[0]
        if op == _att.EXCHANGE_MTU_REQ:
            return bytes([_att.EXCHANGE_MTU_RSP]) + (247).to_bytes(2, "little")

        if op == _att.READ_BY_GROUP_TYPE_REQ:
            start, end = struct.unpack_from("<HH", req, 1)
            if start <= self.H_SVC <= end:
                body = (
                    self.H_SVC.to_bytes(2, "little")
                    + self.H_END.to_bytes(2, "little")
                    + SVC_UUID16.to_bytes(2, "little")
                )
                return bytes([_att.READ_BY_GROUP_TYPE_RSP, 6]) + body
            return self._err(op, start, _att.ATT_ERR_ATTRIBUTE_NOT_FOUND)

        if op == _att.READ_BY_TYPE_REQ:
            start, end = struct.unpack_from("<HH", req, 1)
            entries = []
            for decl, val_h, uuid16, props in (
                (self.H_READ_DECL, self.H_READ_VAL, CHR_READ_UUID16, 0x02),
                (self.H_WRITE_DECL, self.H_WRITE_VAL, CHR_WRITE_UUID16, 0x08),
                (self.H_NOTIFY_DECL, self.H_NOTIFY_VAL, CHR_NOTIFY_UUID16, 0x10),
            ):
                if start <= decl <= end:
                    entries.append((decl, val_h, uuid16, props))
            if not entries:
                return self._err(op, start, _att.ATT_ERR_ATTRIBUTE_NOT_FOUND)
            body = b"".join(
                decl.to_bytes(2, "little")
                + bytes([props])
                + val_h.to_bytes(2, "little")
                + uuid16.to_bytes(2, "little")
                for decl, val_h, uuid16, props in entries
            )
            return bytes([_att.READ_BY_TYPE_RSP, 7]) + body

        if op == _att.FIND_INFO_REQ:
            start, end = struct.unpack_from("<HH", req, 1)
            if start <= self.H_NOTIFY_CCCD <= end:
                body = self.H_NOTIFY_CCCD.to_bytes(2, "little") + CCCD_UUID16.to_bytes(2, "little")
                return bytes([_att.FIND_INFO_RSP, 0x01]) + body
            return self._err(op, start, _att.ATT_ERR_ATTRIBUTE_NOT_FOUND)

        if op == _att.READ_REQ:
            (handle,) = struct.unpack_from("<H", req, 1)
            if handle == self.H_READ_VAL:
                return bytes([_att.READ_RSP]) + self.read_value
            if handle == self.H_NOTIFY_CCCD:
                cccd_val = b"\x01\x00" if self._notify_enabled else b"\x00\x00"
                return bytes([_att.READ_RSP]) + cccd_val
            return self._err(op, handle, _att.ATT_ERR_INVALID_HANDLE)

        if op == _att.WRITE_REQ:
            handle = struct.unpack_from("<H", req, 1)[0]
            value = req[3:]
            if handle == self.H_NOTIFY_CCCD:
                self._notify_enabled = value[:2] == b"\x01\x00"
            elif handle == self.H_WRITE_VAL:
                self.last_written = bytes(value)
                self.writes.append((handle, bytes(value)))
            else:
                return self._err(op, handle, _att.ATT_ERR_INVALID_HANDLE)
            return bytes([_att.WRITE_RSP])

        if op == _att.WRITE_CMD:
            handle = struct.unpack_from("<H", req, 1)[0]
            self.writes.append((handle, bytes(req[3:])))
            return None

        return self._err(op, 0, _att.ATT_ERR_INVALID_HANDLE)
