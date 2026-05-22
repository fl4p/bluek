"""bleaz: a bleak-compatible BLE central API talking to the kernel BlueZ stack.

Drop-in for the subset of bleak used by GATT-client applications, with no
D-Bus and no exclusive control of the controller (it coexists with bluetoothd).
Use as::

    import bleaz as bleak
    from bleaz import BleakClient, BleakScanner

or shadow the real bleak transparently::

    import bleaz.shadow  # noqa: F401
"""

from __future__ import annotations

from . import exc, uuids
from .characteristic import (
    BleakGATTCharacteristic,
    BleakGATTDescriptor,
    BleakGATTService,
    BleakGATTServiceCollection,
)
from .client import BleakClient
from .device import AdvertisementData, BLEDevice
from .exc import (
    BleakCharacteristicNotFoundError,
    BleakDeviceNotFoundError,
    BleakError,
)
from .scanner import BleakScanner

__version__ = "0.1.0"

__all__ = [
    "BleakClient",
    "BleakScanner",
    "BLEDevice",
    "AdvertisementData",
    "BleakGATTCharacteristic",
    "BleakGATTDescriptor",
    "BleakGATTService",
    "BleakGATTServiceCollection",
    "BleakError",
    "BleakDeviceNotFoundError",
    "BleakCharacteristicNotFoundError",
    "exc",
    "uuids",
]
