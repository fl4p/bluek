"""Stack mini-interface that every benchmark stack module implements.

Stacks are async iterators over BLE work; scenarios are stack-agnostic and
just call into this interface.

Each stack module exposes a ``Stack`` class (or a factory ``stack(cfg)``) and
a module-level ``NAME`` constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


@dataclass
class AdvertEvent:
    """One advertisement event as observed by the stack."""
    t_ns: int               # perf_counter_ns when received
    address: str            # MAC, lower or upper case
    rssi: Optional[int] = None
    name: Optional[str] = None
    raw: Any = None         # backend-specific payload (debugging only)


@dataclass
class NotifyEvent:
    """One notification payload."""
    t_ns: int
    value: bytes


@runtime_checkable
class Stack(Protocol):
    NAME: str

    async def scan_iter(self, duration_s: float) -> AsyncIterator[AdvertEvent]:
        ...

    async def setup_gatt(self, target_mac: str, address_type: int = 1) -> Any:
        ...

    async def teardown(self, handle: Any) -> None:
        ...

    async def read(self, handle: Any, char_uuid: str) -> bytes:
        ...

    async def write(self, handle: Any, char_uuid: str, data: bytes) -> None:
        ...

    async def notify_iter(
        self, handle: Any, char_uuid: str, duration_s: float
    ) -> AsyncIterator[NotifyEvent]:
        ...


class StackError(Exception):
    """Raised by a stack when an operation cannot be performed in this environment."""
