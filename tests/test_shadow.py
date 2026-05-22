"""The shadow makes `import bleak` (and bleak_retry_connector) resolve to bleaz."""

import importlib
import sys


def test_shadow_redirects_bleak():
    # Drop any previously-imported bleak so the shim resolves fresh.
    for name in list(sys.modules):
        if name == "bleak" or name.startswith("bleak.") or name.startswith("bleak_retry_connector"):
            del sys.modules[name]

    import bleaz.shadow  # noqa: F401

    bleak = importlib.import_module("bleak")
    assert "bleaz/_shadow/bleak" in bleak.__file__.replace("\\", "/")

    from bleak import BleakClient, BleakScanner  # noqa: F401
    from bleak.backends.characteristic import BleakGATTCharacteristic  # noqa: F401
    from bleak.backends.device import BLEDevice  # noqa: F401
    from bleak.uuids import normalize_uuid_str

    assert BleakClient.__module__ == "bleaz.client"
    assert normalize_uuid_str("ffe0") == "0000ffe0-0000-1000-8000-00805f9b34fb"

    from bleak_retry_connector import BLEAK_TIMEOUT, establish_connection  # noqa: F401

    assert BLEAK_TIMEOUT == 20.0
