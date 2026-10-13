from pathlib import Path

import pytest

from smartsolar import profiles
from smartsolar.errors import ProfileError
from smartsolar.profiles import Profile
from smartsolar.vcsf import build_vcsf
from smartsolar.vreg import WRITABLE

SAMPLE = Path(__file__).parent / "data" / "ve-mppt-75-15-lifepo4.vcsf"


def write(tmp_path, data, **kw):
    path = tmp_path / "p.vcsf"
    path.write_bytes(build_vcsf(data, product_id=kw.get("product_id", 0xA053), min_firmware=kw.get("min_firmware", 0x163)))
    return path


def test_the_sample_profile_maps_to_the_settings():
    p = profiles.load_profile(SAMPLE)
    assert (p.name, p.product_id, p.min_firmware, p.system_voltage) == ("ve-mppt-75-15-lifepo4", 0xA053, 0x163, 12)
    assert p.values == {
        "battery_type": "user", "battery_max_current": 15.0, "absorption_voltage": 14.5, "float_voltage": 13.5, "equalisation_voltage": 0.0,
        "temperature_compensation": 0.0, "absorption_time_limit": 2.0, "adaptive_mode": "off", "auto_equalise_days": 0,
        "equalisation_current_percent": 0, "equalisation_duration": 2.0, "equalisation_auto_stop": "off", "rebulk_voltage_offset": 0.1,
        "tail_current": 0.0, "low_temperature_level": 5.0, "low_temperature_current": None, "load_output_control": "conventional1",
        "load_user_high_voltage": 14.0, "load_user_low_voltage": 10.0, "load_aes_timer": 30, "rx_port_function": "load-config", "tx_port_function": "communication",
    }
    assert "/Settings/Relay/0/Charger/Mode" in p.ignored and "/Settings/StreetLight/MidPointShift" in p.ignored
    assert not any(k in p.ignored for k in ("/Settings/FloatVoltage", "/Settings/BatteryVoltageSetting"))


def test_every_mapped_setting_that_is_in_the_sample_matches_what_the_device_reported():
    """The values of the sample file equal the raw registers read from a SmartSolar 75/15 configured the same way."""
    from fakes import default_registers

    registers = default_registers()
    p = profiles.load_profile(SAMPLE)
    for name, value in p.values.items():
        assert profiles.same(name, WRITABLE[name].decode(registers[WRITABLE[name].vreg]), value), name


def test_every_setting_with_a_vcsf_key_round_trips():
    for w in WRITABLE.values():
        if not w.vcsf:
            continue
        sample = next(iter(w.values)) if w.values else (w.minimum + w.maximum) / 4
        raw, shown = w.encode(sample)
        assert w.decode(raw) == shown and w.from_vcsf(w.to_vcsf(shown)) == shown, w.name


def test_values_are_converted_between_the_file_and_write(tmp_path):
    path = write(tmp_path, {"/Settings/Load/OperationMode": 5, "/Settings/AdaptiveMode": 1, "/Settings/LowBatteryTempCurrent": 65535, "/Settings/TemperatureCompensation": -16, "/Settings/AutoEqualize": 7})
    p = profiles.load_profile(path)
    assert p.values == {"load_output_control": "user1", "adaptive_mode": "on", "low_temperature_current": None, "temperature_compensation": -16.0, "auto_equalise_days": 7}


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, "no setting that smartsolar knows"),
        ({"/Settings/Load/OperationMode": 9}, "not a known value"),
        ({"/Settings/FloatVoltage": "high"}, "not a number"),
        ({"/Settings/FloatVoltage": 99.0}, "outside"),
    ],
)
def test_load_refuses_bad_files(tmp_path, data, message):
    with pytest.raises(ProfileError, match=message):
        profiles.load_profile(write(tmp_path, data))


def test_null_values_and_unknown_keys_are_not_imported(tmp_path):
    p = profiles.load_profile(write(tmp_path, {"/Settings/FloatVoltage": 13.6, "/Settings/Unknown": 1, "/Settings/Other": None, "/Dc/0/Voltage": 13.0}))
    assert p.values == {"float_voltage": 13.6} and p.ignored == ("/Settings/Unknown",)


