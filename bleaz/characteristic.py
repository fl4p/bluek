"""bleak-compatible GATT service/characteristic/descriptor wrappers.

Each wraps a :mod:`bleaz._att` GATT object and exposes the attributes batmon-ha
reads: service ``.uuid``/``.characteristics``; characteristic ``.uuid``/
``.handle``/``.properties``/``.descriptors``; descriptor ``.handle``/``.uuid``.
The wrappers carry the ATT value handle so :class:`bleaz.client.BleakClient` can
read/write/subscribe.
"""

from __future__ import annotations

from typing import List, Optional

from . import _att
from .uuids import normalize_uuid_str


class BleakGATTDescriptor:
    def __init__(self, desc: "_att.GattDescriptor", characteristic_uuid: str):
        self.obj = desc
        self.handle: int = desc.handle
        self.uuid: str = desc.uuid
        self.characteristic_uuid = characteristic_uuid

    def __str__(self):
        return f"{self.handle}: {self.uuid}"


class BleakGATTCharacteristic:
    def __init__(self, char: "_att.GattCharacteristic", service_uuid: str):
        self.obj = char
        self.handle: int = char.handle
        self.value_handle: int = char.value_handle
        self.uuid: str = char.uuid
        self.properties: List[str] = _att.properties_to_strings(char.properties)
        self.service_uuid = service_uuid
        self.descriptors: List[BleakGATTDescriptor] = [
            BleakGATTDescriptor(d, self.uuid) for d in char.descriptors
        ]

    @property
    def description(self) -> str:
        return self.uuid

    def get_descriptor(self, specifier) -> Optional[BleakGATTDescriptor]:
        for d in self.descriptors:
            if d.handle == specifier or d.uuid == specifier:
                return d
        return None

    def __hash__(self):
        return hash(self.handle)

    def __eq__(self, other):
        return isinstance(other, BleakGATTCharacteristic) and other.handle == self.handle

    def __str__(self):
        return f"{self.handle}: {self.uuid} ({','.join(self.properties)})"


class BleakGATTService:
    def __init__(self, svc: "_att.GattService"):
        self.obj = svc
        self.handle: int = svc.handle
        self.uuid: str = svc.uuid
        self.characteristics: List[BleakGATTCharacteristic] = [
            BleakGATTCharacteristic(c, self.uuid) for c in svc.characteristics
        ]

    def __str__(self):
        return f"{self.handle}: {self.uuid}"


class BleakGATTServiceCollection:
    """Iterable collection mirroring ``BleakClient.services``.

    Truthy only once services have been discovered (batmon-ha relies on
    ``if client.services:`` to know whether discovery has happened).
    """

    def __init__(self, services: List[BleakGATTService]):
        self._services = services
        self._by_char_handle = {}
        self._by_char_uuid = {}
        self._by_desc_handle = {}
        for service in services:
            for char in service.characteristics:
                self._by_char_handle[char.handle] = char
                self._by_char_uuid.setdefault(char.uuid, char)
                for desc in char.descriptors:
                    self._by_desc_handle[desc.handle] = desc

    def __iter__(self):
        return iter(self._services)

    def __len__(self):
        return len(self._services)

    @property
    def services(self) -> List[BleakGATTService]:
        return self._services

    @property
    def characteristics(self):
        return dict(self._by_char_handle)

    def get_service(self, specifier) -> Optional[BleakGATTService]:
        if isinstance(specifier, int):
            return next((s for s in self._services if s.handle == specifier), None)
        uuid = normalize_uuid_str(str(specifier))
        return next((s for s in self._services if s.uuid == uuid), None)

    def get_characteristic(self, specifier) -> Optional[BleakGATTCharacteristic]:
        if isinstance(specifier, int):
            return self._by_char_handle.get(specifier)
        return self._by_char_uuid.get(normalize_uuid_str(str(specifier)))

    def get_descriptor(self, handle: int) -> Optional[BleakGATTDescriptor]:
        return self._by_desc_handle.get(handle)
