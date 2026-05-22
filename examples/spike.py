#!/usr/bin/env python3
"""Hardware spike: scan -> connect -> discover -> read -> notify, using bluek.

Run on a Linux box with a BlueZ kernel stack and CAP_NET_ADMIN/CAP_NET_RAW
(e.g. as root) while bluetoothd keeps running — bluek is meant to coexist.

    sudo python3 examples/spike.py [TARGET_MAC_OR_NAME] [hciN]

With no target it just scans for ~6s and lists what it sees. With a target it
connects, dumps the GATT tree, reads every readable characteristic, and (if a
notifying characteristic exists) subscribes for a few seconds.
"""

import asyncio
import sys

from bluek import BleakClient, BleakScanner


async def scan(adapter, seconds=6.0):
    scanner = BleakScanner(adapter=adapter)
    await scanner.start()
    print(f"scanning {seconds}s on {adapter or 'default'} ...")
    await asyncio.sleep(seconds)
    await scanner.stop()
    devices = scanner.discovered_devices_and_advertisement_data
    for addr, (dev, adv) in sorted(devices.items()):
        print(f"  {addr}  rssi={adv.rssi:>4}  name={dev.name!r}  type={dev.address_type}")
    return scanner.discovered_devices


async def resolve(target, adapter):
    for dev in await scan(adapter):
        if dev.address.lower() == target.lower() or (dev.name and dev.name == target):
            return dev
    return None


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    adapter = sys.argv[2] if len(sys.argv) > 2 else None

    if not target:
        await scan(adapter)
        return

    dev = await resolve(target, adapter)
    if dev is None:
        print(f"target {target} not found in scan; trying direct connect by address")
        dev = target

    client = BleakClient(dev, adapter=adapter)
    print("connecting ...")
    await client.connect(timeout=15)
    print(f"connected={client.is_connected} mtu-exchanged")

    for service in client.services:
        print(f"service {service.uuid}")
        for char in service.characteristics:
            print(f"  char {char.uuid} [{','.join(char.properties)}] handle={char.value_handle}")
            if "read" in char.properties:
                try:
                    value = await client.read_gatt_char(char)
                    print(f"    = {value.hex()}")
                except Exception as e:  # noqa: BLE001
                    print(f"    read failed: {e}")
            for desc in char.descriptors:
                print(f"    desc {desc.uuid} handle={desc.handle}")

    # Subscribe to the first notifying characteristic, if any.
    notify_char = next(
        (c for s in client.services for c in s.characteristics
         if "notify" in c.properties or "indicate" in c.properties),
        None,
    )
    if notify_char is not None:
        print(f"subscribing to {notify_char.uuid} for 5s ...")

        def on_notify(sender, data):
            print(f"  notify {sender.uuid}: {bytes(data).hex()}")

        await client.start_notify(notify_char, on_notify)
        await asyncio.sleep(5)
        await client.stop_notify(notify_char)

    await client.disconnect()
    print("disconnected")


if __name__ == "__main__":
    asyncio.run(main())
