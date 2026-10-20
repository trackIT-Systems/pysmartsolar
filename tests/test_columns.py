"""One set of columns for all sources: every value has a column, in one unit."""

from datetime import datetime, timezone
from pathlib import Path

from smartsolar.columns import ALIASES, COLUMNS, DESCRIPTIONS, EVENT_COLUMNS, LEADING, UNITS, _sources, row
from smartsolar.reading import Reading, Value

WHEN = datetime(2026, 10, 13, 10, 3, tzinfo=timezone.utc)
DOC = Path(__file__).resolve().parent.parent / "docs" / "outputs.md"


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
    text = DOC.read_text()
    for column in COLUMNS:
        assert f"| `{column}` | {UNITS[column]} |" in text, column
        assert DESCRIPTIONS[column]
    for column in EVENT_COLUMNS:
        assert f"`{column}`" in text
