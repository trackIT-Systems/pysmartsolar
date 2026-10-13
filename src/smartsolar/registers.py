"""What every way of writing a register has in common, whether over BLE or over the VE.Direct cable."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from smartsolar.errors import ProtocolError, WriteFailed
from smartsolar.vreg import (
    VREG_BATTERY_TYPE,
    VREG_BATTERY_VOLTAGE_ACTUAL,
    VREG_VOLTAGE_RANGE_MAX,
    VREG_VOLTAGE_RANGE_MIN,
    WRITABLE,
    WriteItem,
    check_voltage_limits,
    decode_vreg,
)

# Changing these over the cable could cut the very link the change is made on.
SERIAL_FORBIDDEN = ("rx_port_function", "tx_port_function")

Reader = Callable[[int], Awaitable[Any]]  # register id -> raw value, raises ProtocolError if it is not available


async def check_write(read: Reader, item: WriteItem, *, via_serial: bool = False) -> None:
    """Refuse what the device would turn into error 119, silently ignore (a charge voltage with a
    preset battery type), or what would cut the link a write is made on."""
    if via_serial and item.name in SERIAL_FORBIDDEN:
        raise WriteFailed(f"{item.name}: not written over the VE.Direct cable, it could cut this connection (use Bluetooth)")
    writable = WRITABLE[item.name]
    if not (writable.needs_user_type or writable.check):
        return
    if writable.needs_user_type:
        battery_type = bytes(await read(VREG_BATTERY_TYPE))
        if battery_type[:1] != b"\xff":
            raise WriteFailed(f"{item.name}: the battery type is a preset ({battery_type[0]}), the device only takes this setting with battery_type=user")
    if writable.check:
        system = 12
        try:
            system = bytes(await read(VREG_BATTERY_VOLTAGE_ACTUAL))[0] or 12
        except ProtocolError:
            pass
        vrange = None
        try:
            low = await read(VREG_VOLTAGE_RANGE_MIN)
            high = await read(VREG_VOLTAGE_RANGE_MAX)
            vrange = (int.from_bytes(low, "little") * 0.01, int.from_bytes(high, "little") * 0.01)
        except ProtocolError:
            pass  # an older firmware does not report it: the 12 V limits scaled by the system voltage
        check_voltage_limits(item, writable, system_voltage=system, vrange=vrange)


def write_result(item: WriteItem, old_raw: Any | None, read_raw: Any) -> dict[str, Any]:
    """One entry of a write answer: what was asked, what was there, what the device reads back."""
    old = decode_vreg(item.vreg, old_raw) if old_raw is not None else None
    new = decode_vreg(item.vreg, read_raw)
    return {
        "name": item.name,
        "requested": item.value,
        "old": old.value if old else None,
        "value": new.value if new else None,
        "ok": bytes(read_raw) == item.raw,
    }
