# bluek bench — D-Bus/BlueZ vs raw L2CAP/mgmt benchmark suite

Quantifies bluek's "skip D-Bus, talk to the kernel" claim. Two surface areas:

- **`microbench/`** — pure-IPC round-trip latency: `dbus-fast` call through
  `dbus-daemon` vs `AF_UNIX/SOCK_SEQPACKET` send/recv. No BLE, no
  `bluetoothd`. Measured ~**12× speedup** for the raw-socket path on a Pi 5
  (~96 µs saved per call, payload-independent). Headline number for the
  D-Bus-vs-socket question; see `l2cap-vs-dbus.md`.
- **End-to-end benchmark (`runner.py`)** — drives full BLE scenarios
  (advert / GATT RTT / notify) through four stacks (raw L2CAP, raw D-Bus,
  `bluek`, `bleak`). Useful for shape, but real-hardware RTT is dominated
  by BLE air-time (~50 ms+ on a default connection interval) which buries
  the IPC delta. Run it for verifying correctness end-to-end; rely on the
  microbench for IPC-cost numbers.

Results land in `bench/results/<run_id>.json` (per-sample data + summary
stats + environment metadata). `python -m bench.report` aggregates one or
more end-to-end JSONs into a Markdown table.

## Layout

```
bench/
  config.py                BenchConfig dataclass for the end-to-end runner
  runner.py                end-to-end entrypoint (advert/read_write/notify × stacks)
  report.py                end-to-end JSON -> Markdown table
  measure/                 clock, cpu, memory, recorder, jsonout
  scenarios/               advert, read_write, notify (stack-agnostic)
  stacks/                  raw_l2cap, raw_dbus, lib_bluek, lib_bleak, micro_fake
  fixtures/                fake_peripheral, bumble peripheral, vhci attempt, bluetoothd helpers
  microbench/              standalone D-Bus-vs-Unix-socket IPC microbench
  results/                 gitignored output (one JSON per run, end-to-end or microbench)
```

## IPC microbench (start here)

Standalone — doesn't depend on any BLE state. Spins up a private
`dbus-daemon`, a D-Bus echo server (via `dbus-fast`), and an
`AF_UNIX/SOCK_SEQPACKET` echo server. Times round-trips against each
across multiple payload sizes.

```bash
cd ~/bluek                                 # any Linux box with dbus-daemon
.venv/bin/python -m bench.microbench.echo all \
    --payload-sizes 1 20 200 1500 --iters 1000 --warmup 100
```

Measured on havan (Pi 5, kernel 6.12, Python 3.11, 1000 iters/cell):

| payload | dbus p50 | dbus p95 | socket p50 | socket p95 | ratio |
|--------:|---------:|---------:|-----------:|-----------:|------:|
|     1 B |   105 µs |   114 µs |     8.7 µs |     9.0 µs | 12.1× |
|    20 B |   106 µs |   112 µs |     8.9 µs |     9.1 µs | 11.9× |
|   200 B |   107 µs |   113 µs |     9.0 µs |     9.2 µs | 11.9× |
|  1500 B |   111 µs |   116 µs |     9.4 µs |     9.7 µs | 11.7× |

- D-Bus call has ~100 µs of fixed cost (broker hop + two context-switch
  pairs + marshalling header); per-byte cost is tiny across this range.
- Each BLE GATT op (read/write) and each advert BlueZ surfaces to Python
  is at least one D-Bus call ≈ ~100 µs of IPC. bluek's socket path pays
  ~9 µs. The 96 µs/op difference is what `l2cap-vs-dbus.md` is about.

## End-to-end runner modes

### micro

In-process, no kernel sockets. A single `micro_fake` stack drives the
scenario plumbing against `bench/fixtures/fake_peripheral.py` (an ATT
server built on the same pattern as `tests/test_unit.py:FakeL2CAP`).
Useful for:

- Validating the measurement pipeline on any platform (works on macOS).
- Smoke-testing CI without hardware.
- Quick iteration on scenario / recorder changes.

The absolute numbers are **not** comparable to real BLE — there is no
radio, no kernel, no D-Bus. `mode: "micro"` is recorded in the output so
you don't accidentally diff against integration runs.

```bash
python -m bench.runner --mode micro \
    --target 00:00:00:00:00:01 \
    --duration 0.5 --rtt-iters 100 --warmup-iters 10 --warmup-s 0.1
python -m bench.report bench/results/
```

### integration (Linux + BLE hardware)

Drives all four real stacks against either a real BLE peripheral or the
bundled `bumble`-based mock peripheral on a second adapter.

Hardware prereqs:

