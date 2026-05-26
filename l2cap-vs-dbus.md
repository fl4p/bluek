# Are L2CAP / mgmt sockets faster than D‑Bus?

Short answer: **yes, raw HCI/L2CAP sockets are faster than going through D‑Bus — but the gap is smaller than it sounds, and "faster at what?" matters.**

## Path comparison

D‑Bus path (Bleak + BlueZ):

```
HCI event → kernel → bluetoothd (parses, updates object model)
         → emits PropertiesChanged → dbus-broker (routes)
         → your process (dbus-fast unmarshals) → callback
```

4 processes, 3 context‑switch pairs, two rounds of (un)marshalling per advertisement.

Raw socket path (what `bluek` does):

```
HCI event → kernel → your process (parse bytes)
```

1 process, 1 read, no daemon, no IPC.

## Where the win is real

- **Scanning / advertisement throughput.** This is where the difference is meaningful. A busy room can produce thousands of adverts/minute, each becoming a `PropertiesChanged` signal under BlueZ. Raw HCI cuts ~4 hops to 1 and skips two serialization passes. Expect noticeably lower CPU, especially on small ARM boxes (RPi, HAOS on N100, etc.).
- **Per‑op latency on small reads/writes.** ATT request → response over an L2CAP socket avoids a round trip through bluetoothd's GATT layer. Microseconds, not milliseconds, but it adds up under load.
- **Memory / deployment footprint.** No bluetoothd, no dbus-daemon/broker. Useful in containers or minimal images.
- **Determinism.** You aren't fighting bluetoothd's state machine (auto‑reconnect, caching, "Device removed", etc.).

### Measured cost of one IPC round-trip

From `bench/microbench/echo.py` on a Pi 5 (kernel 6.12, Python 3.11, 1000 iters/cell):

| payload | dbus p50 | dbus p95 | socket p50 | socket p95 | ratio |
|--------:|---------:|---------:|-----------:|-----------:|------:|
|     1 B |   105 µs |   114 µs |     8.7 µs |     9.0 µs | 12.1× |
|    20 B |   106 µs |   112 µs |     8.9 µs |     9.1 µs | 11.9× |
|   200 B |   107 µs |   113 µs |     9.0 µs |     9.2 µs | 11.9× |
|  1500 B |   111 µs |   116 µs |     9.4 µs |     9.7 µs | 11.7× |

- **`dbus`** is a `dbus-fast` method call through a private `dbus-daemon` to an in-process echo handler — same library Bleak's BlueZ backend uses.
- **`socket`** is `send`/`recv` on an `AF_UNIX/SOCK_SEQPACKET` pair — same syscall surface (and roughly the same kernel cost) as bluek's L2CAP socket I/O.

A D-Bus call has ~100 µs of fixed cost (broker hop + two context-switch pairs + marshalling header) and per-byte cost is tiny across this range. **~96 µs saved per IPC round-trip** is the floor of what bluek's socket path gains.

For a central juggling several BLE devices, that's tens of milliseconds of Pi-core time saved per second — every ATT op and every advert BlueZ surfaces to Python costs at least one D-Bus call.

 A central juggling  several devices and a notify stream easily hits hundreds of these per second — so ~10–30 % of a Pi 5 core just on D-Bus shuttling, which bluek avoids entirely.

### Same RTT, different CPU bill

End-to-end on the same Pi 5, against a bumble peripheral on a second adapter (default ~50 ms connection interval), 200 sequential GATT reads + 200 writes per stack, CPU time read from `/proc/{pid}/stat` over the same 36 s window:

| stack | read p50 | total CPU | breakdown | per-op CPU |
|---|---:|---:|---|---:|
| `bluek`  | 92.0 ms | **90 ms**  | 80 ms python + 10 ms bluetoothd | **0.23 ms/op** |
| `bleak`  | 88.6 ms | **410 ms** | 140 ms python + 100 ms bluetoothd + 170 ms dbus-daemon | **1.03 ms/op** |

Wall-clock RTT is statistically identical — both stacks pay the same ~50 ms of BLE air time per operation, and the ~96 µs/IPC delta is invisible at that scale. The difference shows up in **CPU**: bleak burns ~4.5× more, and the extra ~800 µs/op lines up with the microbench number scaled by the ~8 D-Bus messages a typical GATT op traverses (the call + return, plus `PropertiesChanged` for state, plus Variant unmarshalling on both ends).

So the take is: bluek doesn't make individual operations *faster*, it makes them *cheaper*. On a Pi 5 a saturated BLE central running bleak hits ~25 % of a core just on GATT D-Bus shuttling; through bluek the same workload costs ~5 %, freeing the rest for application logic.

## Where it doesn't really matter

- For most HA workloads (dozens of devices, hundreds of adverts/sec), the actual bottleneck is Python + asyncio scheduling, not D‑Bus marshalling. `dbus-fast` already pushed the marshalling cost into Cython hot paths. So you may save 30–60% of *Bluetooth* CPU and still see the same wall‑clock end‑to‑end behavior.
- One‑shot GATT operations on a single device: the D‑Bus overhead is invisible next to the BLE connection interval (7.5 ms – 4 s) and ATT round‑trip times. Air time dominates by orders of magnitude.

## About `mgmt` specifically

The mgmt socket (`HCI_CHANNEL_CONTROL`) is the *control* plane, not data — power, discoverable, pairing, LE params, allow lists. It's not "faster than D‑Bus" for I/O because it's not on the I/O path; it just lets you do the things bluetoothd would have done, without bluetoothd. The performance gain comes from L2CAP (data) and raw HCI (events), with mgmt handling setup.

## Caveats worth knowing for `bluek`

- Coexistence with `bluetoothd` is the painful part. If bluetoothd is running and "owns" the adapter, your scans and connects will race with it. The usual answers: `hciconfig hciX down` / `btmgmt power off` first, or claim the adapter via mgmt and keep bluetoothd off that index.
- You're now on the hook for ATT/GATT/SMP. Pairing + bonding + LE Secure Connections is the part most projects in this space punt on. Read‑only sensor traffic over unencrypted ATT is easy; anything authenticated is a real project.
- Kernel L2CAP CoC has had a string of quirks across kernel versions (and the recent commit `b509ecf client: retry transient L2CAP connect failures (HCI 0x3E -> ENOSYS)` is exactly the kind of thing that bites). Bleak/BlueZ insulates you from those at the cost of the IPC overhead.

## Bottom line

Faster, yes — and for `bluek`'s use case (a Bleak‑compatible central with a much smaller dependency surface and no daemon), it's the right tradeoff. The IPC ratio itself is ~12× on a Pi 5, but the end-to-end wall-clock win on real BLE is "noticeably less CPU and one fewer moving part" — air-time dominates per-op RTT, and the savings show up as freed Pi-core cycles, not faster individual ATT operations.
