"""The shadow makes `import bleak` (and bleak_retry_connector) resolve to bluek."""

import importlib
import sys


def test_shadow_redirects_bleak():
    # Drop any previously-imported bleak so the shim resolves fresh.
    for name in list(sys.modules):
        if name == "bleak" or name.startswith("bleak.") or name.startswith("bleak_retry_connector"):
            del sys.modules[name]

    import bluek.shadow  # noqa: F401

    bleak = importlib.import_module("bleak")
    assert "bluek/_shadow/bleak" in bleak.__file__.replace("\\", "/")

    from bleak import BleakClient, BleakScanner  # noqa: F401
    from bleak.backends.characteristic import BleakGATTCharacteristic  # noqa: F401
    from bleak.backends.device import BLEDevice  # noqa: F401
    from bleak.uuids import normalize_uuid_str

    assert BleakClient.__module__ == "bluek.client"
    assert normalize_uuid_str("ffe0") == "0000ffe0-0000-1000-8000-00805f9b34fb"

    # aiobmsble>=0.25 imports all four of these; a missing one surfaces as a
    # cryptic ImportError that makes every aiobmsble BMS look like an "Unknown
    # device type" (batmon-ha #385). Import them the way aiobmsble does — via the
    # redirected bare `bleak_retry_connector` name, not the dotted submodule path
    # — so this actually exercises the shadow mechanism.
    from bleak_retry_connector import (  # noqa: F401
        BLEAK_TIMEOUT,
        MAX_CONNECT_ATTEMPTS,
        close_stale_connections,
        establish_connection,
    )

    assert BLEAK_TIMEOUT == 20.0
    assert isinstance(MAX_CONNECT_ATTEMPTS, int) and MAX_CONNECT_ATTEMPTS >= 1


def test_shadow_bleak_retry_connector_close_stale_is_awaitable_noop():
    # close_stale_connections must exist through the shadow and be an awaitable
    # no-op accepting aiobmsble's call shape: (device, only_other_adapters=...).
    import asyncio
    import importlib
    import inspect
    import sys

    for name in list(sys.modules):
        if name == "bleak" or name.startswith("bleak.") or name.startswith("bleak_retry_connector"):
            del sys.modules[name]
    import bluek.shadow  # noqa: F401

    brc = importlib.import_module("bleak_retry_connector")
    assert "bluek/_shadow/bleak_retry_connector" in brc.__file__.replace("\\", "/")
    assert inspect.iscoroutinefunction(brc.close_stale_connections)
    asyncio.run(brc.close_stale_connections(object(), only_other_adapters=False))
