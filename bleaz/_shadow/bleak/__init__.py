"""`bleak` shadow package backed by bleaz.

Placed ahead of site-packages on the import path (via ``import bleaz.shadow``)
so that ``import bleak`` — including third-party libraries such as aiobmsble —
resolves to bleaz instead of the real BlueZ/D-Bus bleak.
"""

from bleaz import (  # noqa: F401
    BLEDevice,
    BleakClient,
    BleakScanner,
    exc,
    uuids,
)
from bleaz.exc import (  # noqa: F401
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
