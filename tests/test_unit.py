"""Pure-codec + protocol-logic unit tests (run anywhere, no Bluetooth needed).

The ATT logic is exercised against an in-memory scripted GATT server wired to a
fake L2CAP transport, so discovery / read / write / notify are covered without
hardware.
"""

import asyncio
import errno
import struct

import pytest

from bluek import _att, _hci, _mgmt, uuids
from bluek._att import ATTClient


# -- uuids ----------------------------------------------------------------
def test_normalize_uuid_str():
    assert uuids.normalize_uuid_str("ffe0") == "0000ffe0-0000-1000-8000-00805f9b34fb"
    assert uuids.normalize_uuid_str("180A") == "0000180a-0000-1000-8000-00805f9b34fb"
    full = "12345678-1234-5678-1234-567812345678"
    assert uuids.normalize_uuid_str(full) == full


def test_uuid_bytes_roundtrip():
    assert uuids.uuid_from_bytes(b"\xe0\xff") == "0000ffe0-0000-1000-8000-00805f9b34fb"
    assert uuids.uuid_to_bytes("ffe0") == b"\xe0\xff"
    full = "12345678-1234-5678-1234-567812345678"
    assert uuids.uuid_from_bytes(uuids.uuid_to_bytes(full)) == full
    assert len(uuids.uuid_to_bytes(full)) == 16


# -- mgmt codecs ----------------------------------------------------------
def test_encode_and_parse_command():
    pkt = _mgmt.encode_command(_mgmt.MGMT_OP_START_DISCOVERY, 0, bytes([_mgmt.SCAN_TYPE_LE]))
    event, index, params = _mgmt.parse_packet(pkt)
    assert event == _mgmt.MGMT_OP_START_DISCOVERY
    assert index == 0
    assert params == bytes([_mgmt.SCAN_TYPE_LE])


def test_parse_eir():
    eir = bytes([0x02, 0x01, 0x06]) + bytes([0x05, 0x09]) + b"BMS!"
    parsed = _mgmt.parse_eir(eir)
    assert parsed[0x01] == b"\x06"
    assert parsed[0x09] == b"BMS!"
    assert _mgmt.eir_name(parsed) == "BMS!"


def test_parse_device_found():
    addr = bytes([0x45, 0x23, 0x02, 0x11, 0xA1, 0x20])  # wire order -> 20:A1:11:02:23:45
    eir = bytes([0x08, 0x09]) + b"ANT-BLE"
    params = addr + bytes([0x01]) + struct.pack("<b", -60) + struct.pack("<I", 0) + struct.pack("<H", len(eir)) + eir
    df = _mgmt.parse_device_found(params)
    assert df.address == "20:A1:11:02:23:45"
    assert df.address_type == 1
    assert df.rssi == -60
    assert _mgmt.eir_name(df.eir) == "ANT-BLE"


# -- ATT properties -------------------------------------------------------
def test_properties_to_strings():
    assert _att.properties_to_strings(0x02) == ["read"]
    assert _att.properties_to_strings(0x18) == ["write", "notify"]
    assert _att.properties_to_strings(0x10 | 0x20) == ["notify", "indicate"]


# -- adapter resolution (hciN / MAC) --------------------------------------
def test_adapter_index_hci_and_default():
    assert _hci.adapter_index(None) == 0
    assert _hci.adapter_index("default") == 0
    assert _hci.adapter_index("hci0") == 0
    assert _hci.adapter_index("hci1") == 1
    with pytest.raises(ValueError):
        _hci.adapter_index("wlan0")


def test_adapter_index_mac_resolution(monkeypatch):
    addrs = {0: "0C:EF:15:47:4A:46", 1: "2C:CF:67:5F:4A:6D"}
    monkeypatch.setattr(_hci, "_candidate_hci_indices", lambda: [0, 1])
    monkeypatch.setattr(_hci, "_hci_addr_for_index", lambda i: addrs.get(i))

    assert _hci.adapter_index("2C:CF:67:5F:4A:6D") == 1
    assert _hci.adapter_index("0c:ef:15:47:4a:46") == 0  # case-insensitive
    with pytest.raises(ValueError):
        _hci.adapter_index("AA:BB:CC:DD:EE:FF")


