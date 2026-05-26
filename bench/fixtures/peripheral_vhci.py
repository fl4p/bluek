"""Virtual BLE peripheral + virtual radio via bumble + Linux ``/dev/vhci``.

Architecture (single process, no RF):

    [BlueZ kernel]                  [bumble in-process]
    hciN (central)  <==vhci==> Controller C (radio side)
                                       \\
                                    LocalLink      ← simulated air
                                       /
                              Controller P  (radio side)
                                   |
                              in-mem pipes
                                   |
                               Host + Device + GATT db

BlueZ sees ``hciN`` as a real adapter. The bench targets it with
``--adapter hciN``. raw_l2cap and raw_dbus talk through the kernel to
Controller C; Controller C's "air" is the LocalLink, so adverts and
connections route to Controller P → Host → Device. The connection-interval
RTT floor of real BLE (~50 ms) collapses to microseconds because the
LocalLink dispatches packets synchronously inside one process.

Prints two lines on stdout when ready::

    CENTRAL_READY    <hciN>
    PERIPHERAL_READY <ADDR>

The bench supervisor parses these.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from typing import Optional, Tuple


SVC_UUID16 = 0xFBE0
CHR_READ_UUID16 = 0xFBE1
CHR_WRITE_UUID16 = 0xFBE2
CHR_NOTIFY_UUID16 = 0xFBE3


class MemPipe:
    """In-memory packet pipe. Acts as both a Source (set_packet_sink) and a
    Sink (on_packet). bumble's Host and Controller wire to opposite ends."""

    def __init__(self) -> None:
        self._sink = None
        self.metadata = {}

    def set_packet_sink(self, sink) -> None:
        self._sink = sink

    def on_packet(self, packet: bytes) -> None:
        if self._sink is not None:
            self._sink.on_packet(packet)

    def close(self) -> None:
        pass


async def _open_vhci_and_resolve_hci() -> Tuple[object, str]:
    from bumble.transport import open_transport_or_link
    try:
        before = set(os.listdir("/sys/class/bluetooth"))
    except OSError:
        before = set()
    vhci = await open_transport_or_link("vhci")
    new = None
    for _ in range(50):
        await asyncio.sleep(0.1)
        try:
            now = set(os.listdir("/sys/class/bluetooth"))
        except OSError:
            now = set()
        for n in sorted(now - before):
            if re.fullmatch(r"hci\d+", n):
                new = n
                break
        if new is not None:
            break
    return vhci, (new or "hci?")


async def serve(payload_size: int, notify_rate_hz: int, advert_name: str) -> None:
    from bumble.controller import Controller
    from bumble.core import UUID
    from bumble.device import Device
    from bumble.gatt import Characteristic, CharacteristicValue, Service
    from bumble.hci import Address
    from bumble.host import Host
    from bumble.link import LocalLink

    # Open the single vhci transport that the kernel will see as a new hciN.
    vhci, hci_central = await _open_vhci_and_resolve_hci()
    link = LocalLink()

    # Controller C: kernel ↔ vhci, radio ↔ LocalLink.
    controller_c = Controller(
        "vhciC",
        host_source=vhci.source,
        host_sink=vhci.sink,
        link=link,
        public_address=Address("AA:BB:CC:DD:EE:01/P"),
    )

    # Controller P: in-process host pipes, radio ↔ same LocalLink.
    pipe_h2c = MemPipe()
    pipe_c2h = MemPipe()
    controller_p = Controller(
        "vhciP",
        host_source=pipe_h2c,
        host_sink=pipe_c2h,
        link=link,
        public_address=Address("AA:BB:CC:DD:EE:02/P"),
    )

    # Bumble Host attached to the pipes (so it talks HCI to Controller P).
    host = Host(controller_source=pipe_c2h, controller_sink=pipe_h2c)
    device = Device(
        name=advert_name,
        address=Address("F0:F1:F2:F3:F4:F5"),
        host=host,
    )

    read_value = bytes((i % 256 for i in range(payload_size)))
    notify_payload = bytes((i % 256 for i in range(payload_size)))
    write_holder = {"last": b""}

    def read_fn(_connection):
        return read_value

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
    device.add_service(service)

    await device.power_on()
    await device.start_advertising(auto_restart=True)

    adv_address = device.random_address or device.public_address
    print(f"CENTRAL_READY {hci_central}", flush=True)
    print(f"PERIPHERAL_READY {adv_address}", flush=True)

    interval = 1.0 / notify_rate_hz if notify_rate_hz > 0 else None

    async def notify_pump() -> None:
        while True:
            if not device.connections:
                await asyncio.sleep(0.1)
                continue
            for _connection in list(device.connections.values()):
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
        try:
            await device.power_off()
        except Exception:
            pass
        vhci.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--payload-size", type=int, default=20)
    p.add_argument("--notify-rate", type=int, default=200)
    p.add_argument("--name", default="bluek-vhci")
    args = p.parse_args()

    logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())
    try:
        asyncio.run(serve(args.payload_size, args.notify_rate, args.name))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
