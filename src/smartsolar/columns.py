"""The fixed columns of a reading, as CSV, JSON and CBOR carry them.

The three sources (advertisements, GATT, VE.Direct) report overlapping sets of
values, sometimes under another name or in another unit. A reading is published
without saying which source it came from, so all sources share one set of
columns:

* every value name of the three source tables, once;
* names that mean the same value map to one column (``ALIASES``);
* each column has one unit; a value in another unit is converted (``CONVERSIONS``).

Measurements come first (``LEADING``), then the remaining values in table order.
A reading lacking a value has null in its column.
"""

from __future__ import annotations

import logging
from typing import Any

from smartsolar.reading import Reading
from smartsolar.tracker import ADVERT_FIELDS
from smartsolar.vedirect import TEXT_FIELDS
from smartsolar.vreg import VREGS

log = logging.getLogger(__name__)

EVENT_COLUMNS = ("event", "detail")

# Source name -> column, where a source calls a value differently
ALIASES = {
    "panel_power": "pv_power",  # GATT
    "load_output_current": "load_current",  # GATT
}

# (unit of the source, unit of the column) -> factor
CONVERSIONS = {("Wh", "kWh"): 0.001}

# Units of columns whose sources disagree (the advertisement reports yield_today in Wh)
COLUMN_UNITS = {"yield_today": "kWh"}

# The values most consumers want, first and in this order
LEADING = (
    "device_state",
    "device_state_code",
    "charger_error",
    "charger_error_code",
    "battery_voltage",
    "battery_current",
    "battery_temperature",
    "panel_voltage",
    "pv_power",
    "load_current",
    "load_output",
    "load_output_status",
    "load_output_voltage",
    "yield_today",
    "yield_yesterday",
    "yield_total",
    "max_power_today",
    "max_power_yesterday",
    "charger_temperature",
    "tracker_mode",
    "off_reason",
    "device_mode",
)


def _sources() -> list[tuple[str, str, str, str]]:
    """(source, name, description, unit) of every value of every source table."""
    sources = [("advert", name, friendly, unit) for name, friendly, unit in ADVERT_FIELDS]
    sources += [("vedirect", f.name, f.friendly, f.unit) for f in TEXT_FIELDS]
    sources += [("gatt", v.name, v.friendly, v.unit) for v in VREGS.values()]
    return sources


def _columns() -> tuple[tuple[str, ...], dict[str, str], dict[str, str], dict[str, set[str]]]:
    names: list[str] = []
    units: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    origins: dict[str, set[str]] = {}
    for source, name, friendly, unit in _sources():
        column = ALIASES.get(name, name)
        if column not in names:
            names.append(column)
        units.setdefault(column, COLUMN_UNITS.get(column, unit))
        descriptions.setdefault(column, friendly)
        origins.setdefault(column, set()).add(source)
    ordered = tuple(LEADING) + tuple(n for n in names if n not in LEADING)
    return ordered, units, descriptions, origins


# Column names, their unit, a description, and which sources provide them
COLUMNS, UNITS, DESCRIPTIONS, ORIGINS = _columns()

_dropped: set[str] = set()


def _convert(name: str, value: Any, unit: str, column: str) -> Any:
    target = UNITS[column]
    if unit == target or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    factor = CONVERSIONS.get((unit, target))
    if factor is None:
        log.debug("no conversion from %s to %s for %s", unit, target, name)
        return value
    return round(value * factor, 6)


def row(reading: Reading) -> tuple[Any, ...]:
    """The reading's values in column order, converted to the column units; None for a value it lacks."""
    values: dict[str, Any] = {}
    for v in reading.values:
        column = ALIASES.get(v.name, v.name)
        if column not in UNITS:
            if v.name not in _dropped:
                _dropped.add(v.name)
                log.debug("value %s has no column, not published", v.name)
            continue
        values[column] = _convert(v.name, v.value, v.unit, column)
    return tuple(values.get(column) for column in COLUMNS)
