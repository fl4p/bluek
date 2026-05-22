"""Exceptions mirroring ``bleak.exc`` (the subset batmon-ha relies on)."""

from __future__ import annotations


class BleakError(Exception):
    """Base exception for all bleak(-compatible) errors."""


class BleakDeviceNotFoundError(BleakError):
    """A device with the requested address could not be found."""

    def __init__(self, identifier, *args):
        super().__init__(*args)
        self.identifier = identifier


class BleakCharacteristicNotFoundError(BleakError):
    """A characteristic matching the given specifier was not found."""

    def __init__(self, char_specifier):
        super().__init__(f"Characteristic {char_specifier} was not found!")
        self.char_specifier = char_specifier