def test_dump_and_load_round_trip(tmp_path):
    values = {"float_voltage": 13.7, "load_output_control": "conventional2", "low_temperature_current": None, "battery_type": "preset1", "battery_max_current": 12.5}
    blob = profiles.dump_profile(values, product_id=0xA053, firmware=0x164, system_voltage=24, description="d")
    path = tmp_path / "out.vcsf"
    path.write_bytes(blob)
    p = profiles.load_profile(path)
    assert p.values == values and p.system_voltage == 24 and p.min_firmware == 0x164 and p.description == "d"
    from smartsolar.vcsf import parse_vcsf

    data = parse_vcsf(blob).data
    assert data["/Settings/ChargeCurrentLimit"] == 12.5 and data["/Charger/MaxCurrent"] == 12.5 and data["/Settings/Load/OperationMode"] == 3
    assert data["/Settings/BatteryType"] == 1 and data["/Settings/LowBatteryTempCurrent"] == 0xFFFF


def test_dump_skips_unknown_values():
    from smartsolar.vcsf import parse_vcsf

    blob = profiles.dump_profile({"float_voltage": 13.5, "load_output_control": "unknown (9)", "device_mode": "on", "nope": 1}, product_id=None, firmware=None, system_voltage=None)
    assert list(parse_vcsf(blob).data) == ["/Settings/FloatVoltage"]


def test_same_uses_the_resolution_of_the_register():
    assert profiles.same("float_voltage", 13.5, 13.504) and not profiles.same("float_voltage", 13.5, 13.52)
    assert profiles.same("tail_current", 0.5, 0.52) and not profiles.same("tail_current", 0.5, 0.6)
    assert profiles.same("load_output_control", "Conventional1", "conventional1") and not profiles.same("low_temperature_current", None, 5.0)
    assert profiles.same("low_temperature_current", None, None)


def test_problems():
    p = Profile("p", {"float_voltage": 13.5}, product_id=0xA053, min_firmware=0x163, system_voltage=12)
    assert profiles.problems(p, product_id=0xA053, firmware=0x164, system_voltage=12) == ([], [])
    assert profiles.problems(p, product_id=None, firmware=None, system_voltage=None) == ([], [])  # what the device did not report is not checked
    blockers, warnings = profiles.problems(p, product_id=0xA042, firmware=0x100, system_voltage=24)
    assert len(blockers) == 3 and warnings == []
    blockers, warnings = profiles.problems(p, product_id=0xA042, firmware=0x100, system_voltage=24, ignore_device=True)
    assert len(blockers) == 1 and "24 V" in blockers[0] and len(warnings) == 2
    assert profiles.problems(p, product_id=0xA042, firmware=0x100, system_voltage=12, ignore_device=True)[0] == []
    no_voltage = Profile("p", {"load_output_control": "on"}, system_voltage=12)
    assert profiles.problems(no_voltage, product_id=None, firmware=None, system_voltage=24) == ([], [])  # no voltage setting in it


def current(**changes):
    values = {"battery_type": "user", "float_voltage": 13.5, "absorption_voltage": 14.5, "tail_current": 0.0, "load_output_control": "conventional1"}
    values.update(changes)
    return [{"name": n, "value": v} for n, v in values.items()]


def test_plan_orders_changes_like_the_table_and_puts_the_battery_type_first():
    p = Profile("p", {"load_output_control": "on", "float_voltage": 13.8, "battery_type": "user", "tail_current": 0.5})
    changes, notes = profiles.plan(p, current(battery_type="preset1"))
    assert [c.name for c in changes] == ["battery_type", "float_voltage", "tail_current", "load_output_control"] and notes == []
    assert [(c.old, c.new) for c in changes][:2] == [("preset1", "user"), (13.5, 13.8)]


def test_plan_leaves_out_what_the_preset_defines_and_what_the_device_lacks():
    p = Profile("p", {"battery_type": "preset3", "float_voltage": 13.8, "tail_current": 0.5, "equalisation_duration": 2.0})
    changes, notes = profiles.plan(p, current())
    assert [c.name for c in changes] == ["battery_type", "tail_current"]
    assert any("preset3" in n and "float_voltage" in n for n in notes) and any("equalisation_duration" in n and "does not report" in n for n in notes)


def test_plan_without_differences():
    assert profiles.plan(Profile("p", {"float_voltage": 13.5, "load_output_control": "conventional1"}), current()) == ([], [])
