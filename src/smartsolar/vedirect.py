"""The VE.Direct serial protocol: text blocks, HEX frames and what they mean. No I/O.

A VE.Direct port (19200 8N1) pushes a *text block* about once a second::

    \\r\\nPID\\t0xA053\\r\\nFW\\t164\\r\\nV\\t13320\\r\\n ... \\r\\nChecksum\\t<1 byte>

The checksum byte makes the sum of all bytes of the block, from the leading ``\\r\\n`` to the
checksum byte itself, 0 modulo 256. In between, the device answers *HEX frames* (the same
registers as the BLE GATT protocol, see vreg.py), which may start at any point, except at the
checksum byte, and do not count for the text checksum::

    :[command nibble][hex bytes][checksum byte as 2 hex digits]\\n

A request sent while the device writes a text block cuts that block short: the reply is sent
in between and the block starts again, so that one block fails its checksum and is dropped.

The checksum is chosen so that the command nibble plus all bytes plus the checksum is 0x55.
Commands: 7 get, 8 set, A async notification, 4 error, 3 unknown command, 1 done, 5 ping.
Get/set/async carry ``register (u16 LE), flags (u8), value (little endian)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from smartsolar.instant import ERRORS, STATES

log = logging.getLogger(__name__)

HEX_SUM = 0x55
MAX_LINE = 200  # a text line is much shorter; longer means we lost the framing
MAX_HEX = 200

CMD_PING = 0x1
CMD_GET = 0x7
CMD_SET = 0x8
CMD_ASYNC = 0xA
CMD_ERROR = 0x4
CMD_UNKNOWN = 0x3

FLAG_UNKNOWN_ID = 0x01
FLAG_NOT_SUPPORTED = 0x02
FLAG_PARAMETER_ERROR = 0x04


# -- HEX frames -----------------------------------------------------------------------


def hex_frame(command: int, payload: bytes = b"") -> bytes:
    checksum = (HEX_SUM - command - sum(payload)) & 0xFF
    return b":" + f"{command:X}".encode() + payload.hex().upper().encode() + f"{checksum:02X}".encode() + b"\n"


def get_frame(vreg: int) -> bytes:
    return hex_frame(CMD_GET, vreg.to_bytes(2, "little") + b"\x00")


def set_frame(vreg: int, value: bytes) -> bytes:
    return hex_frame(CMD_SET, vreg.to_bytes(2, "little") + b"\x00" + value)


def ping_frame() -> bytes:
    return hex_frame(CMD_PING)


@dataclass(frozen=True)
class HexFrame:
    command: int
    payload: bytes  # without the checksum

    @property
    def is_register(self) -> bool:
        return self.command in (CMD_GET, CMD_SET, CMD_ASYNC) and len(self.payload) >= 3

    @property
    def vreg(self) -> int | None:
        return int.from_bytes(self.payload[:2], "little") if self.is_register else None

    @property
    def flags(self) -> int:
        return self.payload[2] if self.is_register else 0

    @property
    def value(self) -> bytes:
        return self.payload[3:] if self.is_register else b""


def parse_hex_frame(body: bytes) -> HexFrame | None:
    """The frame between ``:`` and the newline, or None if it is damaged."""
    try:
        text = body.decode("ascii")
        command = int(text[0], 16)
        data = bytes.fromhex(text[1:])
    except (ValueError, IndexError):
        return None
    if not data or (command + sum(data)) & 0xFF != HEX_SUM:
        return None
    return HexFrame(command, data[:-1])


# -- the stream parser ----------------------------------------------------------------


@dataclass(frozen=True)
class TextBlock:
    fields: dict[str, str]


@dataclass
class ParserStats:
    blocks: int = 0
    bad_checksum: int = 0
    resyncs: int = 0
    hex_frames: int = 0
    bad_hex: int = 0


_TEXT, _CHECKSUM, _HEX = range(3)


class StreamParser:
    """Bytes in, ``TextBlock`` and ``HexFrame`` out. Resynchronises after garbage."""

    def __init__(self) -> None:
        self.stats = ParserStats()
        self._state = _TEXT
        self._synced = False  # the first block boundary has not been seen yet
        self._line = bytearray()
        self._fields: dict[str, str] = {}
        self._sum = 0
        self._hex = bytearray()

    def feed(self, data: bytes) -> list[TextBlock | HexFrame]:
        out: list[TextBlock | HexFrame] = []
        for byte in data:
            if byte == 0 and self._state != _CHECKSUM:
                continue  # the device pads its output with NUL bytes, inside text lines and HEX frames too; they add 0 to the sum
            if self._state == _HEX:
                self._hex_byte(byte, out)
            elif self._state == _CHECKSUM:
                self._checksum_byte(byte, out)
            elif byte == 0x3A:  # ':' starts a HEX frame, wherever we are in a text block
                self._state = _HEX
                self._hex = bytearray()
            else:
                self._text_byte(byte)
        return out

    def _hex_byte(self, byte: int, out: list[TextBlock | HexFrame]) -> None:
        if byte == 0x0A:
            self._state = _TEXT
            frame = parse_hex_frame(bytes(self._hex))
            if frame is None:
                self.stats.bad_hex += 1
            else:
                self.stats.hex_frames += 1
                out.append(frame)
        elif byte == 0x3A:  # a new frame starts: the old one was cut off
            self.stats.bad_hex += 1
            self._hex = bytearray()
        elif len(self._hex) >= MAX_HEX:
            self.stats.bad_hex += 1
            self._state = _TEXT
        else:
            self._hex.append(byte)

    def _text_byte(self, byte: int) -> None:
        self._sum += byte
        self._line.append(byte)
        if byte == 0x0A:
            self._finish_line()
        elif byte == 0x09 and bytes(self._line).strip(b"\r\n") == b"Checksum\t":
            self._state = _CHECKSUM
        elif len(self._line) > MAX_LINE:
            self._lose_sync()

    def _finish_line(self) -> None:
        line = bytes(self._line).strip(b"\r\n")
        self._line = bytearray()
        name, tab, value = line.partition(b"\t")
        if not tab or not name:
            return  # an empty or damaged line; the checksum decides about the block
        self._fields[name.decode("latin1")] = value.decode("latin1").strip()

    def _checksum_byte(self, byte: int, out: list[TextBlock | HexFrame]) -> None:
        self._sum += byte
        if self._synced:
            if self._sum & 0xFF == 0:
                self.stats.blocks += 1
                out.append(TextBlock(self._fields))
            else:
                self.stats.bad_checksum += 1
        self._synced = True
        self._begin_block()

    def _begin_block(self) -> None:
        self._state = _TEXT
        self._sum = 0
        self._line = bytearray()
        self._fields = {}

    def _lose_sync(self) -> None:
        self.stats.resyncs += 1
        self._synced = False
        self._begin_block()


# -- the meaning of the text fields ---------------------------------------------------


@dataclass(frozen=True)
class TextField:
    key: str
    name: str
    friendly: str
    unit: str
    convert: Any  # str -> value


def _milli(value: str) -> float | None:
    return None if value == "---" else round(int(value) * 0.001, 3)


def _centi(value: str) -> float:
    return round(int(value) * 0.01, 2)


def _int(value: str) -> int:
    return int(value)


def _state(value: str) -> str:
    return STATES.get(int(value), f"unknown ({value})")


def _error(value: str) -> str:
    return ERRORS.get(int(value), f"unknown ({value})")


TRACKER_MODES = {0: "off", 1: "voltage or current limited", 2: "mpp tracker active"}
OFF_REASONS = {
    0x00000001: "no input power",
    0x00000002: "switched off (power switch)",
    0x00000004: "switched off (device mode register)",
    0x00000008: "remote input",
    0x00000010: "protection active",
    0x00000020: "paygo",
    0x00000040: "bms",
    0x00000080: "engine shutdown detection",
    0x00000100: "analysing input voltage",
}


def _tracker(value: str) -> str:
    return TRACKER_MODES.get(int(value), f"unknown ({value})")


def _off_reason(value: str) -> str:
    number = int(value, 0)
    return ", ".join(name for bit, name in OFF_REASONS.items() if number & bit) or "none"


def _load(value: str) -> str:
    return value.strip().lower()


TEXT_FIELDS: tuple[TextField, ...] = (
    TextField("CS", "device_state", "Device state", "", _state),
    TextField("CS", "device_state_code", "Device state code", "", _int),
    TextField("ERR", "charger_error", "Charger error", "", _error),
    TextField("ERR", "charger_error_code", "Charger error code", "", _int),
    TextField("MPPT", "tracker_mode", "Tracker mode", "", _tracker),
    TextField("OR", "off_reason", "Off reason", "", _off_reason),
    TextField("V", "battery_voltage", "Battery voltage", "V", _milli),
    TextField("I", "battery_current", "Battery current", "A", _milli),
    TextField("VPV", "panel_voltage", "Panel voltage", "V", _milli),
    TextField("PPV", "pv_power", "PV power", "W", _int),
    TextField("IL", "load_current", "Load current", "A", _milli),
    TextField("LOAD", "load_output", "Load output", "", _load),
    TextField("H19", "yield_total", "Yield total", "kWh", _centi),
    TextField("H20", "yield_today", "Yield today", "kWh", _centi),
    TextField("H21", "max_power_today", "Maximum power today", "W", _int),
    TextField("H22", "yield_yesterday", "Yield yesterday", "kWh", _centi),
    TextField("H23", "max_power_yesterday", "Maximum power yesterday", "W", _int),
    TextField("HSDS", "day_sequence", "Day sequence number", "", _int),
)

# Fields that identify the device and do not change; they are not part of the readings.
IDENTITY_KEYS = ("PID", "FW", "SER#")


@dataclass
class TextReading:
    values: dict[str, Any] = field(default_factory=dict)  # name -> value, in the order of TEXT_FIELDS
    serial: str | None = None
    product_id: int | None = None
    firmware: int | None = None  # 0x164 for firmware 1.64
    unknown: dict[str, str] = field(default_factory=dict)  # fields we have no meaning for


def decode_text(fields: dict[str, str]) -> TextReading:
    """The values of a text block. A field that cannot be converted is left out, not guessed."""
    out = TextReading()
    out.serial = fields.get("SER#") or None
    try:
        out.product_id = int(fields["PID"], 0) if "PID" in fields else None
    except ValueError:
        pass
    try:
        out.firmware = int(fields["FW"], 16) if "FW" in fields else None  # "164" is 1.64
    except ValueError:
        pass
    known = {f.key for f in TEXT_FIELDS} | set(IDENTITY_KEYS)
    for f in TEXT_FIELDS:
        raw = fields.get(f.key)
        if raw is None:
            continue
        try:
            out.values[f.name] = f.convert(raw)
        except (ValueError, TypeError):
            log.debug("Cannot convert the VE.Direct field %s=%r", f.key, raw)
    out.unknown = {k: v for k, v in fields.items() if k not in known and k != "Checksum"}
    return out
