"""Readings taken before the clock is trusted only go over MQTT; the files start with the sync."""

import logging
from datetime import datetime

from fakes import FakeClock
from smartsolar.columns import COLUMNS
from smartsolar.outputs.csv import CsvOutput
from smartsolar.reading import Reading, Value

MAC = "F7:48:18:8A:77:9D"
# Position of battery_voltage in a file row (after time, device, block)
VOLTAGE = 3 + COLUMNS.index("battery_voltage")


def local(*args):
    return datetime(*args).astimezone()


# A wrong clock before the sync (the image's build date, say)
WRONG = local(2020, 1, 1).timestamp()


def reading(clock, voltage=24.0):
    """A reading stamped like the sources stamp it, with the fake clock's time."""
    value = Value("battery_voltage", "Battery voltage", "V", voltage)
    return Reading(MAC, "vedirect", datetime.fromtimestamp(clock.time()).astimezone(), (value,))


def files_dir(tmp_path):
    return tmp_path / "tracker" / "smartsolar"


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_readings_without_a_trusted_clock_are_not_written(tmp_path):
    clock = FakeClock(WRONG)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock))
    assert list(files_dir(tmp_path).iterdir()) == []


def test_readings_are_written_from_the_sync_on(tmp_path):
    clock = FakeClock(WRONG)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock, voltage=1.0))
    clock.sync(local(2026, 10, 19, 12, 0, 30).timestamp())
    out.emit_reading(reading(clock, voltage=2.0))

    rows = lines(files_dir(tmp_path) / "tracker_2026-10-19_smartsolar.csv")
    assert len(rows) == 2
    assert rows[1].startswith(local(2026, 10, 19, 12, 0, 30).isoformat())
    assert rows[1].split(";")[VOLTAGE] == "2.0"


def test_dropping_is_logged_when_it_starts_and_ends(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="smartsolar.outputs.csv")
    clock = FakeClock(WRONG)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    for _ in range(3):
        out.emit_reading(reading(clock))
    clock.sync(local(2026, 10, 19, 12).timestamp())
    for _ in range(3):
        out.emit_reading(reading(clock))
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["The clock is not trusted yet", "The clock is trusted"]
