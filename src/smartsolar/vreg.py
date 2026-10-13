"""The Victron GATT protocol: record stream, requests and the registers (VREGs) we decode.

Reverse engineered, see the wiki page "Victron SmartSolar BLE readout". The service
``306b0001-b081-4037-83dc-e59fcc3cdfd0`` has three characteristics, which we call 0021
(control, also carries the ``f901`` acknowledgements), 0024 and 0027 (data). Data is a
byte stream of records::

    [kind] [ctx] [CBOR register id] [CBOR value]     kind 0x07, 0x08, 0x09
    [kind] [CBOR item]                               kind 0x02 (handshake answer)

kind 0x08 carries a value (a CBOR byte string), 0x09 an answer with an error code, 0x07 a
status. ``ctx`` 0 is a plain request, 3 a subscription. Requests have the same shape: kind 0x05
(get) or 0x06 (set), ``ctx``, then a CBOR array: ``05 00 81 19 ec65`` gets register 0xEC65,
``06 00 82 18 93 42 10 27`` sets register 0x93 to ``10 27`` (the keepalive). A record may be
split across notifications or writes, and several records may share one notification.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import cbor2

from smartsolar.reading import Value

log = logging.getLogger(__name__)

SERVICE_UUID = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
CHAR_UUIDS = {
    "0021": "306b0002-b081-4037-83dc-e59fcc3cdfd0",
    "0024": "306b0003-b081-4037-83dc-e59fcc3cdfd0",
    "0027": "306b0004-b081-4037-83dc-e59fcc3cdfd0",
}

VREG_KEY = 0xEC65  # the Instant Readout encryption key
VREG_MAC = 0xEC66
VREG_KEEPALIVE = 0x93  # set to 0x2710 by the ping: the device closes the link 10 s after the last one

PING = bytes.fromhex("0600821893421027")

# What VictronConnect sends to get data going (captured with a Bluetooth trace). Only the
# first part is the handshake; the rest asks for the key, MAC and the subscriptions.
INIT_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("0021", "fa80ff"),
    ("0021", "f980"),
    ("0024", "01"),
    ("0024", "0300"),
    ("0024", "0600821893421027" + "05008219ec6619ec65" + "03010303"),
    ("0024", "05008119010d" + "05008119ec7d" + "0500811890" + "05008119ec3f" + "05008119ec12"),
    ("0024", "050181190100" + "050181190100"),
    (
        "0027",
        "05018119ec7d" + "0501811890" + "05018119ec3f" + "05018119ec12" + "050381190102" + "05038119ed8c" + "05038119edec"
        + "050381192211" + "050381192212" + "05038119eda8" + "0600821893421027" + "05038119eda905",
    ),
    ("0024", "038119edac"),
    ("0021", "f941"),
)


def get_request(*vregs: int, ctx: int = 0) -> bytes:
    """``[0x05][ctx][CBOR array of register ids]``: read registers (kind 0x06 would set one)."""
    return bytes([0x05, ctx]) + cbor2.dumps(list(vregs))


@dataclass(frozen=True)
class Record:
    kind: int
    ctx: int | None
    vreg: int | None
    value: Any


class RecordParser:
    """Turns the notification chunks of one characteristic into records."""

    def __init__(self) -> None:
        self._buffer = b""
        self.dropped = 0  # bytes that could not be understood

    def feed(self, chunk: bytes) -> list[Record]:
        data = self._buffer + chunk
        self._buffer = b""
        records: list[Record] = []
        pos = 0
        while pos < len(data):
            record, used = self._one(data, pos)
            if record is None and used == 0:  # incomplete: keep the rest for the next chunk
                self._buffer = data[pos:]
                break
            if record is None:
                self.dropped += used
                log.debug("dropping unparsable bytes %s", data[pos : pos + used].hex())
            else:
                records.append(record)
            pos += used
        return records

    @staticmethod
    def _cbor(stream: io.BytesIO) -> Any:
        return cbor2.CBORDecoder(stream).decode()

    def _one(self, data: bytes, pos: int) -> tuple[Record | None, int]:
        """Parse one record at ``pos``: (record, bytes used); (None, 0) = incomplete; (None, n) = skip n."""
        kind = data[pos]
        if kind == 0x02:
            body = io.BytesIO(data[pos + 1 :])
            try:
                value = self._cbor(body)
            except (cbor2.CBORDecodeEOF, EOFError):
                return None, 0
            except cbor2.CBORDecodeError:
                return None, len(data) - pos
            return Record(kind, None, None, value), 1 + body.tell()
        if kind in (0x07, 0x08, 0x09):
            if pos + 1 >= len(data):
                return None, 0
            ctx = data[pos + 1]
            body = io.BytesIO(data[pos + 2 :])
            try:
                vreg = self._cbor(body)
                value = self._cbor(body)
            except (cbor2.CBORDecodeEOF, EOFError):
                return None, 0
            except cbor2.CBORDecodeError:
                return None, len(data) - pos
            if not isinstance(vreg, int):
                return None, len(data) - pos
            return Record(kind, ctx, vreg, value), 2 + body.tell()
        # An unknown record kind: we cannot know its length, so the rest of the chunk is lost.
        return None, len(data) - pos


# -- registers ------------------------------------------------------------------------


@dataclass(frozen=True)
class Vreg:
    id: int
    name: str
    friendly: str
    unit: str
    decode: Callable[[Any], Any]
    verified: bool = True  # seen with plausible values on a real device


def _bytes(raw: Any) -> bytes:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, int) and raw >= 0:
        return raw.to_bytes(max(1, (raw.bit_length() + 7) // 8), "little")
    raise ValueError(f"unexpected value {raw!r}")


def _unsigned(scale: float = 1.0, na: int | None = None, ndigits: int = 2) -> Callable[[Any], Any]:
    def decode(raw: Any) -> Any:
        number = int.from_bytes(_bytes(raw), "little")
        if na is not None and number == na:
            return None
        return number if scale == 1.0 else round(number * scale, ndigits)

    return decode


def _signed(scale: float = 1.0, ndigits: int = 2) -> Callable[[Any], Any]:
    def decode(raw: Any) -> Any:
        number = int.from_bytes(_bytes(raw), "little", signed=True)
        return number if scale == 1.0 else round(number * scale, ndigits)

    return decode


def _kelvin_to_celsius(raw: Any) -> Any:
    number = int.from_bytes(_bytes(raw), "little")
    return None if number == 0xFFFF else round(number * 0.01 - 273.15, 2)


def _hex(raw: Any) -> str:
    return _bytes(raw).hex()


def _mac(raw: Any) -> str:
    return ":".join(f"{b:02x}" for b in _bytes(raw))


VREGS: dict[int, Vreg] = {
    v.id: v
    for v in (
        Vreg(0xEC66, "mac_address", "MAC address", "", _mac),
        Vreg(0x0102, "firmware_version", "Firmware version", "", _hex),
        Vreg(0xEC5A, "uptime", "Uptime", "s", _unsigned()),
        Vreg(0x0200, "device_mode", "Device mode", "", _unsigned()),
        Vreg(0xED8D, "channel_1_voltage", "Channel 1 voltage", "V", _unsigned(0.01)),
        Vreg(0xED8F, "channel_1_current", "Channel 1 current", "A", _signed(0.1), verified=False),
        Vreg(0xEDA8, "load_output_status", "Load output status", "", _unsigned()),
        Vreg(0xEDA9, "load_output_voltage", "Load output voltage", "V", _unsigned(0.01)),
        Vreg(0xEDAC, "load_output_offset_voltage", "Load output offset voltage", "V", _unsigned(0.01)),
        Vreg(0xEDAD, "load_output_current", "Load output current", "A", _unsigned(0.1), verified=False),
        Vreg(0xEDEC, "battery_temperature", "Battery temperature", "°C", _kelvin_to_celsius),
        Vreg(0x2211, "adjustable_voltage_min", "Adjustable voltage minimum", "V", _unsigned(0.01)),
        Vreg(0x2212, "adjustable_voltage_max", "Adjustable voltage maximum", "V", _unsigned(0.01)),
        Vreg(0xEDBB, "panel_voltage", "Panel voltage", "V", _unsigned(0.01), verified=False),
        Vreg(0xEDBC, "panel_power", "Panel power", "W", _unsigned(0.01), verified=False),
    )
}

# Never turned into a Value: it is a secret.
SECRET_VREGS = {VREG_KEY}


def decode_vreg(vreg: int, raw: Any) -> Value | None:
    """The Value for a register, or None for registers that must not be published."""
    if vreg in SECRET_VREGS:
        return None
    known = VREGS.get(vreg)
    try:
        if known is None:
            text = _hex(raw) if isinstance(raw, (bytes, bytearray)) else raw
            return Value(f"vreg_{vreg:04x}", f"VREG 0x{vreg:04X}", "", text)
        return Value(known.name, known.friendly, known.unit, known.decode(raw))
    except (ValueError, TypeError):
        return None


def vreg_table() -> list[dict[str, Any]]:
    return [
        {"id": f"0x{v.id:04x}", "name": v.name, "friendly": v.friendly, "unit": v.unit, "verified": v.verified}
        for v in sorted(VREGS.values(), key=lambda v: v.id)
    ]
