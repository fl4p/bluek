"""ATT protocol client + GATT discovery over an L2CAP ATT socket.

Implements the central/client subset: MTU exchange, primary-service /
characteristic / descriptor discovery, read (with long-read continuation),
write (with and without response), and notification/indication handling.

ATT is strictly sequential — one outstanding request at a time — so a single
transaction lock + pending future is sufficient. Notifications/indications are
demultiplexed out of the same stream by opcode.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .exc import BleakError

# -- ATT opcodes ----------------------------------------------------------
ERROR_RSP = 0x01
EXCHANGE_MTU_REQ = 0x02
EXCHANGE_MTU_RSP = 0x03
FIND_INFO_REQ = 0x04
FIND_INFO_RSP = 0x05
READ_BY_TYPE_REQ = 0x08
READ_BY_TYPE_RSP = 0x09
READ_REQ = 0x0A
READ_RSP = 0x0B
READ_BLOB_REQ = 0x0C
READ_BLOB_RSP = 0x0D
READ_BY_GROUP_TYPE_REQ = 0x10
READ_BY_GROUP_TYPE_RSP = 0x11
WRITE_REQ = 0x12
WRITE_RSP = 0x13
WRITE_CMD = 0x52
HANDLE_VALUE_NTF = 0x1B
HANDLE_VALUE_IND = 0x1D
HANDLE_VALUE_CFM = 0x1E

# -- ATT error codes ------------------------------------------------------
ATT_ERR_INVALID_HANDLE = 0x01
ATT_ERR_READ_NOT_PERMITTED = 0x02
ATT_ERR_INSUFFICIENT_AUTHENTICATION = 0x05
ATT_ERR_INVALID_OFFSET = 0x07
ATT_ERR_ATTRIBUTE_NOT_FOUND = 0x0A
ATT_ERR_ATTRIBUTE_NOT_LONG = 0x0B
ATT_ERR_INSUFFICIENT_ENCRYPTION = 0x0F

# -- GATT well-known attribute types (little-endian 16-bit) ---------------
PRIMARY_SERVICE = b"\x00\x28"
CHARACTERISTIC = b"\x03\x28"
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

# -- characteristic property flags ---------------------------------------
PROP_BROADCAST = 0x01
PROP_READ = 0x02
PROP_WRITE_NO_RESPONSE = 0x04
PROP_WRITE = 0x08
PROP_NOTIFY = 0x10
PROP_INDICATE = 0x20
PROP_AUTH_SIGNED_WRITES = 0x40
PROP_EXTENDED = 0x80

_PROPERTY_MAP = [
    (PROP_BROADCAST, "broadcast"),
    (PROP_READ, "read"),
    (PROP_WRITE_NO_RESPONSE, "write-without-response"),
    (PROP_WRITE, "write"),
    (PROP_NOTIFY, "notify"),
    (PROP_INDICATE, "indicate"),
    (PROP_AUTH_SIGNED_WRITES, "authenticated-signed-writes"),
    (PROP_EXTENDED, "extended-properties"),
]

DEFAULT_MTU = 23
PREFERRED_MTU = 247
ATT_TIMEOUT = 10.0


def properties_to_strings(properties: int) -> List[str]:
    return [name for flag, name in _PROPERTY_MAP if properties & flag]


class ATTError(BleakError):
    def __init__(self, req_opcode: int, handle: int, error_code: int):
        self.req_opcode = req_opcode
        self.handle = handle
        self.error_code = error_code
        super().__init__(f"ATT error 0x{error_code:02x} (req 0x{req_opcode:02x}, handle 0x{handle:04x})")


# -- GATT model -----------------------------------------------------------
@dataclass
class GattDescriptor:
    handle: int
    uuid: str


@dataclass
class GattCharacteristic:
    handle: int  # declaration handle
    value_handle: int
    uuid: str
    properties: int
    end_handle: int = 0
    descriptors: List[GattDescriptor] = field(default_factory=list)


@dataclass
class GattService:
    handle: int
    end_handle: int
    uuid: str
    characteristics: List[GattCharacteristic] = field(default_factory=list)


class ATTClient:
    """ATT client bound to a connected :class:`bluek._l2cap.L2CAPSocket`."""

    def __init__(self, l2cap, on_disconnect: Optional[Callable[[Optional[Exception]], None]] = None):
        from .uuids import uuid_from_bytes  # local import avoids cycle at module load

        self._uuid_from_bytes = uuid_from_bytes
        self._l2 = l2cap
        self._loop = asyncio.get_event_loop()
        self._txn_lock = asyncio.Lock()
        self._pending: Optional[asyncio.Future] = None
        self._mtu = DEFAULT_MTU
        self._notify_handlers: Dict[int, Callable[[bytearray], None]] = {}
        self._on_disconnect = on_disconnect
        l2cap.start_reader(self._on_data, self._on_close)

    def close(self) -> None:
        self._l2.close()

    @property
    def mtu(self) -> int:
        return self._mtu

    # -- inbound demux -----------------------------------------------------
    def _on_data(self, data: bytes) -> None:
        if not data:
            return
        opcode = data[0]
        if opcode == HANDLE_VALUE_NTF:
            self._dispatch_notify(int.from_bytes(data[1:3], "little"), data[3:])
            return
        if opcode == HANDLE_VALUE_IND:
            self._dispatch_notify(int.from_bytes(data[1:3], "little"), data[3:])
            # Acknowledge the indication (fire-and-forget).
            self._loop.create_task(self._confirm())
            return
        fut = self._pending
        if fut is not None and not fut.done():
            fut.set_result(bytes(data))

    async def _confirm(self) -> None:
        try:
            await self._l2.send(bytes([HANDLE_VALUE_CFM]))
        except OSError:
            pass

    def _dispatch_notify(self, value_handle: int, value: bytes) -> None:
        cb = self._notify_handlers.get(value_handle)
        if cb is not None:
            cb(bytearray(value))

    def _on_close(self, exc: Optional[Exception]) -> None:
        fut = self._pending
        if fut is not None and not fut.done():
            fut.set_exception(exc or BleakError("disconnected"))
        if self._on_disconnect is not None:
            self._on_disconnect(exc)

    # -- transactions ------------------------------------------------------
    async def _request(self, payload: bytes, expected_opcode: int) -> bytes:
        async with self._txn_lock:
            fut = self._loop.create_future()
            self._pending = fut
            try:
                await self._l2.send(payload)
                data = await asyncio.wait_for(fut, ATT_TIMEOUT)
            finally:
                self._pending = None
        opcode = data[0]
        if opcode == ERROR_RSP:
            req_op, handle, err = struct.unpack_from("<BHB", data, 1)
            raise ATTError(req_op, handle, err)
        if opcode != expected_opcode:
            raise BleakError(f"unexpected ATT opcode 0x{opcode:02x} (wanted 0x{expected_opcode:02x})")
        return data

    async def exchange_mtu(self, mtu: int = PREFERRED_MTU) -> int:
        payload = bytes([EXCHANGE_MTU_REQ]) + mtu.to_bytes(2, "little")
        try:
            data = await self._request(payload, EXCHANGE_MTU_RSP)
        except ATTError:
            return self._mtu
        server_mtu = int.from_bytes(data[1:3], "little")
        self._mtu = max(DEFAULT_MTU, min(mtu, server_mtu))
        return self._mtu

    # -- reads -------------------------------------------------------------
    async def read(self, handle: int) -> bytes:
        data = await self._request(bytes([READ_REQ]) + handle.to_bytes(2, "little"), READ_RSP)
        value = bytearray(data[1:])
        # If the value filled the PDU, more may follow — continue with Read Blob.
        while len(value) >= self._mtu - 1:
            blob = await self._read_blob(handle, len(value))
            if not blob:
                break
            value += blob
        return bytes(value)

    async def _read_blob(self, handle: int, offset: int) -> bytes:
        payload = bytes([READ_BLOB_REQ]) + handle.to_bytes(2, "little") + offset.to_bytes(2, "little")
        try:
            data = await self._request(payload, READ_BLOB_RSP)
        except ATTError as e:
            if e.error_code in (ATT_ERR_INVALID_OFFSET, ATT_ERR_ATTRIBUTE_NOT_LONG):
                return b""
            raise
        return data[1:]

    # -- writes ------------------------------------------------------------
    async def write(self, handle: int, value: bytes) -> None:
        payload = bytes([WRITE_REQ]) + handle.to_bytes(2, "little") + bytes(value)
        await self._request(payload, WRITE_RSP)

    async def write_command(self, handle: int, value: bytes) -> None:
        payload = bytes([WRITE_CMD]) + handle.to_bytes(2, "little") + bytes(value)
        await self._l2.send(payload)

    # -- discovery primitives ---------------------------------------------
    async def read_by_group_type(self, start: int, end: int, group_uuid: bytes):
        results = []
        while start <= end:
            payload = (
                bytes([READ_BY_GROUP_TYPE_REQ])
                + start.to_bytes(2, "little")
                + end.to_bytes(2, "little")
                + group_uuid
            )
            try:
                data = await self._request(payload, READ_BY_GROUP_TYPE_RSP)
            except ATTError as e:
                if e.error_code == ATT_ERR_ATTRIBUTE_NOT_FOUND:
                    break
                raise
            length = data[1]
            entries = data[2:]
            last = start
            for i in range(0, len(entries) - length + 1, length):
                entry = entries[i : i + length]
                h = int.from_bytes(entry[0:2], "little")
                end_h = int.from_bytes(entry[2:4], "little")
                results.append((h, end_h, bytes(entry[4:length])))
                last = end_h
            if last >= 0xFFFF or last < start:
                break
            start = last + 1
        return results

    async def read_by_type(self, start: int, end: int, type_uuid: bytes):
        results = []
        while start <= end:
            payload = (
                bytes([READ_BY_TYPE_REQ]) + start.to_bytes(2, "little") + end.to_bytes(2, "little") + type_uuid
            )
            try:
                data = await self._request(payload, READ_BY_TYPE_RSP)
            except ATTError as e:
                if e.error_code == ATT_ERR_ATTRIBUTE_NOT_FOUND:
                    break
                raise
            length = data[1]
            entries = data[2:]
            last = start
            for i in range(0, len(entries) - length + 1, length):
                entry = entries[i : i + length]
                h = int.from_bytes(entry[0:2], "little")
                results.append((h, bytes(entry[2:length])))
                last = h
            if last >= 0xFFFF or last < start:
                break
            start = last + 1
        return results

    async def find_information(self, start: int, end: int):
        results = []
        while start <= end:
            payload = bytes([FIND_INFO_REQ]) + start.to_bytes(2, "little") + end.to_bytes(2, "little")
            try:
                data = await self._request(payload, FIND_INFO_RSP)
            except ATTError as e:
                if e.error_code == ATT_ERR_ATTRIBUTE_NOT_FOUND:
                    break
                raise
            fmt = data[1]
            uuid_len = 2 if fmt == 0x01 else 16
            entry_len = 2 + uuid_len
            entries = data[2:]
            last = start
            for i in range(0, len(entries) - entry_len + 1, entry_len):
                entry = entries[i : i + entry_len]
                h = int.from_bytes(entry[0:2], "little")
                results.append((h, self._uuid_from_bytes(entry[2:entry_len])))
                last = h
            if last >= 0xFFFF or last < start:
                break
            start = last + 1
        return results

    # -- notifications -----------------------------------------------------
    def set_notify_handler(self, value_handle: int, callback: Callable[[bytearray], None]) -> None:
        self._notify_handlers[value_handle] = callback

    def remove_notify_handler(self, value_handle: int) -> None:
        self._notify_handlers.pop(value_handle, None)

    # -- high-level GATT discovery ----------------------------------------
    async def discover(self) -> List[GattService]:
        """Discover the full primary-service / characteristic / descriptor tree."""
        services: List[GattService] = []
        for h, end_h, value in await self.read_by_group_type(0x0001, 0xFFFF, PRIMARY_SERVICE):
            services.append(GattService(h, end_h, self._uuid_from_bytes(value)))

        for svc in services:
            chars = []
            for decl_handle, value in await self.read_by_type(svc.handle, svc.end_handle, CHARACTERISTIC):
                properties = value[0]
                value_handle = int.from_bytes(value[1:3], "little")
                uuid = self._uuid_from_bytes(value[3:])
                chars.append(GattCharacteristic(decl_handle, value_handle, uuid, properties))
            # Compute each characteristic's end handle (next decl - 1, else service end).
            for i, char in enumerate(chars):
                char.end_handle = chars[i + 1].handle - 1 if i + 1 < len(chars) else svc.end_handle
                # Descriptors live between the value handle and the char's end.
                if char.end_handle > char.value_handle:
                    for dh, duuid in await self.find_information(char.value_handle + 1, char.end_handle):
                        char.descriptors.append(GattDescriptor(dh, duuid))
            svc.characteristics = chars
        return services
