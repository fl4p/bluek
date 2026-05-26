"""Layer 1 stack: hand-rolled dbus-fast calls against BlueZ.

Talks directly to org.bluez over the system bus, without bleak in the loop.
This is the "raw" D-Bus counterpart to ``raw_l2cap`` — what a Python program
would do if it owned the BlueZ wire format itself.

Notes / limitations:

- BlueZ deduplicates advertisements per-device. The advert scenario's
  observed rate is the rate of ``InterfacesAdded`` + ``PropertiesChanged``,
  NOT the underlying HCI advert rate. The scenario annotates this in
  ``notes``.
- A Device1.Connect call waits for ``ServicesResolved`` before returning,
  so MTU exchange + service discovery are folded into the connect cost.
  That's apples-to-apples with ``raw_l2cap.setup_gatt`` which also
  discovers in setup.
- StartNotify is per-process: each call adds a signal subscription.
  ``StopNotify`` is called in teardown.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from bench.config import BenchConfig
from bench.measure.clock import perf_ns
from .base import AdvertEvent, NotifyEvent, Stack


NAME = "raw_dbus"

BLUEZ_SERVICE = "org.bluez"


def _mac_to_dev_path(adapter: str, mac: str) -> str:
    return f"/org/bluez/{adapter}/dev_{mac.upper().replace(':', '_')}"


class RawDbusStack:
    NAME = NAME

    def __init__(self, adapter: str = "hci0"):
        self._adapter = adapter
        self._bus = None  # MessageBus, lazy-connected

    # -- bus lifecycle ----------------------------------------------------
    async def _bus_handle(self):
        if self._bus is None:
            from dbus_fast.aio import MessageBus
            from dbus_fast import BusType
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self._bus

    async def _proxy(self, path: str):
        bus = await self._bus_handle()
        intro = await bus.introspect(BLUEZ_SERVICE, path)
        return bus.get_proxy_object(BLUEZ_SERVICE, path, intro)

    async def _adapter_iface(self):
        proxy = await self._proxy(f"/org/bluez/{self._adapter}")
        return proxy.get_interface("org.bluez.Adapter1")

    async def _managed_objects(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        proxy = await self._proxy("/")
        om = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        return await om.call_get_managed_objects()

    # -- scan -------------------------------------------------------------
    async def scan_iter(self, duration_s: float) -> AsyncIterator[AdvertEvent]:
        bus = await self._bus_handle()
        queue: asyncio.Queue[AdvertEvent] = asyncio.Queue()
        adapter_path = f"/org/bluez/{self._adapter}"
        adapter_prefix = adapter_path + "/dev_"

        def _from_props(path: str, props: Dict[str, Any]) -> Optional[AdvertEvent]:
            if not path.startswith(adapter_prefix):
                return None
            # props values from dbus-fast are Variant; unwrap .value if present.
            def _v(name, default=None):
                v = props.get(name, default)
                return getattr(v, "value", v)
            address = _v("Address")
            if address is None:
                return None
            return AdvertEvent(
                t_ns=perf_ns(),
                address=str(address),
                rssi=_v("RSSI"),
                name=_v("Name") or _v("Alias"),
            )

        # Subscribe to InterfacesAdded for new devices.
        root_proxy = await self._proxy("/")
        om = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")

        def on_added(path: str, interfaces: Dict[str, Dict[str, Any]]) -> None:
            dev = interfaces.get("org.bluez.Device1")
            if dev is None:
                return
            ev = _from_props(path, dev)
            if ev is not None:
                queue.put_nowait(ev)

        om.on_interfaces_added(on_added)

        # Subscribe to PropertiesChanged for known devices (RSSI updates).
        # We listen on the bus directly via a match rule because each existing
        # device has its own proxy; signal name is namespaced by interface.
        from dbus_fast import Message, MessageType
        match = (
            "type='signal',"
            "interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged'"
        )
        await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[match],
            )
        )

        def on_signal(msg: Message) -> None:
            if msg.message_type != MessageType.SIGNAL:
                return
            if msg.member != "PropertiesChanged" or msg.interface != "org.freedesktop.DBus.Properties":
                return
            body = msg.body
            if not body or body[0] != "org.bluez.Device1":
                return
            changed = body[1] if len(body) > 1 else {}
            ev = _from_props(msg.path, changed)
            if ev is not None:
                queue.put_nowait(ev)

        bus.add_message_handler(on_signal)

        adapter_iface = await self._adapter_iface()
        # LE-only discovery filter for fairness with raw_l2cap.
        try:
            from dbus_fast import Variant
            await adapter_iface.call_set_discovery_filter({"Transport": Variant("s", "le")})
        except Exception:
            pass
        await adapter_iface.call_start_discovery()
        try:
            deadline = perf_ns() + int(duration_s * 1_000_000_000)
            while perf_ns() < deadline:
                try:
                    timeout = max(0.001, (deadline - perf_ns()) / 1_000_000_000)
                    yield await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
        finally:
            try:
                await adapter_iface.call_stop_discovery()
            except Exception:
                pass
            try:
                bus.remove_message_handler(on_signal)
            except Exception:
                pass

    # -- setup / teardown -------------------------------------------------
    async def setup_gatt(self, target_mac: str, address_type: int = 1) -> Any:
        bus = await self._bus_handle()
        dev_path = _mac_to_dev_path(self._adapter, target_mac)

        # Make sure BlueZ knows about the device: short discovery if not.
        objs = await self._managed_objects()
        if dev_path not in objs:
            adapter_iface = await self._adapter_iface()
            try:
                await adapter_iface.call_start_discovery()
                # Wait briefly for the device to appear.
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    objs = await self._managed_objects()
                    if dev_path in objs:
                        break
            finally:
                try:
                    await adapter_iface.call_stop_discovery()
                except Exception:
                    pass

        dev_proxy = await self._proxy(dev_path)
        device = dev_proxy.get_interface("org.bluez.Device1")
        dev_props = dev_proxy.get_interface("org.freedesktop.DBus.Properties")

        connected = await dev_props.call_get("org.bluez.Device1", "Connected")
        if not getattr(connected, "value", connected):
            await device.call_connect()

        # Wait for ServicesResolved (BlueZ exposes GATT only after this).
        for _ in range(100):
            sr = await dev_props.call_get("org.bluez.Device1", "ServicesResolved")
            if getattr(sr, "value", sr):
                break
            await asyncio.sleep(0.05)

        # Walk managed objects to build uuid -> (char_path, has_cccd) map.
        objs = await self._managed_objects()
        chars_by_uuid: Dict[str, str] = {}
        char_path_to_uuid: Dict[str, str] = {}
        notify_paths_by_uuid: Dict[str, str] = {}
        descriptor_parents: Dict[str, str] = {}  # descriptor path -> parent char path

        for path, ifaces in objs.items():
            if not path.startswith(dev_path + "/"):
                continue
            char = ifaces.get("org.bluez.GattCharacteristic1")
            if char is not None:
                uuid_v = char.get("UUID")
                uuid = getattr(uuid_v, "value", uuid_v)
                if uuid:
                    chars_by_uuid[str(uuid).lower()] = path
                    char_path_to_uuid[path] = str(uuid).lower()
                continue
            desc = ifaces.get("org.bluez.GattDescriptor1")
            if desc is not None:
                # parent path of "/.../charXXXX/descriptorYYYY" is "/.../charXXXX"
                parent = path.rsplit("/", 1)[0]
                uuid_v = desc.get("UUID")
                uuid = getattr(uuid_v, "value", uuid_v)
                if uuid and str(uuid).lower().startswith("00002902"):
                    descriptor_parents[path] = parent

        return {
            "device_path": dev_path,
            "device_iface": device,
            "chars_by_uuid": chars_by_uuid,
            "_char_iface_cache": {},
        }

    async def teardown(self, handle: Any) -> None:
        # Disconnect leaves the device cached in BlueZ — fine for the bench.
        try:
            await handle["device_iface"].call_disconnect()
        except Exception:
            pass

    # -- characteristic helpers -------------------------------------------
    async def _char_iface(self, handle: Any, uuid: str):
        cache = handle["_char_iface_cache"]
        norm = uuid.lower()
        if norm in cache:
            return cache[norm]
        path = handle["chars_by_uuid"].get(norm)
        if path is None:
            raise KeyError(f"raw_dbus: characteristic {uuid} not found")
        proxy = await self._proxy(path)
        iface = proxy.get_interface("org.bluez.GattCharacteristic1")
        cache[norm] = iface
        return iface

    async def read(self, handle: Any, char_uuid: str) -> bytes:
        iface = await self._char_iface(handle, char_uuid)
        value = await iface.call_read_value({})
        return bytes(value)

    async def write(self, handle: Any, char_uuid: str, data: bytes) -> None:
        from dbus_fast import Variant
        iface = await self._char_iface(handle, char_uuid)
        await iface.call_write_value(list(data), {"type": Variant("s", "request")})

    async def notify_iter(
        self, handle: Any, char_uuid: str, duration_s: float
    ) -> AsyncIterator[NotifyEvent]:
        bus = await self._bus_handle()
        iface = await self._char_iface(handle, char_uuid)
        path = handle["chars_by_uuid"][char_uuid.lower()]

        queue: asyncio.Queue[NotifyEvent] = asyncio.Queue()

        # The change comes via PropertiesChanged on this char path; the proxy's
        # Properties interface fires it scoped to this object.
        proxy = await self._proxy(path)
        props = proxy.get_interface("org.freedesktop.DBus.Properties")

        def on_props_changed(interface: str, changed: Dict[str, Any], _invalidated):
            if interface != "org.bluez.GattCharacteristic1":
                return
            v = changed.get("Value")
            if v is None:
                return
            value = getattr(v, "value", v)
            queue.put_nowait(NotifyEvent(t_ns=perf_ns(), value=bytes(value)))

        props.on_properties_changed(on_props_changed)
        await iface.call_start_notify()
        try:
            deadline = perf_ns() + int(duration_s * 1_000_000_000)
            while perf_ns() < deadline:
                try:
                    timeout = max(0.001, (deadline - perf_ns()) / 1_000_000_000)
                    yield await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
        finally:
            try:
                await iface.call_stop_notify()
            except Exception:
                pass


def stack(cfg: BenchConfig) -> Stack:
    return RawDbusStack(adapter=cfg.adapter)
