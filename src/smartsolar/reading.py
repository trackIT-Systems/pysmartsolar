"""Readings and events as they are handed to the outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_iso(when: datetime) -> str:
    return when.astimezone().isoformat(sep=" ", timespec="milliseconds")


def field_key(friendly_name: str, unit: str) -> str:
    """The key used in MQTT payloads, e.g. "Battery voltage (V)"."""
    return f"{friendly_name} ({unit})" if unit else friendly_name


def mac12(address: str) -> str:
    """AA:BB:CC:DD:EE:FF -> aabbccddeeff, for topics."""
    return address.replace(":", "").lower()


@dataclass(frozen=True)
class Value:
    name: str
    friendly_name: str
    unit: str
    value: Any  # None: the device reports the value as not available

    @property
    def key(self) -> str:
        return field_key(self.friendly_name, self.unit)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "friendly": self.friendly_name, "unit": self.unit, "value": self.value}


@dataclass(frozen=True)
class Reading:
    """The values of one source ("advert" or "gatt") of one device at one time."""

    device: str
    block: str
    time: datetime
    values: tuple[Value, ...]

    def payload(self) -> dict[str, Any]:
        """Flat MQTT payload: ``_time`` plus one "Friendly name (unit)" entry per value."""
        payload: dict[str, Any] = {"_time": local_iso(self.time), "source": self.block}
        for value in self.values:
            payload[value.key] = value.value
        return payload


@dataclass(frozen=True)
class Event:
    time: datetime
    kind: str
    device: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"_time": local_iso(self.time), "event": self.kind}
        if self.device is not None:
            payload["device"] = self.device
        payload.update(self.data)
        return payload

    def detail(self) -> str:
        """Compact ``k=v`` text for the CSV."""
        return " ".join(f"{k}={v}" for k, v in self.data.items())