- **A Linux host with BlueZ.** Tested target: Raspberry Pi 4 / 5 running
  HAOS or Pi OS. Any x86 Linux box with BT works for development.
- **Two HCI adapters** are recommended: one for the central (the stack
  under test), one for the bumble peripheral. The onboard Pi BT can be
  either side; a USB BT5 dongle as the central is the better setup
  because the onboard radio shares SDIO with Wi-Fi.
- A real BLE peripheral MAC for the integration variant.

```bash
sudo apt-get install -y bluez
python -m venv /opt/bench-venv
/opt/bench-venv/bin/pip install -e /path/to/bluek
/opt/bench-venv/bin/pip install -r /path/to/bluek/bench/requirements.txt
hciconfig -a   # confirm hci0 + hci1

# release hci1 so bumble can claim it
sudo hciconfig hci1 down

# integration against a real peripheral
sudo /opt/bench-venv/bin/python -m bench.runner \
    --target AA:BB:CC:DD:EE:FF \
    --adapter hci0 \
    --duration 10 --rtt-iters 1000 --notify-rate 200

# integration with the bundled bumble peripheral on hci1
sudo /opt/bench-venv/bin/python -m bench.runner \
    --use-mock-peripheral --peripheral-adapter hci1 \
    --adapter hci0

python -m bench.report bench/results/
```

## What each stack measures

| Stack | Transport | Where it lives |
|---|---|---|
| `raw_l2cap` | Direct L2CAP + mgmt sockets | `bench/stacks/raw_l2cap.py` (uses `bluek._l2cap`, `bluek._att`, `bluek._mgmt`) |
| `raw_dbus`  | Hand-rolled `dbus-fast` → BlueZ | `bench/stacks/raw_dbus.py` |
| `lib_bluek` | bluek's public API | `bench/stacks/lib_bluek.py` |
| `lib_bleak` | bleak's public API (→ dbus-fast → BlueZ) | `bench/stacks/lib_bleak.py` |
| `micro_fake` | In-process FakePeripheral | `bench/stacks/micro_fake.py` (micro mode only) |

Expect `raw_l2cap` ≈ `lib_bluek` and `raw_dbus` ≈ `lib_bleak` within a few
percent — both layers traverse essentially the same underlying transport.
The interesting axis is socket-vs-D-Bus, not raw-vs-library.

## bluetoothd coordination

| Stack | bluetoothd requirement |
|---|---|
| `raw_dbus`, `lib_bleak` | Must be running and own the central adapter. |
| `raw_l2cap`, `lib_bluek` scan | Coexists fine (mgmt socket is broadcast). |
| `raw_l2cap`, `lib_bluek` GATT | Must NOT hold an open connection to the test peripheral. |

The runner calls `bluetoothctl disconnect <addr>` before bluek GATT
scenarios. Pass `--isolate-adapter` to also `btmgmt power off` the central
adapter around bluek runs (more reproducible numbers, less realistic).

## Measurement protocol

- Warmup is 50 iterations (RTT) or 2 s (window scenarios); discarded.
- 1000 iterations per RTT scenario; 10 s window for advert + notify.
- `time.perf_counter_ns()` brackets the public call. Per-sample latency is
  kept in the JSON, not just summary stats, so you can re-aggregate later.
- CPU time is read from `/proc/{pid}/stat` (utime+stime) for the Python
  process, plus `bluetoothd`, `dbus-broker`, `dbus-daemon` (any that
  exist). Deltas across the measurement window.
- The advert scenario records `time_to_first_device_ns`. The notify
  scenario records inter-arrival p50/p95/p99.

**Note on advert counts:** the D-Bus path (`raw_dbus`, `lib_bleak`) sees
events at the rate BlueZ emits `InterfacesAdded` / `PropertiesChanged`,
which is *not* 1:1 with HCI advertisements — BlueZ deduplicates per
device. The raw L2CAP/mgmt path sees every advert the controller
reports. The scenario annotates this in its `notes` field.

## What success looks like

For the **microbench**:

1. **Hard:** the run writes a JSON with all `payload_size × {dbus, socket}`
   cells populated and emits the summary table.
2. **Soft:** `dbus` p50 is at least 10× the `socket` p50 across all
   payload sizes (Pi-class hardware). The actual measured ratio on havan
   was ~12×; substantially smaller would suggest an unusually fast
   `dbus-broker` or unusually slow socket path.

For the **end-to-end runner**:

1. **Hard:** one run produces one well-formed JSON with all targeted
   `scenario × stack` cells populated, no orchestration errors.
2. **Hard:** two consecutive runs yield p50 read-RTT within ±10 % per
   stack (jitter floor).
