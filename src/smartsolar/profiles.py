"""Profiles: VictronConnect settings files (``.vcsf``) applied to a SmartSolar.

Reading a file never touches a device. ``plan`` compares it with the live settings and refuses
what does not fit (another product, too old firmware, another system voltage). This module has
no BLE or daemon dependency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartsolar import __version__
from smartsolar.errors import InvalidWriteError, ProfileError
from smartsolar.vcsf import build_vcsf, read_vcsf
from smartsolar.vreg import WRITABLE, build_write

KEY_SYSTEM_VOLTAGE = "/Settings/BatteryVoltageSetting"
KEY_SYSTEM_VOLTAGE_ALT = "/Settings/BatteryVoltage"
KEY_CHARGER_MAX_CURRENT = "/Charger/MaxCurrent"
# Keys of the file that are used differently or never imported: the system voltage is a precondition,
# the rest is not a setting of the device.
NOT_IMPORTED = {KEY_SYSTEM_VOLTAGE, KEY_SYSTEM_VOLTAGE_ALT, "/Settings/SettingsLock/Key"}

VCSF_BY_KEY = {w.vcsf: w for w in WRITABLE.values() if w.vcsf}


@dataclass(frozen=True)
class Profile:
    name: str
    values: dict[str, Any]  # setting name -> value, as `smartsolar write` takes it (None: not set)
    product_id: int | None = None
    min_firmware: int | None = None
    system_voltage: int | None = None
    description: str = ""
    ignored: tuple[str, ...] = ()  # /Settings/ keys of the file that smartsolar can not set
    source: str = ""


@dataclass(frozen=True)
class Change:
    name: str
    unit: str
    old: Any
    new: Any


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    file = read_vcsf(path)
    values: dict[str, Any] = {}
    ignored = []
    for key, raw in file.data.items():
        if not key.startswith("/Settings/") or raw is None or key in NOT_IMPORTED:
            continue
        writable = VCSF_BY_KEY.get(key)
        if writable is None:
            ignored.append(key)
            continue
        try:
            values[writable.name] = writable.from_vcsf(raw)
        except InvalidWriteError as exc:
            raise ProfileError(f"{path}: {key}: {exc}") from None
    if not values:
        raise ProfileError(f"{path}: no setting that smartsolar knows (is this a settings file of a SmartSolar?)")
    try:
        build_write({n: v for n, v in values.items() if v is not None or WRITABLE[n].na_raw is not None})  # validate
    except InvalidWriteError as exc:
        raise ProfileError(f"{path}: {exc}") from None
    voltage = file.data.get(KEY_SYSTEM_VOLTAGE, file.data.get(KEY_SYSTEM_VOLTAGE_ALT))
    return Profile(
        name=path.stem,
        values=values,
        product_id=file.product_id,
        min_firmware=file.min_firmware,
        system_voltage=int(voltage) if isinstance(voltage, (int, float)) and not isinstance(voltage, bool) and voltage else None,
        description=file.description,
        ignored=tuple(sorted(ignored)),
        source=str(path),
    )


def dump_profile(values: Mapping[str, Any], *, product_id: int | None, firmware: int | None, system_voltage: int | None, description: str = "") -> bytes:
    """A settings file with the given settings (experimental: not checked with VictronConnect)."""
    data: dict[str, Any] = {}
    for name, value in values.items():
        writable = WRITABLE.get(name)
        if writable is None or not writable.vcsf or isinstance(value, str) and value.startswith("unknown"):
            continue
        data[writable.vcsf] = writable.to_vcsf(value)
        if writable.vcsf == "/Settings/ChargeCurrentLimit":
            data[KEY_CHARGER_MAX_CURRENT] = value
    if system_voltage:
        data[KEY_SYSTEM_VOLTAGE] = system_voltage
        data[KEY_SYSTEM_VOLTAGE_ALT] = system_voltage
    return build_vcsf(data, product_id=product_id, min_firmware=firmware, description=description, app_version=f"smartsolar {__version__}")


def same(name: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    writable = WRITABLE.get(name)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) <= (writable.scale / 2 if writable else 0.005) + 1e-9
    return str(a).strip().casefold() == str(b).strip().casefold()


def problems(profile: Profile, *, product_id: int | None, firmware: int | None, system_voltage: int | None) -> list[str]:
    """Why this profile must not be loaded on this device (empty: fine)."""
    out = []
    if profile.product_id and product_id and profile.product_id != product_id:
        out.append(f"the profile is for product 0x{profile.product_id:X}, the device is 0x{product_id:X}")
    if profile.min_firmware and firmware and firmware < profile.min_firmware:
        out.append(f"the profile needs firmware 0x{profile.min_firmware:X} or newer, the device has 0x{firmware:X}")
    voltage_settings = [n for n in profile.values if WRITABLE[n].check]
    if profile.system_voltage and system_voltage and profile.system_voltage != system_voltage and voltage_settings:
        out.append(f"the profile is for a {profile.system_voltage} V system, the device runs at {system_voltage} V (it sets {', '.join(voltage_settings)})")
    return out


def plan(profile: Profile, current: Iterable[Mapping[str, Any]]) -> tuple[list[Change], list[str]]:
    """What loading the profile changes (in the order the device needs), and notes about what is left out."""
    now = {v["name"]: v for v in current}
    notes = []
    values = dict(profile.values)
    if "battery_type" in values and values["battery_type"] != "user":
        left = [n for n in values if WRITABLE[n].needs_user_type]
        if left:
            notes.append(f"the battery type is the preset {values['battery_type']}: {', '.join(left)} are part of the preset and are not written")
        values = {n: v for n, v in values.items() if not WRITABLE[n].needs_user_type}
    changes = []
    for name in WRITABLE:  # the order of the table: the battery type comes before the settings that depend on it
        if name not in values:
            continue
        have = now.get(name)
        if have is None:
            notes.append(f"{name}: this device does not report it, left out")
            continue
        if not same(name, have["value"], values[name]):
            changes.append(Change(name, WRITABLE[name].unit, have["value"], values[name]))
    return changes, notes
