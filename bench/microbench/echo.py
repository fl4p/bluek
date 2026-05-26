"""Pure-IPC echo microbench: D-Bus call RTT vs Unix-socket send/recv RTT.

Compares the actual IPC paths the bench cares about:

- ``dbus``  : caller → ``dbus-daemon`` → server → ``dbus-daemon`` → caller.
              Uses dbus-fast (same library Bleak's BlueZ backend uses), so the
              numbers reflect what Python-on-the-Linux-BT-stack pays per call.
- ``socket``: caller ↔ server over AF_UNIX/SOCK_SEQPACKET. Same syscall surface
              as the L2CAP socket (``socket.send``/``recv``), no broker, no
              marshalling beyond raw bytes — i.e. the floor for what a Python
              app pays for one IPC round-trip.

No BLE, no bluetoothd, no kernel BT subsystem. Just IPC.

Architecture (one process, three roles, run with ``python -m bench.microbench.echo``):

      [client mode]                                 [server-dbus mode]
   sends Echo(bytes) ───── dbus-daemon ────► OBJECT.Echo handler ──┐
   ◄──────────────────────────────────────── return same bytes ◄──┘

      [client mode]                                 [server-socket mode]
   sock.send(bytes)  ─── AF_UNIX/SEQPACKET ───► sock.recv → sock.send ──┐
   ◄────────────────────────────────────────── ◄────────────────────────┘

The default ``--all`` runs the orchestrator: spawns a private dbus-daemon,
spawns both server modes, runs the client against each, emits JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Tuple

from asyncio import create_subprocess_exec as _spawn


# Constants -----------------------------------------------------------------
SERVICE_NAME = "io.bluek.bench.echo"
OBJECT_PATH = "/io/bluek/bench/echo"
INTERFACE = "io.bluek.bench.echo.Echo"


# ---------------------------------------------------------------------------
# server-dbus
# ---------------------------------------------------------------------------
async def serve_dbus(bus_address: str) -> None:
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType
    from dbus_fast.service import ServiceInterface, method

    class Echo(ServiceInterface):
        def __init__(self):
            super().__init__(INTERFACE)

        @method()
        def Echo(self, data: "ay") -> "ay":  # noqa: F821 — dbus-fast type strings
            return data

    bus = await MessageBus(bus_address=bus_address).connect()
    iface = Echo()
    bus.export(OBJECT_PATH, iface)
    await bus.request_name(SERVICE_NAME)
    print("DBUS_SERVER_READY", flush=True)
    await asyncio.Future()  # serve forever


# ---------------------------------------------------------------------------
# server-socket (AF_UNIX/SOCK_SEQPACKET)
# ---------------------------------------------------------------------------
def serve_socket(socket_path: str) -> None:
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.bind(socket_path)
    s.listen(4)
    print("SOCKET_SERVER_READY", flush=True)
    try:
        while True:
            conn, _ = s.accept()
            try:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    conn.send(data)
            finally:
                conn.close()
    finally:
        s.close()
        try:
            os.unlink(socket_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------
async def client_dbus(bus_address: str, payload: bytes, iters: int, warmup: int) -> list[int]:
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_address=bus_address).connect()
    intro = await bus.introspect(SERVICE_NAME, OBJECT_PATH)
    proxy = bus.get_proxy_object(SERVICE_NAME, OBJECT_PATH, intro)
    iface = proxy.get_interface(INTERFACE)
    call = iface.call_echo  # dbus-fast lowercases method names

    # dbus-fast wants bytes for 'ay'.
    arg = bytes(payload)
    # Warmup
    for _ in range(warmup):
        await call(arg)
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        await call(arg)
        t1 = time.perf_counter_ns()
        samples.append(t1 - t0)
    bus.disconnect()
    return samples


def client_socket(socket_path: str, payload: bytes, iters: int, warmup: int) -> list[int]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    # Allow up to ~1s for the server to be listening.
    for _ in range(50):
        try:
            s.connect(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.02)
    else:
        raise RuntimeError(f"could not connect to {socket_path}")

    try:
        for _ in range(warmup):
            s.send(payload)
            s.recv(65536)
        samples = []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            s.send(payload)
            s.recv(65536)
            t1 = time.perf_counter_ns()
            samples.append(t1 - t0)
        return samples
    finally:
        s.close()


def percentile(xs: list[int], p: float) -> int:
    if not xs:
        return 0
    xs = sorted(xs)
    rank = max(0, min(len(xs) - 1, int(p / 100.0 * len(xs)) - (1 if p == 100 else 0)))
    return xs[rank]


def summarize(name: str, payload: int, samples: list[int]) -> dict:
    return {
        "name": name,
        "payload_size": payload,
        "n": len(samples),
        "min_ns": min(samples),
        "p50_ns": percentile(samples, 50),
        "p95_ns": percentile(samples, 95),
        "p99_ns": percentile(samples, 99),
        "max_ns": max(samples),
        "mean_ns": sum(samples) // len(samples),
        "samples_ns": samples,
    }


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
async def orchestrate(args) -> dict:
    if not shutil.which("dbus-daemon"):
        raise SystemExit("dbus-daemon not found in PATH")

    tmp = Path(tempfile.mkdtemp(prefix="bluek-microbench-"))
    bus_socket = tmp / "bus"
    socket_path = tmp / "echo.sock"

    # ---- spawn dbus-daemon on a private socket ----
    config_path = tmp / "bus.conf"
    config_path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={bus_socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
""")
    daemon = await _spawn(
        "dbus-daemon",
        "--nofork",
        f"--config-file={config_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    # Wait for the unix socket to appear (daemon doesn't reliably print address
    # with our config — but it creates the listen socket as soon as it's ready).
    bus_address = f"unix:path={bus_socket}"
    for _ in range(50):
        if bus_socket.exists():
            break
        await asyncio.sleep(0.05)
    if not bus_socket.exists():
        # Drain any error output before giving up.
        try:
            assert daemon.stdout is not None
            data = await asyncio.wait_for(daemon.stdout.read(4096), timeout=1.0)
            sys.stderr.write(f"[dbus-daemon] {data.decode('utf-8', 'replace')}\n")
        except (asyncio.TimeoutError, AssertionError):
            pass
        raise SystemExit(f"dbus-daemon did not create socket at {bus_socket}")
    print(f"[orchestrator] dbus on {bus_address}", flush=True)

    # ---- spawn the two echo servers ----
    dbus_server = await _spawn(
        sys.executable, "-u", "-m", "bench.microbench.echo",
        "server-dbus", "--bus-address", bus_address,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    sock_server = await _spawn(
        sys.executable, "-u", "-m", "bench.microbench.echo",
        "server-socket", "--socket-path", str(socket_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    async def wait_ready(proc, marker: str, name: str):
        assert proc.stdout is not None
        for _ in range(100):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            print(f"[{name}] {text}", flush=True)
            if text == marker:
                return
        raise SystemExit(f"{name} did not become ready")

    try:
        await wait_ready(dbus_server, "DBUS_SERVER_READY", "dbus-server")
        # The socket server's accept() blocks the readiness line until a client
        # connects, so its READY arrives via stdout *before* accept blocks.
        await wait_ready(sock_server, "SOCKET_SERVER_READY", "sock-server")

        # ---- run client measurements ----
        results = []
        for payload_size in args.payload_sizes:
            payload = bytes((i % 256 for i in range(payload_size)))
            print(f"[client] payload={payload_size} ...", flush=True)

            dbus_samples = await client_dbus(bus_address, payload, args.iters, args.warmup)
            results.append(summarize("dbus", payload_size, dbus_samples))

            sock_samples = client_socket(str(socket_path), payload, args.iters, args.warmup)
            results.append(summarize("socket", payload_size, sock_samples))

        return {
            "env": {
                "python": sys.version.split()[0],
                "host": socket.gethostname(),
                "kernel": subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip(),
            },
            "iters": args.iters,
            "warmup": args.warmup,
            "results": results,
        }
    finally:
        for proc in (dbus_server, sock_server):
            if proc.returncode is None:
                proc.send_signal(signal.SIGTERM)
        if daemon.returncode is None:
            daemon.send_signal(signal.SIGTERM)
        for proc in (dbus_server, sock_server, daemon):
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        # tmpdir
        try:
            for p in tmp.iterdir():
                p.unlink(missing_ok=True)
            tmp.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(prog="bench.microbench.echo")
    sub = p.add_subparsers(dest="mode", required=True)

    s_d = sub.add_parser("server-dbus")
    s_d.add_argument("--bus-address", required=True)

    s_s = sub.add_parser("server-socket")
    s_s.add_argument("--socket-path", required=True)

    s_c = sub.add_parser("client")
    s_c.add_argument("--bus-address", required=True)
    s_c.add_argument("--socket-path", required=True)
    s_c.add_argument("--payload-size", type=int, default=20)
    s_c.add_argument("--iters", type=int, default=2000)
    s_c.add_argument("--warmup", type=int, default=200)

    s_o = sub.add_parser("all")
    s_o.add_argument("--payload-sizes", type=int, nargs="+", default=[1, 20, 200, 1500])
    s_o.add_argument("--iters", type=int, default=2000)
    s_o.add_argument("--warmup", type=int, default=200)
    s_o.add_argument("--out", default=None, help="write JSON results here")

    args = p.parse_args()

    if args.mode == "server-dbus":
        asyncio.run(serve_dbus(args.bus_address))
        return 0
    if args.mode == "server-socket":
        serve_socket(args.socket_path)
        return 0
    if args.mode == "client":
        payload = bytes((i % 256 for i in range(args.payload_size)))
        ds = asyncio.run(client_dbus(args.bus_address, payload, args.iters, args.warmup))
        ss = client_socket(args.socket_path, payload, args.iters, args.warmup)
        out = {
            "dbus": summarize("dbus", args.payload_size, ds),
            "socket": summarize("socket", args.payload_size, ss),
        }
        print(json.dumps({**out, "dbus": {**out["dbus"], "samples_ns": "..."}}, indent=2))
        return 0
    if args.mode == "all":
        result = asyncio.run(orchestrate(args))
        out_path = args.out or f"bench/results/microbench-{time.strftime('%Y%m%dT%H%M%S')}-{socket.gethostname()}.json"
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        # short summary table
        print()
        print(f"{'payload':>8} {'method':>8} {'n':>6} {'min':>10} {'p50':>10} {'p95':>10} {'p99':>10} {'max':>10}  (ns)")
        for r in result["results"]:
            print(f"{r['payload_size']:>8} {r['name']:>8} {r['n']:>6} {r['min_ns']:>10} {r['p50_ns']:>10} {r['p95_ns']:>10} {r['p99_ns']:>10} {r['max_ns']:>10}")
        print()
        print(f"wrote {out_path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