3. **Realism:** RTT against a real BLE peer is **air-time-dominated** at
   default bumble parameters — ~88 ms read p50 observed on havan, which
   is the BLE connection interval (~50 ms) plus a packet round-trip. The
   IPC delta (~100 µs) is in the noise. **Do not** rely on absolute RTT
   numbers to size the D-Bus cost — use the microbench. The runner's job
   is shape correctness and end-to-end smoke, not IPC measurement.

## Common pitfalls

- **EPERM on the mgmt or HCI raw socket.** The bench needs
  `CAP_NET_ADMIN` / `CAP_NET_RAW` (open mgmt, read BD_ADDR via ioctl).
  Run with `sudo` or grant the capability to the python interpreter.
- **`raw_l2cap` / `lib_bluek` connect times out silently.** The Linux
  kernel L2CAP LE connect needs a recent advert observation to know the
  peer's address type and route; without it, `connect()` either hangs
  (with `bluetoothd` active) or returns `EHOSTUNREACH`. Since bluek
  0.1.x `BleakClient.connect()` does a short mgmt-level pre-scan itself,
  so `lib_bluek` works without an explicit `BleakScanner` step. The
  `raw_l2cap` stack mirrors that pre-scan inline (it bypasses
  `BleakClient`).
- **`raw_l2cap` ENOSYS / HCI 0x3E.** Transient link-layer failure on a
  flaky / weak peer. The stack mirrors `bluek.client.BleakClient`'s
  retry. If it persists, move the central closer to the peripheral.
- **Bumble peripheral target MAC mismatch.** Bumble advertises with
  `device.random_address` (the `F0:F1:F2:F3:F4:F5/R` we configure), not
  `device.public_address` (the HCI adapter's MAC). The supervisor parses
  the right field; if you wire to bumble directly, mirror that.
- **`raw_dbus` cannot find the device.** BlueZ only exposes
  `org.bluez.GattCharacteristic1` after `ServicesResolved` is true. The
  stack waits up to 5 s; bump the timeout in `setup_gatt` for slow peers.
- **`raw_dbus` Device1.Connect fails with `br-connection-canceled`.**
  Stale BlueZ state from a previous run, often after `raw_l2cap` ran
  first and didn't clear bonding. `bluetoothctl disconnect <addr>` first,
  or reset both adapters.
- **`lib_bleak` refuses to load.** You imported `bluek.shadow` somewhere
  earlier, redirecting `bleak` → `bluek`. Run in a fresh process tree.
- **Bumble can't bind the peripheral adapter.** `bluetoothd` is holding
  it. `sudo hciconfig hci1 down` releases it. The supervisor does this
  automatically when called with `release_adapter=True` (default).

## Status

**Microbench:** complete and validated. Measured numbers on havan (Pi 5)
are stable across reruns.

**End-to-end runner — what's validated:**

- Micro mode end-to-end on macOS (`FakeL2CAP` plumbing).
- `raw_l2cap`: advert + read/write against bumble on a second adapter,
  end-to-end on havan (read p50 88 ms, air-time-dominated).
- `lib_bluek` + `lib_bleak` modules: import + protocol parity verified
  on macOS; runtime smoke is the same code paths as `raw_l2cap`.
- Bumble peripheral on a real HCI adapter (`peripheral_bumble.py`): runs
  on havan, advertising at the configured `random_address`.

**End-to-end runner — known issues:**

- `raw_dbus`: implementation complete but trips `br-connection-canceled`
  intermittently when run after `raw_l2cap`. State-cleanup work needed
  between stacks (force-disconnect + brief delay) before this is
  reliable for full matrix runs.
- Notify: client-observed rate of 0 in the integration smoke when notify
  ran after read/write in the same matrix. Suspected bumble re-subscribe
  state issue across teardown/reconnect. Reproduces; not yet root-caused.
- `peripheral_vhci.py` (the no-RF attempt): present in the repo but
  **does not work**. `/dev/vhci` exposes a new `hciN`, but bumble's
  `Controller` doesn't fully implement the Linux kernel's HCI init
  sequence past `HCI_RESET`, so the adapter never reaches `UP RUNNING`.
  Left in place as a starting point for a future kernel-facing HCI
  emulator; do not enable.

**Dependency pin:** `bumble==0.0.198` (PyPI doesn't ship `0.0.197`).
`psutil` was dropped — `/proc/{pid}/stat` is the canonical path on the
Pi target.

The microbench is the canonical reference for the D-Bus-vs-socket
question. The end-to-end runner is useful for verifying correctness of
the stacks end-to-end against real BLE state; treat its absolute RTT
numbers as air-time-dominated.
