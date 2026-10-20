"""Readings and events as they are handed to the outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from smartsolar.clock import boottime


def utc_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_iso(when: datetime) -> str:
    return when.astimezone().isoformat(sep=" ", timespec="milliseconds")


def mac12(address: str) -> str:
    """AA:BB:CC:DD:EE:FF -> aabbccddeeff, for topics."""
    return address.replace(":", "").lower()


SERIAL_RE = re.compile(r"\b[A-Z]{2}\d{4}[A-Z0-9]{5}\b")


def serial_from_name(name: str | None) -> str | None:
    """The serial number in a BLE name such as "SmartSolar HQ22294ZGEU", if there is one."""
    match = SERIAL_RE.search(name or "")
    return match.group(0) if match else None


def device_id(address: str, name: str | None = None) -> str:
    """How a device is called in readings, topics and the CSV: its serial number, else its MAC address."""
    return serial_from_name(name) or address


def topic_id(device: str) -> str:
    """A device id as a topic level: serial numbers as they are, MAC addresses as 12 hex digits."""
    return mac12(device) if ":" in device else device


@dataclass(frozen=True)
class Value:
    name: str
    friendly_name: str
    unit: str
    value: Any  # None: the device reports the value as not available

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "friendly": self.friendly_name, "unit": self.unit, "value": self.value}


@dataclass(frozen=True)
class Reading:
    """The values of one source ("advert", "gatt" or "vedirect") of one device at one time.

    ``device`` is the serial number, or the MAC address when no serial is known (see ``device_id``).
    """

    device: str
    block: str
    time: datetime
    values: tuple[Value, ...]
    # CLOCK_BOOTTIME when the reading was taken, to re-time it if the clock was not synced yet
    boottime: float = field(default_factory=boottime)


@dataclass(frozen=True)
class Event:
    time: datetime
    kind: str
    device: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    boottime: float = field(default_factory=boottime)

    def detail(self) -> str:
        """Compact ``k=v`` text for the CSV."""
        return " ".join(f"{k}={v}" for k, v in self.data.items())
