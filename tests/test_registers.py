"""The checks every write makes, over the cable and GATT, before anything is sent."""

import dataclasses

import pytest
from fakes import run

from smartsolar import registers
from smartsolar.errors import InvalidWriteError, ProtocolError, WriteFailed
from smartsolar.vreg import (
    VREG_BATTERY_TYPE,
    VREG_BATTERY_VOLTAGE_ACTUAL,
    VREG_VOLTAGE_RANGE_MAX,
    VREG_VOLTAGE_RANGE_MIN,
    WRITABLE,
    build_write,
)

USER = b"\xff"


def reader(values):
    """A device with these registers; any other is not available."""
    asked = []

    async def read(vreg):
        asked.append(vreg)
        if vreg not in values:
            raise ProtocolError(f"0x{vreg:04x}: unknown register")
        return values[vreg]

    read.asked = asked
    return read


def check(values, **write):
    [item] = build_write(write)
    run(registers.check_write(reader(values), item))


def test_the_voltage_range_the_device_reports_is_the_limit():
    device = {VREG_BATTERY_TYPE: USER, VREG_BATTERY_VOLTAGE_ACTUAL: b"\x0c", VREG_VOLTAGE_RANGE_MIN: (1000).to_bytes(2, "little"), VREG_VOLTAGE_RANGE_MAX: (1500).to_bytes(2, "little")}
    check(device, float_voltage=14.9)
    with pytest.raises(InvalidWriteError, match="15.1 V is outside the 10..15 V"):
        check(device, float_voltage=15.1)


def test_without_a_reported_range_the_12_v_limits_scale_with_the_system_voltage():
    device = {VREG_BATTERY_TYPE: USER, VREG_BATTERY_VOLTAGE_ACTUAL: b"\x18"}  # an older firmware, 24 V
    check(device, float_voltage=27.6)
    with pytest.raises(InvalidWriteError, match="35 V is outside the 8..34.8 V"):
        check(device, float_voltage=35.0)


def test_without_the_system_voltage_the_limits_of_a_12_v_system_apply():
    device = {VREG_BATTERY_TYPE: USER}
    with pytest.raises(InvalidWriteError, match="27.6 V is outside the 8..17.4 V"):
        check(device, float_voltage=27.6)
    with pytest.raises(InvalidWriteError, match="30 mV/K is outside \\+-21 for a 12 V system"):
        check(device, temperature_compensation=30.0)
    device[VREG_BATTERY_VOLTAGE_ACTUAL] = b"\x00"  # not known yet: 12 V too
    with pytest.raises(InvalidWriteError, match="8..17.4 V"):
        check(device, float_voltage=27.6)


def test_a_charge_voltage_needs_the_user_battery_type():
    with pytest.raises(WriteFailed, match="the battery type is a preset \\(4\\)"):
        check({VREG_BATTERY_TYPE: b"\x04"}, float_voltage=13.5)


def test_a_setting_that_needs_the_user_type_but_no_limit_check(monkeypatch):
    plain = dataclasses.replace(WRITABLE["float_voltage"], check=None)
    monkeypatch.setitem(WRITABLE, "float_voltage", plain)
    [item] = build_write({"float_voltage": 99.0 if plain.maximum >= 99 else plain.maximum})
    read = reader({VREG_BATTERY_TYPE: USER})
    run(registers.check_write(read, item))
    assert read.asked == [VREG_BATTERY_TYPE]  # no voltage registers read


def test_a_setting_without_checks_reads_nothing():
    [item] = build_write({"load_output_control": "on"})
    read = reader({})
    run(registers.check_write(read, item))
    assert read.asked == []


@pytest.mark.parametrize("name", registers.SERIAL_FORBIDDEN)
def test_the_port_functions_are_not_written_over_the_cable(name):
    value = next(iter(WRITABLE[name].values))
    [item] = build_write({name: value})
    with pytest.raises(WriteFailed, match="could cut this connection"):
        run(registers.check_write(reader({}), item, via_serial=True))
    run(registers.check_write(reader({}), item))  # over GATT: fine


def test_bits_outside_a_setting_are_kept_and_an_unread_register_refuses_the_write():
    [item] = build_write({"load_output_control": "on"})
    kept = registers.keep_other_bits(item, b"\x81")  # streetlight on, algorithm 1
    assert kept.raw == bytes([0x80 | item.raw[0]])
    with pytest.raises(WriteFailed, match="cannot read the register first"):
        registers.keep_other_bits(item, None)
    [plain] = build_write({"float_voltage": 13.5})
    assert registers.keep_other_bits(plain, None) is plain


def test_a_limit_check_without_the_user_type(monkeypatch):
    monkeypatch.setitem(WRITABLE, "float_voltage", dataclasses.replace(WRITABLE["float_voltage"], needs_user_type=False))
    [item] = build_write({"float_voltage": 13.5})
    read = reader({VREG_BATTERY_VOLTAGE_ACTUAL: b"\x0c"})
    run(registers.check_write(read, item))
    assert VREG_BATTERY_TYPE not in read.asked


def test_the_write_result_compares_what_the_device_reads_back():
    [item] = build_write({"float_voltage": 13.5})
    assert registers.write_result(item, (1350 - 1).to_bytes(2, "little"), item.raw) == {"name": "float_voltage", "requested": 13.5, "old": 13.49, "value": 13.5, "ok": True}
    assert registers.write_result(item, None, b"\x00\x00")["old"] is None
    assert registers.write_result(item, None, b"\x00\x00")["ok"] is False
