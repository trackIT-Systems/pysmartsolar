"""One set of columns for all sources: every value has a column, in one unit."""

from datetime import datetime, timezone
from pathlib import Path

from smartsolar.columns import ALIASES, COLUMNS, DESCRIPTIONS, EVENT_COLUMNS, LEADING, TRAILING, UNITS, _sources, row
from smartsolar.reading import Reading, Value

WHEN = datetime(2026, 10, 13, 10, 3, tzinfo=timezone.utc)
DOC = Path(__file__).resolve().parent.parent / "docs" / "outputs.md"

# The columns of 1.3.0 with their units. From 1.3.0 on, a column is only ever appended: none is removed,
# renamed, moved or changes its unit, so a consumer decodes a CBOR array by position (docs/outputs.md).
COLUMNS_1_3 = (
    ("device_state", ""),
    ("device_state_code", ""),
    ("charger_error", ""),
    ("charger_error_code", ""),
    ("battery_voltage", "V"),
    ("battery_current", "A"),
    ("battery_temperature", "°C"),
    ("panel_voltage", "V"),
    ("pv_power", "W"),
    ("load_current", "A"),
    ("load_output", ""),
    ("load_output_status", ""),
    ("load_output_voltage", "V"),
    ("yield_today", "kWh"),
    ("yield_yesterday", "kWh"),
    ("yield_total", "kWh"),
    ("max_power_today", "W"),
    ("max_power_yesterday", "W"),
    ("charger_temperature", "°C"),
    ("tracker_mode", ""),
    ("off_reason", ""),
    ("device_mode", ""),
    ("day_sequence", ""),
    ("mac_address", ""),
    ("firmware_version", ""),
    ("uptime", "s"),
    ("channel_1_voltage", "V"),
    ("channel_1_current", "A"),
    ("load_output_offset_voltage", "V"),
    ("adjustable_voltage_min", "V"),
    ("adjustable_voltage_max", "V"),
    ("battery_voltage_setting", "V"),
    ("battery_type", ""),
    ("battery_max_current", "A"),
    ("absorption_voltage", "V"),
    ("float_voltage", "V"),
    ("equalisation_voltage", "V"),
    ("temperature_compensation", "mV/K"),
    ("absorption_time_limit", "h"),
    ("adaptive_mode", ""),
    ("auto_equalise_days", "days"),
    ("equalisation_current_percent", "%"),
    ("equalisation_duration", "h"),
    ("equalisation_auto_stop", ""),
    ("rebulk_voltage_offset", "V"),
    ("tail_current", "A"),
    ("low_temperature_level", "°C"),
    ("low_temperature_current", "A"),
    ("load_output_control", ""),
    ("load_user_high_voltage", "V"),
    ("load_user_low_voltage", "V"),
    ("load_aes_timer", "min"),
    ("rx_port_function", ""),
    ("tx_port_function", ""),
    ("channel_1_current_ma", "mA"),
    ("pv_power_trend", "W"),
    ("load_current_trend", "A"),
    ("remote_control_used", ""),
    ("ble_mode", ""),
    ("ble_capabilities", ""),
    ("errors_total", ""),
    ("yield_system_total", "kWh"),
    ("panel_voltage_max_total", "V"),
    ("battery_voltage_max_total", "V"),
    ("history_days", ""),
    ("battery_voltage_min_total", "V"),
    ("consumed_today", "kWh"),
    ("battery_voltage_max_today", "V"),
    ("battery_voltage_min_today", "V"),
    ("errors_today", ""),
    ("time_bulk_today", "min"),
    ("time_absorption_today", "min"),
    ("time_float_today", "min"),
    ("battery_current_max_today", "A"),
    ("panel_voltage_max_today", "V"),
    ("instant_readout", ""),
    ("custom_name", ""),
    ("bms_present", ""),
    ("identify", ""),
    ("charge_current", "A"),
    ("trend_time_reference", "s"),
)


def test_every_source_value_has_a_column_in_its_unit_or_a_conversion():
    from smartsolar.columns import CONVERSIONS

    for source, name, _, unit in _sources():
        column = ALIASES.get(name, name)
        assert column in COLUMNS, (source, name)
        assert unit == UNITS[column] or (unit, UNITS[column]) in CONVERSIONS, (source, name, unit, UNITS[column])


def test_columns_are_unique_and_the_measurements_come_first():
    assert len(COLUMNS) == len(set(COLUMNS))
    assert COLUMNS[: len(LEADING)] == LEADING
    assert set(LEADING) <= set(UNITS)
    assert COLUMNS[-len(TRAILING) :] == TRAILING and COLUMNS[len(COLUMNS) - len(TRAILING) - 1] == "tx_port_function"


def test_columns_of_1_3_keep_their_position_and_unit():
    assert tuple((column, UNITS[column]) for column in COLUMNS[: len(COLUMNS_1_3)]) == COLUMNS_1_3
    assert EVENT_COLUMNS == ("event", "detail")


def test_aliases_and_conversions_are_applied():
    values = (
        Value("panel_power", "Panel power", "W", 41.5),  # GATT's name for pv_power
        Value("load_output_current", "Load output current", "A", 0.3),  # GATT's name for load_current
        Value("yield_today", "Yield today", "Wh", 120),  # the advertisement's unit
        Value("vreg_1234", "VREG 0x1234", "", "ab"),  # unknown: dropped
    )
    published = dict(zip(COLUMNS, row(Reading("F7:48:18:8A:77:9D", "gatt", WHEN, values)), strict=True))
    assert published["pv_power"] == 41.5 and published["load_current"] == 0.3
    assert published["yield_today"] == 0.12 and UNITS["yield_today"] == "kWh"
    assert published["battery_voltage"] is None and "vreg_1234" not in published


def test_docs_list_every_column_with_its_unit():
    text = DOC.read_text(encoding="utf-8")
    for column in COLUMNS:
        assert f"| `{column}` | {UNITS[column]} |" in text, column
        assert DESCRIPTIONS[column]
    for column in EVENT_COLUMNS:
        assert f"`{column}`" in text
