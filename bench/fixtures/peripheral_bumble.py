"""Bumble-based BLE peripheral for benchmarks.

Run as a standalone process (spawned by ``peripheral_supervisor.py``). Hosts
a single service with three characteristics that match the UUIDs in
``bench/config.py``:

- read   (FBE1): returns a fixed payload of configurable size
- write  (FBE2): accepts writes-with-response (and write-without-response)
- notify (FBE3): fires notifications at the configured rate

Usage (standalone, for debugging)::

    sudo hciconfig hci1 down                  # release the adapter
    sudo python -m bench.fixtures.peripheral_bumble \\
        --hci hci1 --payload-size 20 --notify-rate 200

It will print "PERIPHERAL_READY <mac>" on stdout when advertising; the
supervisor reads that line to know it can proceed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional


# 16-bit UUIDs match bench/fixtures/fake_peripheral.py
SVC_UUID16 = 0xFBE0
CHR_READ_UUID16 = 0xFBE1
CHR_WRITE_UUID16 = 0xFBE2
CHR_NOTIFY_UUID16 = 0xFBE3


def _uuid128_from_uuid16(u: int) -> str:
    return f"0000{u:04x}-0000-1000-8000-00805f9b34fb"


async def serve(hci_index: int, payload_size: int, notify_rate_hz: int, advert_name: str = "bluek-bench") -> None:
    # Bumble is imported lazily so the broader bench suite can import this
    # module on systems without bumble installed.
    from bumble.device import Device
    from bumble.transport import open_transport_or_link
    from bumble.gatt import (
        Service,
        Characteristic,
        CharacteristicValue,
    )
    from bumble.core import UUID
    from bumble.hci import Address

    read_value = bytes((i % 256 for i in range(payload_size)))
    notify_payload = bytes((i % 256 for i in range(payload_size)))

    transport = await open_transport_or_link(f"hci-socket:{hci_index}")

    def read_fn(_connection):
        return read_value

    write_holder = {"last": b""}

    def write_fn(_connection, value):
        write_holder["last"] = bytes(value)

    read_char = Characteristic(
        UUID.from_16_bits(CHR_READ_UUID16),
        Characteristic.Properties.READ,
        Characteristic.READABLE,
        CharacteristicValue(read=read_fn),
    )
    write_char = Characteristic(
        UUID.from_16_bits(CHR_WRITE_UUID16),
        Characteristic.Properties.WRITE | Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
        Characteristic.WRITEABLE,
        CharacteristicValue(write=write_fn),
    )
    notify_char = Characteristic(
        UUID.from_16_bits(CHR_NOTIFY_UUID16),
        Characteristic.Properties.NOTIFY,
        Characteristic.READABLE,
        bytes(payload_size),
    )

    service = Service(UUID.from_16_bits(SVC_UUID16), [read_char, write_char, notify_char])

    device = Device.with_hci(
        advert_name,
        Address("F0:F1:F2:F3:F4:F5"),
        transport.source,
        transport.sink,
    )
    device.add_service(service)
    await device.power_on()
    await device.start_advertising(auto_restart=True)

    # Announce readiness for the supervisor.
    # device.random_address is the address bumble actually advertises under
    # (static random) when we pass a /R-style address to with_hci(). The
    # public_address from the HCI adapter is NOT used for advertising.
    adv_address = device.random_address or device.public_address
    print(f"PERIPHERAL_READY {adv_address}", flush=True)

    interval = 1.0 / notify_rate_hz if notify_rate_hz > 0 else None

    async def notify_pump() -> None:
        while True:
            if not device.connections:
                await asyncio.sleep(0.1)
                continue
            for connection in list(device.connections.values()):
                try:
                    await notify_char.notify_subscribers(notify_payload)
                except Exception:
                    pass
            if interval is None:
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(interval)

    pump_task = asyncio.create_task(notify_pump())
    try:
        await asyncio.Future()  # run forever
    finally:
        pump_task.cancel()
        await device.power_off()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hci", default="hci1", help="adapter to bind (hciN)")
    p.add_argument("--payload-size", type=int, default=20)
    p.add_argument("--notify-rate", type=int, default=200)
    p.add_argument("--name", default="bluek-bench")
    args = p.parse_args()

    if not args.hci.startswith("hci"):
        print("--hci must be hciN", file=sys.stderr)
        return 2
    hci_index = int(args.hci[3:])

    try:
        asyncio.run(serve(hci_index, args.payload_size, args.notify_rate, args.name))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
