"""`bleak` shadow package backed by bluek.

Placed ahead of site-packages on the import path (via ``import bluek.shadow``)
so that ``import bleak`` — including third-party libraries such as aiobmsble —
resolves to bluek instead of the real BlueZ/D-Bus bleak.
"""

from bluek import (  # noqa: F401
    BLEDevice,
    BleakClient,
    BleakScanner,
    exc,
    uuids,
)
from bluek.exc import (  # noqa: F401
    BleakCharacteristicNotFoundError,
    BleakDeviceNotFoundError,
    BleakError,
)

# Report a bleak-3.x-compatible version for any feature/version checks.
__version__ = "3.0.2"

__all__ = [
    "BleakClient",
    "BleakScanner",
    "BLEDevice",
    "BleakError",
    "BleakDeviceNotFoundError",
    "BleakCharacteristicNotFoundError",
    "exc",
    "uuids",
]
