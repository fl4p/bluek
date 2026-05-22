"""Pairing delegated to ``bluetoothctl``.

bleaz does not implement SMP; the kernel/bluetoothd performs and stores the
bond. We drive an interactive ``bluetoothctl`` session (spawned without a shell,
arguments passed as a list): enable an agent, pair, answer any passkey/PIN
prompt from the bleak-style ``callback(device, pin, passkey)``, then trust the
device.
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable, Optional

from . import _hci
from .exc import BleakError

_SUCCESS_RE = re.compile(r"Pairing successful|already.*paired|Paired:\s*yes", re.IGNORECASE)
_PASSKEY_RE = re.compile(r"(passkey|PIN code|Enter)", re.IGNORECASE)


def _controller_address(adapter: Optional[str]) -> Optional[str]:
    try:
        return _hci.adapter_address(_hci.adapter_index(adapter))
    except ValueError:
        return None


async def pair_with_bluetoothctl(
    address: str,
    adapter: Optional[str] = None,
    callback: Optional[Callable] = None,
    timeout: float = 30.0,
) -> bool:
    """Pair with ``address`` via an interactive bluetoothctl session."""
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def send(line: str) -> None:
        proc.stdin.write((line + "\n").encode())
        await proc.stdin.drain()

    ctrl = _controller_address(adapter)
    if ctrl:
        await send(f"select {ctrl}")
    await send("agent on")
    await send("default-agent")
    await send(f"pair {address}")

    success = False
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                break
            if not raw:
                break
            line = raw.decode(errors="replace")
            if _SUCCESS_RE.search(line):
                success = True
                break
            if "Failed to pair" in line:
                break
            if _PASSKEY_RE.search(line) and callback is not None:
                answer = callback(address, None, None)
                if isinstance(answer, str):
                    await send(answer)
                elif answer:
                    await send("yes")
    finally:
        if success:
            await send(f"trust {address}")
        await send("quit")
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()

    if not success:
        raise BleakError(f"bluetoothctl pairing with {address} failed")
    return True


async def remove_with_bluetoothctl(address: str, adapter: Optional[str] = None) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        "remove",
        address,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0
