import pytest

from fakes import TEST_KEY
from smartsolar.errors import InvalidWriteError
from smartsolar.vreg import VREG_KEY, WRITABLE, build_write, decode_values, decode_vreg, vreg_table, writable_table


def test_decode_registers():
    assert decode_vreg(0xED8D, b"\x33\x05").value == 13.31
    assert decode_vreg(0xEC5A, (55501134).to_bytes(4, "little")).value == 55501134
    assert decode_vreg(0xEC66, bytes.fromhex("f748188a779d")).value == "f7:48:18:8a:77:9d"
    assert decode_vreg(0x0102, bytes.fromhex("00640100")).value == "00640100"
    assert decode_vreg(0x2211, bytes.fromhex("2003")).value == 8.0 and decode_vreg(0x2212, bytes.fromhex("cc06")).value == 17.4


def test_temperature_not_available_and_value():
    assert decode_vreg(0xEDEC, bytes.fromhex("ffff")).value is None
    assert decode_vreg(0xEDEC, (29815).to_bytes(2, "little")).value == 25.0  # 298.15 K


def test_the_key_is_never_decoded_into_a_value():
    assert decode_vreg(VREG_KEY, TEST_KEY) is None


def test_unknown_registers_are_kept_raw():
    value = decode_vreg(0xEC8B, bytes.fromhex("d8ffffff"))
    assert (value.name, value.value) == ("vreg_ec8b", "d8ffffff")
    assert decode_vreg(0xBEEF, 5).value == 5


def test_values_that_cannot_be_decoded_are_skipped():
    assert decode_vreg(0xED8D, "text") is None


def test_vreg_table():
    rows = vreg_table()
    assert rows == sorted(rows, key=lambda r: r["id"]) and all(set(r) == {"id", "name", "friendly", "unit", "verified"} for r in rows)
    assert not any(r["id"] == "0xec65" for r in rows)


def test_build_write_encodes_like_the_captured_writes():
    (item,) = build_write({"load_output_control": "conventional1"})
    assert (item.vreg, item.raw, item.value) == (0xEDAB, b"\x02", "conventional1")
    assert build_write({"load_output_control": "alt1"})[0].value == "conventional1"  # the old name
    assert [build_write({"load_output_control": m})[0].raw[0] for m in ("off", "batterylife", "conventional1", "conventional2", "on", "user1", "user2", "aes")] == list(range(8))
    assert build_write({"load_output_control": " ON "})[0].raw == b"\x04" and build_write({"device_mode": "off"})[0].raw == b"\x04"
    float_item = build_write({"float_voltage": "13.51"})[0]
    assert float_item.raw == bytes.fromhex("4705") and float_item.value == 13.51  # 1351 = 0x0547
    assert build_write({"battery_max_current": 15})[0].raw == bytes.fromhex("9600")


def test_build_write_keeps_the_order_and_handles_several_registers():
    items = build_write({"device_mode": "on", "float_voltage": 13.5})
    assert [i.name for i in items] == ["device_mode", "float_voltage"]


@pytest.mark.parametrize(
    "values, message",
    [
        ({}, "nothing to write"),
        ({"device_mode": "sleep"}, "is not one of on, off"),
        ({"load_output_control": "auto"}, "is not one of off, batterylife, conventional1"),
        ({"battery_voltage": 12}, "read-only"),
        ({"key": "x"}, "unknown register"),
        ({"vreg_ec65": "00"}, "unknown register"),
        ({"float_voltage": "high"}, "not a number"),
        ({"float_voltage": 4.9}, "outside"),
        ({"float_voltage": 70.5}, "outside"),
        ({"battery_max_current": -1}, "outside"),
    ],
)
def test_build_write_refuses(values, message):
    with pytest.raises(InvalidWriteError, match=message):
        build_write(values)


def test_the_key_and_the_keepalive_cannot_be_written():
    assert not {0xEC65, 0x93, 0x0001}.intersection(w.vreg for w in WRITABLE.values())
    assert {w["name"] for w in writable_table()} == set(WRITABLE)


def test_the_new_registers_decode_with_the_values_seen_on_the_device():
    assert decode_vreg(0x0200, b"\x01").value == "on" and decode_vreg(0x0200, b"\x04").value == "off"
    assert decode_vreg(0x0201, b"\x00").value == "off" and decode_vreg(0x0201, b"\x03").value == "bulk"
    assert decode_vreg(0xEDDA, b"\x00").value == "none" and decode_vreg(0xEDDA, b"\x02").value == "battery voltage too high"
    assert decode_vreg(0xEDD5, bytes.fromhex("3305")).value == 13.31
    assert decode_vreg(0xEDBB, bytes.fromhex("0300")).value == 0.03
    assert decode_vreg(0xEDAB, b"\x02").value == "conventional1" and decode_vreg(0xEDAB, b"\x00").value == "off" and decode_vreg(0xEDAB, b"\x09").value == "unknown (9)"
    assert decode_vreg(0xEDF6, bytes.fromhex("4605")).value == 13.5 and decode_vreg(0xEDF7, bytes.fromhex("aa05")).value == 14.5
    assert decode_vreg(0xEDF0, bytes.fromhex("9600")).value == 15.0
    assert decode_vreg(0xEDDB, bytes.fromhex("400b")).value == 28.8
    assert decode_vreg(0xEDDC, bytes.fromhex("77260000")).value == 98.47


def test_registers_pushed_by_the_smartsolar():
    assert decode_vreg(0xED8C, bytes.fromhex("d8ffffff")).value == -40  # mA
    assert decode_vreg(0xED8C, bytes.fromhex("ffffff7f")).value is None
    assert decode_vreg(0xEC8A, bytes.fromhex("0b00")).value == 11 and decode_vreg(0xEC89, b"\x09").value == 0.9
    assert decode_vreg(0x0090, b"\x01").value == "enabled" and decode_vreg(0x0202, bytes.fromhex("02000000")).value == 2


def test_history_records_as_the_smartsolar_sent_them():
    total = {v.name: v.value for v in decode_values(0x104F, bytes.fromhex("0100000000006022000060220000d412c8051e7300" + "ff" * 13))}
    assert total == {
        "errors_total": "0,0,0,0", "yield_system_total": 88.0, "panel_voltage_max_total": 48.2,
        "battery_voltage_max_total": 14.8, "history_days": 30, "battery_voltage_min_total": 1.15,
    }
    today = {v.name: v.value for v in decode_values(0x1050, bytes.fromhex("000300000012000000220513050000000000ea0000000000130000000e00040c2101"))}
    assert today["consumed_today"] == 0.18 and today["time_bulk_today"] == 234 and today["day_sequence"] == 289
    assert today["battery_current_max_today"] == 1.4 and today["panel_voltage_max_today"] == 30.76
    assert "yield_today" not in today and "max_power_today" not in today  # 0xEDD3 and 0xEDD2 give them
    unknown = {v.name: v.value for v in decode_values(0x104F, bytes.fromhex("00" + "ff" * 33))}
    assert unknown["yield_system_total"] is None and "battery_voltage_min_total" not in unknown  # version 0
    assert decode_values(0x1050, bytes.fromhex("ff" * 34)) == []  # version 255: unknown record
