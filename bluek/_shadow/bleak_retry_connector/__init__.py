"""Minimal `bleak_retry_connector` shim backed by bluek.

Only the surface aiobmsble uses: ``BLEAK_TIMEOUT`` and ``establish_connection``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from bluek import BLEDevice, BleakClient

BLEAK_TIMEOUT: float = 20.0


class BleakNotFoundError(Exception):
    """Compatibility alias used by some callers (e.g. batmon-ha)."""


async def establish_connection(
    client_class: type,
    device: BLEDevice,
    name: str,
    disconnected_callback: Optional[Callable[[Any], None]] = None,
    max_attempts: int = 4,
    **kwargs: Any,
) -> BleakClient:
    """Create a client of ``client_class`` for ``device`` and connect, with retries.

    Extra kwargs (e.g. ``services=``, ``cached_services=``) are accepted and
    ignored — bluek discovers services on connect.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        client = client_class(device, disconnected_callback=disconnected_callback)
        try:
            await client.connect()
            return client
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(0.25 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError(f"could not connect to {name}")
