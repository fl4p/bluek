"""Bluetooth management (mgmt) socket — used for LE discovery.

We open ``AF_BLUETOOTH/SOCK_RAW/BTPROTO_HCI`` on ``HCI_CHANNEL_CONTROL``, the
same API ``bluetoothd`` uses. DEVICE_FOUND events are broadcast to *every* open
management socket, so even when something else (bluetoothd) is already
discovering, we still receive advertisements — and our own START_DISCOVERY is
best-effort (a "Busy" reply just means someone else already started it).

The packet codecs are pure functions so they can be unit-tested off-Linux.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import socket
import struct
from typing import Callable, Dict, List, Optional, Tuple

from ._util import bdaddr_to_str

AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", 31)
BTPROTO_HCI = 1
HCI_CHANNEL_CONTROL = 3
HCI_DEV_NONE = 0xFFFF

# Command opcodes
MGMT_OP_READ_CONTROLLER_INFO = 0x0004
MGMT_OP_START_DISCOVERY = 0x0023
MGMT_OP_STOP_DISCOVERY = 0x0024

# Event codes
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
MGMT_EV_DEVICE_FOUND = 0x0012

# START_DISCOVERY address-type bitmask: BR/EDR(1) | LE Public(2) | LE Random(4)
SCAN_TYPE_LE = 0x06

MGMT_STATUS_SUCCESS = 0x00
MGMT_STATUS_BUSY = 0x0A
MGMT_STATUS_ALREADY_PAIRED = 0x18

_HDR = struct.Struct("<HHH")  # opcode/event, index, param_len

_libc = ctypes.CDLL(None, use_errno=True)


class _sockaddr_hci(ctypes.Structure):
    _fields_ = [
        ("hci_family", ctypes.c_ushort),
        ("hci_dev", ctypes.c_ushort),
        ("hci_channel", ctypes.c_ushort),
    ]


# -- pure codecs ----------------------------------------------------------
def encode_command(opcode: int, index: int, params: bytes = b"") -> bytes:
    return _HDR.pack(opcode, index, len(params)) + params


def parse_packet(data: bytes) -> Tuple[int, int, bytes]:
    """Split a received mgmt packet into (event_code, controller_index, params)."""
    event, index, plen = _HDR.unpack(data[:6])
    return event, index, data[6 : 6 + plen]


def parse_eir(eir: bytes) -> Dict[int, bytes]:
    """Parse EIR/AD TLV data into ``{ad_type: value}`` (last wins)."""
    out: Dict[int, bytes] = {}
    i = 0
    n = len(eir)
    while i < n:
        length = eir[i]
        if length == 0 or i + 1 + length > n:
            break
        ad_type = eir[i + 1]
        out[ad_type] = eir[i + 2 : i + 1 + length]
        i += 1 + length
    return out


# AD/EIR types we care about
_AD_SHORT_NAME = 0x08
_AD_COMPLETE_NAME = 0x09


def eir_name(eir: Dict[int, bytes]) -> Optional[str]:
    raw = eir.get(_AD_COMPLETE_NAME) or eir.get(_AD_SHORT_NAME)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace")


class DeviceFound:
    __slots__ = ("address", "address_type", "rssi", "flags", "eir")

    def __init__(self, address: str, address_type: int, rssi: int, flags: int, eir: Dict[int, bytes]):
        self.address = address
        self.address_type = address_type
        self.rssi = rssi
        self.flags = flags
        self.eir = eir


def parse_device_found(params: bytes) -> DeviceFound:
    address = bdaddr_to_str(params[0:6])
    address_type = params[6]
    rssi = struct.unpack_from("<b", params, 7)[0]
    flags = struct.unpack_from("<I", params, 8)[0]
    eir_len = struct.unpack_from("<H", params, 12)[0]
    eir = parse_eir(params[14 : 14 + eir_len])
    return DeviceFound(address, address_type, rssi, flags, eir)


# -- async socket ---------------------------------------------------------
class MgmtSocket:
    """A management socket bound to the control channel, driven by asyncio."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._loop = asyncio.get_event_loop()
        self._closed = False
        # pending command futures keyed by (opcode) -> future((status, data))
        self._pending: Dict[int, asyncio.Future] = {}
        # handlers called as handler(controller_index, DeviceFound)
        self._device_found_handlers: List[Callable[[int, DeviceFound], None]] = []
        self._loop.add_reader(self._sock.fileno(), self._read_ready)

    @classmethod
    def open(cls) -> "MgmtSocket":
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
        s.setblocking(False)
        addr = _sockaddr_hci()
        addr.hci_family = AF_BLUETOOTH
        addr.hci_dev = HCI_DEV_NONE
        addr.hci_channel = HCI_CHANNEL_CONTROL
        if _libc.bind(s.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
            e = ctypes.get_errno()
            s.close()
            raise OSError(e, os.strerror(e), "mgmt bind (need CAP_NET_ADMIN?)")
        return cls(s)

    # -- events ------------------------------------------------------------
    def add_device_found_handler(self, handler: Callable[[int, DeviceFound], None]) -> None:
        self._device_found_handlers.append(handler)

    def remove_device_found_handler(self, handler) -> None:
        if handler in self._device_found_handlers:
            self._device_found_handlers.remove(handler)

    def _read_ready(self) -> None:
        try:
            data = self._sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            return
        if len(data) < 6:
            return
        event, index, params = parse_packet(data)
        if event == MGMT_EV_DEVICE_FOUND:
            df = parse_device_found(params)
            for h in list(self._device_found_handlers):
                h(index, df)
        elif event == MGMT_EV_CMD_COMPLETE:
            opcode, status = struct.unpack_from("<HB", params, 0)
            self._resolve(opcode, status, params[3:])
        elif event == MGMT_EV_CMD_STATUS:
            opcode, status = struct.unpack_from("<HB", params, 0)
            self._resolve(opcode, status, b"")

    def _resolve(self, opcode: int, status: int, data: bytes) -> None:
        fut = self._pending.pop(opcode, None)
        if fut is not None and not fut.done():
            fut.set_result((status, data))

    # -- commands ----------------------------------------------------------
    async def command(self, opcode: int, index: int, params: bytes = b"", timeout: float = 5.0) -> Tuple[int, bytes]:
        fut = self._loop.create_future()
        self._pending[opcode] = fut
        await self._send(encode_command(opcode, index, params))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(opcode, None)

    async def _send(self, data: bytes) -> None:
        while True:
            try:
                self._sock.send(data)
                return
            except (BlockingIOError, InterruptedError):
                fut = self._loop.create_future()
                self._loop.add_writer(self._sock.fileno(), lambda: fut.done() or fut.set_result(None))
                try:
                    await fut
                finally:
                    self._loop.remove_writer(self._sock.fileno())

    async def read_controller_info(self, index: int) -> Optional[str]:
        status, data = await self.command(MGMT_OP_READ_CONTROLLER_INFO, index)
        if status != MGMT_STATUS_SUCCESS or len(data) < 6:
            return None
        return bdaddr_to_str(data[0:6])

    async def start_discovery(self, index: int) -> bool:
        status, _ = await self.command(MGMT_OP_START_DISCOVERY, index, bytes([SCAN_TYPE_LE]))
        # Busy = someone else is already discovering; we still get DEVICE_FOUND.
        return status in (MGMT_STATUS_SUCCESS, MGMT_STATUS_BUSY)

    async def stop_discovery(self, index: int) -> None:
        try:
            await self.command(MGMT_OP_STOP_DISCOVERY, index, bytes([SCAN_TYPE_LE]))
        except (asyncio.TimeoutError, OSError):
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.remove_reader(self._sock.fileno())
        except (OSError, ValueError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass
