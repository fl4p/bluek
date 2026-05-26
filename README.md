# bluek

A [bleak](https://github.com/hbldh/bleak)-compatible BLE **central** API for
Linux that talks to the **in-kernel** BlueZ stack directly over sockets — **no
D-Bus**, and (unlike [bumble-bleak](https://github.com/fl4p/bumble-bleak)) **no
exclusive control of the controller**. The kernel keeps managing the adapter, so
`bluek` coexists with `bluetoothd` / Home Assistant.

```
app → bluek → bluez (kernel) → hw
```

- **Scanning**: Bluetooth management socket (`HCI_CHANNEL_CONTROL`) — the same
  API `bluetoothd` uses, so discovery coexists.
- **GATT**: an L2CAP socket on the ATT channel (CID `0x0004`); the ATT/GATT
  client protocol is implemented in Python (the `gatttool`/`btgatt-client` model).
- **Pairing**: delegated to `bluetoothctl` (the kernel keeps the keys); no SMP
  in bluek.

`BleakClient.connect()` runs a short (≤2 s) mgmt-level discovery first so the
kernel's L2CAP-LE connect path has a recent advert observation for the peer —
without this, `connect()` silently hangs (when bluetoothd is also scanning)
or returns `EHOSTUNREACH` for the bare-MAC case. The pre-scan bails as soon
as the peer's advert arrives, so the cost is one advertising interval
(typically <500 ms) when a `BleakScanner` was already running.

It's the Linux sibling of [micropython-bleak](https://github.com/fl4p/micropython-bleak)
(wraps `aioble`) and [bumble-bleak](https://github.com/fl4p/bumble-bleak) (wraps
Bumble).

## Usage

```python
import bluek as bleak
from bluek import BleakClient, BleakScanner
```

or transparently shadow the real `bleak`:

```python
import bluek.shadow  # noqa: F401  — makes `import bleak` resolve to bluek
```

The `adapter=` argument accepts an `hciN` name, a controller **MAC**
(`"2C:CF:67:5F:4A:6D"`, re-resolved to its current index on each connect so it
survives USB re-enumeration), or `None`/`"default"` for `hci0`.

## Requirements

Linux with a BlueZ kernel stack. Opening the management socket and L2CAP LE
sockets needs `CAP_NET_ADMIN` / `CAP_NET_RAW` (root, or the equivalent
capabilities in a container).

## Status

Early. Implements the GATT-client subset that `batmon-ha` uses. See
`tests/` for the pure codec tests and `examples/spike.py` for a hardware probe.