# -- connect retry on transient LE link failure ---------------------------
# HCI 0x3E "connection failed to be established" surfaces as ENOSYS via SO_ERROR
# on RPi-class controllers; it's transient and must be retried, whereas a real
# asyncio.TimeoutError (device absent) must NOT be retried (batmon's scanner
# handles that path).
class _FakeL2:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeATT:
    def __init__(self, l2, on_disconnect=None):
        self._l2 = l2

    async def exchange_mtu(self):
        return 247

    async def discover(self):
        return []

    def close(self):
        self._l2.close()


def _patch_client_deps(monkeypatch, connect_side_effects):
    """Patch client.connect's collaborators; return a call counter list."""
    from bluek import client as client_mod
    from bluek._l2cap import L2CAPSocket

    monkeypatch.setattr(client_mod._hci, "adapter_address", lambda idx: None)
    monkeypatch.setattr(client_mod._hci, "adapter_index", lambda a: 0)
    monkeypatch.setattr(client_mod._att, "ATTClient", _FakeATT)

    calls = []

    async def fake_connect(*, dst, dst_type, src, timeout):
        calls.append(dst_type)
        exc = connect_side_effects[min(len(calls) - 1, len(connect_side_effects) - 1)]
        if exc is not None:
            raise exc
        return _FakeL2()

    monkeypatch.setattr(L2CAPSocket, "connect", staticmethod(fake_connect))
    return calls


def test_connect_retries_transient_enosys(monkeypatch):
    from bluek.client import BleakClient

    # A full candidate-type round (public, random) fails ENOSYS, then the next
    # round succeeds -> proves a real cross-round retry, not just trying the
    # other address type.
    enosys = OSError(errno.ENOSYS, "Function not implemented", "l2cap connect")
    calls = _patch_client_deps(monkeypatch, [enosys, enosys, None])

    client = BleakClient("20:A1:11:02:23:45")
    # short retry delay so the test is fast
    monkeypatch.setattr("bluek.client._CONNECT_RETRY_DELAY", 0.0)
    ok = asyncio.run(client.connect(timeout=5.0))
    assert ok is True
    assert client.is_connected
    assert len(calls) == 3  # 2 failed (public+random) + 1 retry success


def test_connect_does_not_retry_timeout(monkeypatch):
    from bluek.client import BleakClient
    from bluek.exc import BleakDeviceNotFoundError

    # Always times out -> device absent -> must NOT spin on retries.
    calls = _patch_client_deps(monkeypatch, [asyncio.TimeoutError()])
    monkeypatch.setattr("bluek.client._CONNECT_RETRY_DELAY", 0.0)

    client = BleakClient("20:A1:11:02:23:45")
    with pytest.raises(BleakDeviceNotFoundError):
        asyncio.run(client.connect(timeout=5.0))
    # one round over candidate types (public, random); no transient-retry rounds
    assert len(calls) <= 2


