"""The registers (VREGs) read over GATT and how to decode them.

The protocol that carries them is in ``vesmart.py``. Register ids, types and scales are those of
Victron's VE.Direct HEX protocol and of the register dictionary inside VictronConnect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from smartsolar.errors import InvalidWriteError
from smartsolar.reading import Value

log = logging.getLogger(__name__)

SERVICE_UUID = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
CHAR_UUIDS = {
    "0021": "306b0002-b081-4037-83dc-e59fcc3cdfd0",  # control
    "0024": "306b0003-b081-4037-83dc-e59fcc3cdfd0",  # lastData
    "0027": "306b0004-b081-4037-83dc-e59fcc3cdfd0",  # data
}

VREG_KEY = 0xEC65  # the Instant Readout encryption key (instance 0)
VREG_MAC = 0xEC66  # instance 0
VREG_ADVERTISEMENT_MODE = 0xEC7D  # instance 0: 1 = Instant Readout on
# Registers of the Bluetooth part (VE.Smart instance 0), not of the charger: GATT only.
BLUETOOTH_VREGS = (VREG_KEY, VREG_MAC, VREG_ADVERTISEMENT_MODE)


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


def _signed(scale: float = 1.0, na: int | None = None, ndigits: int = 2) -> Callable[[Any], Any]:
    def decode(raw: Any) -> Any:
        number = int.from_bytes(_bytes(raw), "little", signed=True)
        if na is not None and number == na:
            return None
        return number if scale == 1.0 else round(number * scale, ndigits)

    return decode


def _kelvin_to_celsius(raw: Any) -> Any:
    number = int.from_bytes(_bytes(raw), "little")
    return None if number == 0xFFFF else round(number * 0.01 - 273.15, 2)


def _hex(raw: Any) -> str:
    return _bytes(raw).hex()


def _enum(names: dict[int, str]) -> Callable[[Any], Any]:
    def decode(raw: Any) -> Any:
        number = int.from_bytes(_bytes(raw), "little")
        return names.get(number, f"unknown ({number})")

    return decode


DEVICE_MODES = {1: "on", 4: "off", 5: "eco"}
LOAD_CONTROLS = {0: "off", 1: "batterylife", 2: "conventional1", 3: "conventional2", 4: "on", 5: "user1", 6: "user2", 7: "aes"}
LOAD_CONTROL_ALIASES = {"alt1": "conventional1", "alt2": "conventional2", "always-on": "on", "always-off": "off"}


def _state(raw: Any) -> Any:
    from smartsolar.instant import STATES

    return _enum(STATES)(raw)


def _error(raw: Any) -> Any:
    from smartsolar.instant import ERRORS

    return _enum(ERRORS)(raw)


def _tracker(raw: Any) -> Any:
    from smartsolar.instant import TRACKER_MODES

    return _enum(TRACKER_MODES)(raw)


def _off_reason(raw: Any) -> str:
    from smartsolar.instant import off_reasons

    return off_reasons(int.from_bytes(_bytes(raw), "little"))


def _mac(raw: Any) -> str:
    return ":".join(f"{b:02x}" for b in _bytes(raw))


VREGS: dict[int, Vreg] = {
    v.id: v
    for v in (
        Vreg(0xEC66, "mac_address", "MAC address", "", _mac),
        Vreg(0x0102, "firmware_version", "Firmware version", "", _hex),
        Vreg(0x0120, "uptime", "Time since the charger started", "s", _unsigned()),
        Vreg(0x0200, "device_mode", "Device mode", "", _enum(DEVICE_MODES)),
        Vreg(0x0201, "device_state", "Device state", "", _state),
        Vreg(0xEDDA, "charger_error", "Charger error", "", _error),
        Vreg(0xEDD5, "battery_voltage", "Battery voltage", "V", _unsigned(0.01)),
        Vreg(0xEDD7, "charge_current", "Charge current (battery and load output)", "A", _signed(0.1)),
        Vreg(0xEDD3, "yield_today", "Yield today", "kWh", _unsigned(0.01), verified=False),
        Vreg(0xEDD2, "max_power_today", "Maximum power today", "W", _unsigned(), verified=False),
        Vreg(0xEDD1, "yield_yesterday", "Yield yesterday", "kWh", _unsigned(0.01), verified=False),
        Vreg(0xEDD0, "max_power_yesterday", "Maximum power yesterday", "W", _unsigned(), verified=False),
        Vreg(0xEDDC, "yield_total", "Yield total", "kWh", _unsigned(0.01), verified=False),
        Vreg(0xEDDB, "charger_temperature", "Charger temperature", "°C", _signed(0.01), verified=False),
        Vreg(0xEDBB, "panel_voltage", "Panel voltage", "V", _unsigned(0.01)),
        Vreg(0xEDBC, "panel_power", "Panel power", "W", _unsigned(0.01), verified=False),
        Vreg(0xED8D, "channel_1_voltage", "Channel 1 voltage", "V", _signed(0.01)),
        Vreg(0xED8F, "channel_1_current", "Channel 1 current", "A", _signed(0.1), verified=False),
        Vreg(0xEDA8, "load_output_status", "Load output status", "", _unsigned()),
        Vreg(0xEDA9, "load_output_voltage", "Load output voltage", "V", _unsigned(0.01)),
        Vreg(0xEDAC, "load_output_offset_voltage", "Load output offset voltage", "V", _unsigned(0.01)),
        Vreg(0xEDAD, "load_output_current", "Load output current", "A", _unsigned(0.1), verified=False),
        Vreg(0xEDEC, "battery_temperature", "Battery temperature", "°C", _kelvin_to_celsius),
        Vreg(0x2211, "adjustable_voltage_min", "Adjustable voltage minimum", "V", _unsigned(0.01)),
        Vreg(0x2212, "adjustable_voltage_max", "Adjustable voltage maximum", "V", _unsigned(0.01)),
        Vreg(0xEDEF, "battery_voltage_setting", "Battery voltage setting", "V", _unsigned(), verified=False),
    )
}

# Registers that are read at every poll and every hold publish. The device pushes the ones that
# change by itself (panel voltage and power, channel 1 voltage and current, load output, ...).
_POLLED_BASE: tuple[int, ...] = (
    0x0200, 0x0201, 0xEDDA, 0xEDD5, 0xEDD7, 0xED8F, 0xEDD3, 0xEDD2, 0xEDD1, 0xEDD0, 0xEDDC, 0xEDDB,
    0xEDBB, 0xEDBC, 0xEDAD, 0xEDEF, 0x0120,
)

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


def _register_settings() -> None:
    """The writable registers are also readable values: decode them with the same codec."""
    for w in WRITABLE.values():
        if w.vreg not in VREGS:
            VREGS[w.vreg] = Vreg(w.vreg, w.name, w.friendly, w.unit, w.decode, verified=w.verified)


def vreg_table() -> list[dict[str, Any]]:
    """Every published register, a history record with one row per value."""
    rows = [{"id": f"0x{v.id:04x}", "name": v.name, "friendly": v.friendly, "unit": v.unit, "verified": v.verified} for v in VREGS.values()]
    rows += [{"id": f"0x{r.id:04x}", "name": f.name, "friendly": f.friendly, "unit": f.unit, "verified": True} for r in RECORDS.values() for f in r.fields]
    rows += [{"id": f"0x{v.id:04x}", "name": v.name, "friendly": v.friendly, "unit": v.unit, "verified": v.verified} for v in DERIVED.values()]
    return sorted(rows, key=lambda row: row["id"])


# -- settings: registers that can be read, written and imported from a VictronConnect file ------


@dataclass(frozen=True)
class Writable:
    """A setting register that ``smartsolar write`` and ``profile load`` may change.

    Everything else is refused. Register ids, widths and scales follow Victron's "VE.Direct
    protocol, BlueSolar and SmartSolar MPPT chargers" (rev 18); the ones with ``vcsf`` set were
    checked against a SmartSolar 75/15 by reading them and comparing with a VictronConnect
    settings file of the same configuration.
    """

    name: str
    vreg: int
    description: str
    width: int  # bytes, little endian
    unit: str = ""
    values: dict[str, int] | None = None  # named values; None: a number
    aliases: dict[str, str] | None = None  # other spellings of the named values
    scale: float = 1.0  # raw = value / scale
    minimum: float = 0.0
    maximum: float = 0.0
    signed: bool = False
    na_raw: int | None = None  # a raw value that means "not set / use the maximum"
    vcsf: str | None = None  # the key in a VictronConnect settings file
    check: str | None = None  # "voltage" or "tempcomp": validated against the device before writing
    zero_ok: bool = False  # 0 is allowed although it is outside the range (the feature is off)
    needs_user_type: bool = False  # the device only takes it if the battery type is user defined (255)
    mask: int | None = None  # bits of the raw value that belong to this setting
    verified: bool = True  # the scale and meaning were seen on a device (zero values do not count)
    text: bool = False  # a UTF-8 string of up to ``width`` bytes

    @property
    def friendly(self) -> str:
        return self.name.replace("_", " ").capitalize()

    def encode(self, value: Any) -> tuple[bytes, Any]:
        """The bytes to send and the value as it will be read back."""
        if self.text:
            text = "" if value is None else str(value)
            data = text.encode()
            if len(data) > self.width:
                raise InvalidWriteError(f"{self.name}: {len(data)} bytes, at most {self.width} fit")
            return data, text
        if self.values is not None:
            key = str(value).strip().lower()
            key = (self.aliases or {}).get(key, key)
            if key not in self.values:
                raise InvalidWriteError(f"{self.name}: {value!r} is not one of {', '.join(self.values)}")
            return self.values[key].to_bytes(self.width, "little"), key
        if value is None or str(value).strip().lower() in ("none", "max", "n/a"):
            if self.na_raw is None:
                raise InvalidWriteError(f"{self.name}: needs a number")
            return self.na_raw.to_bytes(self.width, "little"), None
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise InvalidWriteError(f"{self.name}: {value!r} is not a number") from None
        if not (self.zero_ok and number == 0) and not self.minimum <= number <= self.maximum:
            raise InvalidWriteError(f"{self.name}: {number:g} is outside {self.minimum:g}..{self.maximum:g} {self.unit}".rstrip())
        raw = round(number / self.scale)
        try:
            data = raw.to_bytes(self.width, "little", signed=self.signed)
        except OverflowError:
            raise InvalidWriteError(f"{self.name}: {number:g} does not fit the register") from None
        return data, self._shown(raw)

    def _shown(self, raw: int) -> Any:
        return raw if self.scale == 1.0 else round(raw * self.scale, 2)

    def decode(self, raw: Any) -> Any:
        if self.text:
            return _bytes(raw).decode("utf-8", "replace").rstrip("\x00")
        number = int.from_bytes(_bytes(raw), "little", signed=self.signed)
        if self.mask is not None:
            number &= self.mask
        if self.values is not None:
            for name, value in self.values.items():
                if value == number:
                    return name
            return f"unknown ({number})"
        if self.na_raw is not None and number == self.na_raw:
            return None
        return self._shown(number)

    def from_vcsf(self, value: Any) -> Any:
        """The value of a VictronConnect settings file entry, in the units of ``write``."""
        if self.values is not None:
            for name, number in self.values.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and number == int(value):
                    return name
            if isinstance(value, bool):
                for name, number in self.values.items():
                    if number == int(value):
                        return name
            raise InvalidWriteError(f"{self.name}: {value!r} is not a known value")
        if self.na_raw is not None and isinstance(value, (int, float)) and value == self.na_raw:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidWriteError(f"{self.name}: {value!r} is not a number")
        return float(value) if self.scale != 1.0 else int(value)

    def to_vcsf(self, value: Any) -> Any:
        if self.values is not None:
            return self.values[value]
        if value is None:
            return self.na_raw
        return value


def _enum_values(names: dict[int, str]) -> dict[str, int]:
    return {name: number for number, name in names.items()}


BATTERY_TYPES = {255: "user", **{n: f"preset{n}" for n in range(1, 9)}}
RX_PORT_FUNCTIONS = {0: "remote-on-off", 1: "load-config", 2: "load-on-off-inverted", 3: "load-on-off-normal"}
TX_PORT_FUNCTIONS = {0: "communication", 1: "pulse-0.01kwh", 2: "pwm-normal", 3: "pwm-inverted", 4: "virtual-load"}
OFF_ON = {0: "off", 1: "on"}

WRITABLE: dict[str, Writable] = {
    w.name: w
    for w in (
        Writable("device_mode", 0x0200, "Charger on or off", 1, values={"on": 1, "off": 4}),
        # battery: the type first, because the device only takes the charge settings when it is "user"
        Writable("battery_type", 0xEDF1, "Battery type: user (own settings) or a factory preset", 1, values=_enum_values(BATTERY_TYPES), vcsf="/Settings/BatteryType"),
        Writable("battery_max_current", 0xEDF0, "Battery maximum charge current", 2, "A", scale=0.1, minimum=0.0, maximum=100.0, vcsf="/Settings/ChargeCurrentLimit"),
        Writable("absorption_voltage", 0xEDF7, "Absorption voltage", 2, "V", scale=0.01, minimum=8.0, maximum=70.0, vcsf="/Settings/AbsorptionVoltage", check="voltage", needs_user_type=True),
        Writable("float_voltage", 0xEDF6, "Float voltage", 2, "V", scale=0.01, minimum=8.0, maximum=70.0, vcsf="/Settings/FloatVoltage", check="voltage", needs_user_type=True),
        Writable("equalisation_voltage", 0xEDF4, "Equalisation voltage (0: not used)", 2, "V", scale=0.01, minimum=8.0, maximum=70.0, zero_ok=True, vcsf="/Settings/EqualizeVoltage", check="voltage", needs_user_type=True),
        Writable("temperature_compensation", 0xEDF2, "Temperature compensation", 2, "mV/K", scale=0.01, minimum=-42.0, maximum=42.0, signed=True, vcsf="/Settings/TemperatureCompensation", check="tempcomp", needs_user_type=True, verified=False),
        Writable("absorption_time_limit", 0xEDFB, "Maximum absorption time", 2, "h", scale=0.01, minimum=0.0, maximum=12.98, vcsf="/Settings/AbsorptionMaxTime"),
        Writable("adaptive_mode", 0xEDFE, "Adaptive absorption time", 1, values=_enum_values(OFF_ON), vcsf="/Settings/AdaptiveMode"),
        Writable("auto_equalise_days", 0xEDFD, "Automatic equalisation every N days (0: off)", 1, "days", minimum=0, maximum=250, vcsf="/Settings/AutoEqualize"),
        Writable("equalisation_current_percent", 0xEDE4, "Equalisation current, percent of the maximum current", 1, "%", minimum=0, maximum=100, vcsf="/Settings/EqualisationCurrentPercentage"),
        Writable("equalisation_duration", 0xEDE3, "Maximum equalisation duration", 2, "h", scale=0.01, minimum=0.0, maximum=12.98, vcsf="/Settings/EqualisationDuration"),
        Writable("equalisation_auto_stop", 0xEDE5, "Stop equalisation when the voltage is reached", 1, values=_enum_values(OFF_ON), vcsf="/Settings/EqualisationAutoStop"),
        Writable("rebulk_voltage_offset", 0xED2E, "Re-bulk voltage offset", 2, "V", scale=0.01, minimum=0.0, maximum=5.0, vcsf="/Settings/RebulkVoltageOffset"),
        Writable("tail_current", 0xEDE7, "Tail current that ends absorption (0: off)", 2, "A", scale=0.1, minimum=0.0, maximum=100.0, vcsf="/Settings/TailCurrent"),
        Writable("low_temperature_level", 0xEDE0, "Low temperature cut-off", 2, "°C", scale=0.01, minimum=-40.0, maximum=50.0, signed=True, vcsf="/Settings/LowBatteryTempLevel"),
        Writable("low_temperature_current", 0xEDE6, "Charge current below the low temperature level (none: the maximum)", 2, "A", scale=0.1, minimum=0.0, maximum=100.0, na_raw=0xFFFF, vcsf="/Settings/LowBatteryTempCurrent"),
        # load output
        Writable(
            "load_output_control",
            0xEDAB,
            "Load output algorithm: off, batterylife, conventional1 (off < 11.1 V, on > 13.1 V), conventional2 (11.8 / 14.0 V), on, user1, user2, aes (voltages for 12 V)",
            1,
            values=_enum_values(LOAD_CONTROLS),
            aliases=LOAD_CONTROL_ALIASES,
            mask=0x0F,
            vcsf="/Settings/Load/OperationMode",
        ),
        Writable("load_user_high_voltage", 0xED9D, "Load output switch-on voltage of the user and AES algorithms", 2, "V", scale=0.01, minimum=0.0, maximum=70.0, vcsf="/Settings/Load/UserHighVoltage"),
        Writable("load_user_low_voltage", 0xED9C, "Load output switch-off voltage of the user and AES algorithms", 2, "V", scale=0.01, minimum=0.0, maximum=70.0, vcsf="/Settings/Load/UserLowVoltage"),
        Writable("load_aes_timer", 0xED90, "AES timer", 2, "min", minimum=0, maximum=65535, vcsf="/Settings/Load/AesTimer"),
        # VE.Direct ports
        Writable("rx_port_function", 0xED98, "VE.Direct RX port function", 1, values=_enum_values(RX_PORT_FUNCTIONS), vcsf="/Settings/RxPortFunction"),
        Writable("tx_port_function", 0xED9E, "VE.Direct TX port function", 1, values=_enum_values(TX_PORT_FUNCTIONS), vcsf="/Settings/TxPortFunction"),
    )
}

# Read next to the settings: what a profile is checked against.
VREG_PRODUCT_ID = 0x0100
VREG_FIRMWARE = 0x0102
VREG_BATTERY_VOLTAGE_ACTUAL = 0xEDEF
VREG_VOLTAGE_RANGE_MIN = 0x2211
VREG_VOLTAGE_RANGE_MAX = 0x2212
VREG_BATTERY_TYPE = 0xEDF1
TEMPERATURE_COMPENSATION_LIMIT_12V = 21.0  # mV/K; the device raises error 119 beyond this
VOLTAGE_RANGE_12V = (8.0, 17.4)  # V; the same, and the fallback if the device does not report a range
PROFILE_CONTEXT_VREGS = (VREG_PRODUCT_ID, VREG_FIRMWARE, VREG_BATTERY_VOLTAGE_ACTUAL, VREG_VOLTAGE_RANGE_MIN, VREG_VOLTAGE_RANGE_MAX)


def product_id(raw: Any) -> int | None:
    """The product id of register 0x0100: byte 0 is an identifier, bytes 1 and 2 the id (little endian), byte 3 flags."""
    data = _bytes(raw)
    return int.from_bytes(data[1:3], "little") if len(data) >= 3 else None


def firmware_version(raw: Any) -> int | None:
    """Register 0x0102 as the number VictronConnect shows (0x0164 is 1.64)."""
    data = _bytes(raw)
    return int.from_bytes(data[1:3], "little") if len(data) >= 3 else None


def check_voltage_limits(item: "WriteItem", writable: Writable, *, system_voltage: int, vrange: tuple[float, float] | None) -> None:
    """Refuse a value the device would turn into error 119 ("settings data invalid").

    The device only accepts charge voltages inside its allowed range, and a temperature
    compensation of at most 21 mV/K for a 12 V system. The error can only be cleared by a
    restore to factory defaults followed by a power cycle.
    """
    value = item.value
    if value is None or not isinstance(value, (int, float)):
        return
    if writable.check == "voltage":
        low, high = vrange or (VOLTAGE_RANGE_12V[0], VOLTAGE_RANGE_12V[1] * system_voltage / 12)
        if writable.zero_ok and value == 0:
            return
        if not low <= value <= high:
            raise InvalidWriteError(f"{item.name}: {value:g} V is outside the {low:g}..{high:g} V this device accepts (it would raise error 119)")
    elif writable.check == "tempcomp":
        limit = TEMPERATURE_COMPENSATION_LIMIT_12V * system_voltage / 12
        if abs(value) > limit:
            raise InvalidWriteError(f"{item.name}: {value:g} mV/K is outside +-{limit:g} for a {system_voltage} V system (it would raise error 119)")


@dataclass(frozen=True)
class WriteItem:
    name: str
    vreg: int
    raw: bytes
    value: Any  # what the register will read as


def build_write(values: dict[str, Any]) -> list[WriteItem]:
    """Validate a write request and turn it into register writes. Nothing is sent here."""
    if not values:
        raise InvalidWriteError("nothing to write")
    items = []
    for name, value in values.items():
        writable = WRITABLE.get(name)
        if writable is None:
            known = any(v.name == name for v in VREGS.values())
            raise InvalidWriteError(f"{name}: {'is read-only' if known else 'unknown register'} (writable: {', '.join(WRITABLE)})")
        raw, shown = writable.encode(value)
        items.append(WriteItem(name, writable.vreg, raw, shown))
    return items


def writable_table() -> list[dict[str, Any]]:
    return [
        {
            "name": w.name,
            "vreg": f"0x{w.vreg:04x}",
            "description": w.description,
            "unit": w.unit,
            "allowed": list(w.values) if w.values else f"text, up to {w.width} bytes" if w.text else ("none or " if w.na_raw is not None else "") + f"{w.minimum:g}..{w.maximum:g}",
            "vcsf": w.vcsf,
        }
        for w in WRITABLE.values()
    ]


_register_settings()


# -- more registers the SmartSolar pushes ------------------------------------------------
# Added after the settings: their columns come after all others, so the positions in the CBOR
# array of the older columns stay the same. Names, types and scales were checked against the
# register dictionary in VictronConnect.

BLE_MODES = {0: "disabled", 1: "enabled"}

for _v in (
    Vreg(0xED8C, "channel_1_current_ma", "Channel 1 current, in mA", "mA", _signed(na=0x7FFFFFFF)),
    Vreg(0xEC8A, "pv_power_trend", "PV power for the trend graphs (1 W)", "W", _unsigned(na=0xFFFF)),
    Vreg(0xEC89, "load_current_trend", "Load output current for the trend graphs (0.1 A)", "A", _unsigned(0.1, na=0xFF)),
    Vreg(0x0202, "remote_control_used", "Remote control flags (bit 1: remote on/off)", "", _unsigned()),
    Vreg(0x0090, "ble_mode", "Bluetooth mode", "", _enum(BLE_MODES)),
    Vreg(0x0150, "ble_capabilities", "Bluetooth capability flags", "", _unsigned()),
    # the same columns as the VE.Direct text block (MPPT, OR)
    Vreg(0xEDB3, "tracker_mode", "Tracker mode", "", _tracker),
    Vreg(0x0207, "off_reason", "Off reason", "", _off_reason),
    # was published as uptime, but counts the seconds of the trend data (VE_REG_TREND_LAST_TIME_REFERENCE)
    Vreg(0xEC5A, "trend_time_reference", "Time reference of the trend data", "s", _unsigned()),
):
    VREGS[_v.id] = _v

# A second value of a register, for the columns the VE.Direct text block has (CS, ERR, LOAD, I).
DERIVED: dict[int, Vreg] = {
    v.id: v
    for v in (
        Vreg(0x0201, "device_state_code", "Device state code", "", _unsigned()),
        Vreg(0xEDDA, "charger_error_code", "Charger error code", "", _unsigned()),
        Vreg(0xEDA8, "load_output", "Load output", "", _enum({0: "off", 1: "on"})),
        Vreg(0xED8F, "battery_current", "Battery current", "A", _signed(0.1)),  # VE.Direct I; 0xEDD7 adds the load
    )
}

# Settings added later, registered here so that their columns come last too.
for _w in (
    Writable("instant_readout", VREG_ADVERTISEMENT_MODE, "Instant Readout: the advertisements carry the encrypted live data (Bluetooth part, GATT only)", 1, values=_enum_values(OFF_ON)),
    Writable("custom_name", 0x010C, "The name VictronConnect shows", 80, text=True),
    Writable("bms_present", 0xEDE8, "A BMS controls charging (the charger sets this itself when it sees one)", 1, values={"absent": 0, "present": 1}),
    Writable("identify", 0x010E, "Identify mode: the device blinks its LEDs (on) until set off", 1, values=_enum_values(OFF_ON)),
):
    WRITABLE[_w.name] = _w
_register_settings()


@dataclass(frozen=True)
class RecordField:
    name: str
    friendly: str
    unit: str
    offset: int  # after the version byte
    width: int  # bytes, little endian; 4 for the error list
    scale: float = 1.0
    min_version: int = 0
    errors: bool = False  # four error codes, published as "e1,e2,e3,e4"


@dataclass(frozen=True)
class HistoryRecord:
    """A history register: a version byte, then fields; one register gives several values.

    Unknown fields are all ones (0xFF, 0xFFFF, ...) and published as null; version 255 is an unknown record.
    The values that other registers already give (the user yield in 0xEDDC, yield and maximum power of
    today in 0xEDD3 and 0xEDD2) are not taken from the record again.
    """

    id: int
    name: str
    fields: tuple[RecordField, ...]

    def decode(self, raw: Any) -> list[Value]:
        data = _bytes(raw)
        if not data or data[0] == 0xFF:
            return []
        version, body = data[0], data[1:]
        values = []
        for f in self.fields:
            if version < f.min_version or len(body) < f.offset + f.width:
                continue
            part = body[f.offset : f.offset + f.width]
            if f.errors:
                value: Any = ",".join(str(b) for b in part)
            else:
                number = int.from_bytes(part, "little")
                value = None if number == (1 << (8 * f.width)) - 1 else (number if f.scale == 1.0 else round(number * f.scale, 2))
            values.append(Value(f.name, f.friendly, f.unit, value))
        return values


RECORDS: dict[int, HistoryRecord] = {
    r.id: r
    for r in (
        # version 0/1: database, 4 errors, user yield, system yield, PV max, battery max, days [, battery min]
        HistoryRecord(0x104F, "history_total", (
            RecordField("errors_total", "Last four error codes, all time", "", 1, 4, errors=True),
            RecordField("yield_system_total", "Yield total, not resettable", "kWh", 9, 4, 0.01),
            RecordField("panel_voltage_max_total", "Maximum panel voltage, all time", "V", 13, 2, 0.01),
            RecordField("battery_voltage_max_total", "Maximum battery voltage, all time", "V", 15, 2, 0.01),
            RecordField("history_days", "Days in the history", "", 17, 1),
            RecordField("battery_voltage_min_total", "Minimum battery voltage, all time", "V", 18, 2, 0.01, min_version=1),
        )),
        # version 0: yield, consumed, battery max/min, database, 4 errors, time in bulk/absorption/float,
        # maximum power, maximum battery current, PV max, day sequence number
        HistoryRecord(0x1050, "history_today", (
            RecordField("consumed_today", "Load output consumption today", "kWh", 4, 4, 0.01),
            RecordField("battery_voltage_max_today", "Maximum battery voltage today", "V", 8, 2, 0.01),
            RecordField("battery_voltage_min_today", "Minimum battery voltage today", "V", 10, 2, 0.01),
            RecordField("errors_today", "Last four error codes today", "", 13, 4, errors=True),
            RecordField("time_bulk_today", "Time in bulk today", "min", 17, 2),
            RecordField("time_absorption_today", "Time in absorption today", "min", 19, 2),
            RecordField("time_float_today", "Time in float today", "min", 21, 2),
            RecordField("battery_current_max_today", "Maximum battery current today", "A", 27, 2, 0.1),
            RecordField("panel_voltage_max_today", "Maximum panel voltage today", "V", 29, 2, 0.01),
            RecordField("day_sequence", "Day sequence number", "", 31, 2),
        )),
    )
}


# The day records, today (0x1050) and the 30 days before (0x1051..0x106E): read on request, not published.
HISTORY_DAY0 = 0x1050
HISTORY_DAYS = 30
HISTORY_DAY = HistoryRecord(HISTORY_DAY0, "history_day", (
    RecordField("yield", "Yield", "kWh", 0, 4, 0.01),
    RecordField("consumed", "Load output consumption", "kWh", 4, 4, 0.01),
    RecordField("battery_voltage_max", "Maximum battery voltage", "V", 8, 2, 0.01),
    RecordField("battery_voltage_min", "Minimum battery voltage", "V", 10, 2, 0.01),
    RecordField("errors", "Last four error codes", "", 13, 4, errors=True),
    RecordField("time_bulk", "Time in bulk", "min", 17, 2),
    RecordField("time_absorption", "Time in absorption", "min", 19, 2),
    RecordField("time_float", "Time in float", "min", 21, 2),
    RecordField("max_power", "Maximum power", "W", 23, 4),
    RecordField("battery_current_max", "Maximum battery current", "A", 27, 2, 0.1),
    RecordField("panel_voltage_max", "Maximum panel voltage", "V", 29, 2, 0.01),
    RecordField("day_sequence", "Day sequence number", "", 31, 2),
))


def decode_values(vreg: int, raw: Any) -> list[Value]:
    """All values of a register: one for a plain register, several for a history record, none for the key."""
    record = RECORDS.get(vreg)
    if record is not None:
        try:
            return record.decode(raw)
        except (ValueError, TypeError):
            return []
    value = decode_vreg(vreg, raw)
    if value is None:
        return []
    derived = DERIVED.get(vreg)
    if derived is None:
        return [value]
    try:
        return [value, Value(derived.name, derived.friendly, derived.unit, derived.decode(raw))]
    except (ValueError, TypeError):
        return [value]


def known_vreg(vreg: int) -> bool:
    return vreg in VREGS or vreg in RECORDS


POLLED_VREGS: tuple[int, ...] = tuple(
    dict.fromkeys(_POLLED_BASE + tuple(w.vreg for w in WRITABLE.values()) + (0xED8C, 0x0202, 0x0090, 0x0150, 0xEDB3, 0x0207, 0xEDA8) + tuple(RECORDS))
)
