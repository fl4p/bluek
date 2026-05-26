"""Spawn the bumble peripheral as a subprocess and wait for its ready marker."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import Optional

from asyncio import create_subprocess_exec as _spawn_subprocess  # safe: no shell

from .bluetoothd import adapter_down


READY_PREFIX = "PERIPHERAL_READY "


@asynccontextmanager
async def bumble_peripheral(
    adapter: str = "hci1",
    payload_size: int = 20,
    notify_rate_hz: int = 200,
    release_adapter: bool = True,
    startup_timeout_s: float = 10.0,
):
    """Async context manager: spawns the bumble peripheral, yields its MAC,
    and tears it down on exit."""
    if release_adapter:
        # bumble takes exclusive HCI control; bluetoothd must release first.
        adapter_down(adapter)

    cmd = [
        sys.executable, "-u", "-m", "bench.fixtures.peripheral_bumble",
        "--hci", adapter,
        "--payload-size", str(payload_size),
        "--notify-rate", str(notify_rate_hz),
    ]
    env = dict(os.environ)
    proc = await _spawn_subprocess(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
    )
    mac: Optional[str] = None
    try:
        async def _read_ready() -> str:
            assert proc.stdout is not None
            while True:
                line_b = await proc.stdout.readline()
                if not line_b:
                    raise RuntimeError("peripheral exited before READY")
                line = line_b.decode("utf-8", "replace").strip()
                print(f"[peripheral] {line}", file=sys.stderr)
                if line.startswith(READY_PREFIX):
                    addr = line[len(READY_PREFIX):].strip()
                    # bumble prints "AA:BB:CC:DD:EE:FF/P" or "/R"; strip suffix
                    return addr.split("/", 1)[0]

        mac = await asyncio.wait_for(_read_ready(), timeout=startup_timeout_s)
        yield mac
    finally:
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