# -- scripted GATT server over a fake L2CAP transport ---------------------
class FakeL2CAP:
    """Minimal in-memory GATT server speaking ATT to an ATTClient."""

    SERVICES = [
        dict(decl=1, end=3, uuid=0x180A),
        dict(decl=4, end=7, uuid=0xFFE0),
    ]
    CHARS = [
        dict(decl=2, value=3, props=0x02, uuid=0x2A29, val=b"ACME"),
        dict(decl=5, value=6, props=0x18, uuid=0xFFE1, val=b""),
    ]
    DESCS = [dict(handle=7, uuid=0x2902, val=b"\x00\x00")]

    def __init__(self):
        self._on_data = None
        self._loop = asyncio.get_event_loop()
        self.writes = []
        self.notify_value_handle = 6

    def start_reader(self, on_data, on_close=None):
        self._on_data = on_data

    def close(self):
        pass

    async def send(self, data: bytes):
        rsp = self._handle(bytes(data))
        if rsp is not None:
            self._loop.call_soon(self._on_data, rsp)

    def fire_notification(self, value_handle: int, payload: bytes):
        pkt = bytes([_att.HANDLE_VALUE_NTF]) + value_handle.to_bytes(2, "little") + payload
        self._on_data(pkt)

    @staticmethod
    def _err(req_op, handle, code):
        return struct.pack("<BBHB", _att.ERROR_RSP, req_op, handle, code)

    def _handle(self, req: bytes):
        op = req[0]
        if op == _att.EXCHANGE_MTU_REQ:
            return bytes([_att.EXCHANGE_MTU_RSP]) + (247).to_bytes(2, "little")
        if op == _att.READ_BY_GROUP_TYPE_REQ:
            start, end = struct.unpack_from("<HH", req, 1)
            matched = [s for s in self.SERVICES if start <= s["decl"] <= end]
            if not matched:
                return self._err(op, start, _att.ATT_ERR_ATTRIBUTE_NOT_FOUND)
            body = b"".join(
                s["decl"].to_bytes(2, "little") + s["end"].to_bytes(2, "little") + s["uuid"].to_bytes(2, "little")
                for s in matched
            )
            return bytes([_att.READ_BY_GROUP_TYPE_RSP, 6]) + body
        if op == _att.READ_BY_TYPE_REQ:
            start, end = struct.unpack_from("<HH", req, 1)
            matched = [c for c in self.CHARS if start <= c["decl"] <= end]
            if not matched:
                return self._err(op, start, _att.ATT_ERR_ATTRIBUTE_NOT_FOUND)
            body = b"".join(
                c["decl"].to_bytes(2, "little")
                + bytes([c["props"]])
                + c["value"].to_bytes(2, "little")
                + c["uuid"].to_bytes(2, "little")
                for c in matched
            )
            return bytes([_att.READ_BY_TYPE_RSP, 7]) + body
        if op == _att.FIND_INFO_REQ:
            start, end = struct.unpack_from("<HH", req, 1)
            matched = [d for d in self.DESCS if start <= d["handle"] <= end]
            if not matched:
                return self._err(op, start, _att.ATT_ERR_ATTRIBUTE_NOT_FOUND)
            body = b"".join(d["handle"].to_bytes(2, "little") + d["uuid"].to_bytes(2, "little") for d in matched)
            return bytes([_att.FIND_INFO_RSP, 0x01]) + body
        if op == _att.READ_REQ:
            (handle,) = struct.unpack_from("<H", req, 1)
            for c in self.CHARS:
                if c["value"] == handle:
                    return bytes([_att.READ_RSP]) + c["val"]
            return self._err(op, handle, _att.ATT_ERR_INVALID_HANDLE)
        if op == _att.WRITE_REQ:
            handle = struct.unpack_from("<H", req, 1)[0]
            self.writes.append((handle, req[3:]))
            return bytes([_att.WRITE_RSP])
        if op == _att.WRITE_CMD:
            handle = struct.unpack_from("<H", req, 1)[0]
            self.writes.append((handle, req[3:]))
            return None
        return self._err(op, 0, _att.ATT_ERR_INVALID_HANDLE)


async def _discover():
    fake = FakeL2CAP()
    att = ATTClient(fake)
    await att.exchange_mtu()
    services = await att.discover()
    return fake, att, services


def test_gatt_discovery():
    fake, att, services = asyncio.run(_discover())
    assert len(services) == 2
    uuids_found = {s.uuid for s in services}
    assert uuids.normalize_uuid_16(0x180A) in uuids_found
    assert uuids.normalize_uuid_16(0xFFE0) in uuids_found

    # discover() returns the raw _att GATT model (properties is an int bitmask).
    svc2 = next(s for s in services if s.uuid == uuids.normalize_uuid_16(0xFFE0))
    assert len(svc2.characteristics) == 1
    char = svc2.characteristics[0]
    assert char.uuid == uuids.normalize_uuid_16(0xFFE1)
    assert char.value_handle == 6
    assert sorted(_att.properties_to_strings(char.properties)) == ["notify", "write"]
    assert len(char.descriptors) == 1
    assert char.descriptors[0].handle == 7
    assert char.descriptors[0].uuid == uuids.normalize_uuid_16(0x2902)


def test_read_and_notify():
    async def run():
        fake = FakeL2CAP()
        att = ATTClient(fake)
        await att.exchange_mtu()
        value = await att.read(3)
        assert value == b"ACME"

        received = []
        att.set_notify_handler(6, lambda data: received.append(bytes(data)))
        fake.fire_notification(6, b"\x01\x02\x03")
        await asyncio.sleep(0)
        assert received == [b"\x01\x02\x03"]

        await att.write(7, b"\x01\x00")
        assert fake.writes[-1] == (7, b"\x01\x00")

    asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
