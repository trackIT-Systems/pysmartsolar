"""Readings taken before the clock is trusted only go over MQTT; the files start with the sync."""

import logging
import os
from datetime import datetime

import pytest
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
    assert not files_dir(tmp_path).exists()


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


# The checks need file permissions that root ignores
needs_permissions = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")


@needs_permissions
def test_an_unwritable_directory_does_not_stop_the_output(tmp_path):
    # A station without a data volume: /data exists but is not writable
    tmp_path.chmod(0o500)
    try:
        clock = FakeClock(local(2026, 10, 19, 12).timestamp(), synced=True)
        out = CsvOutput(tmp_path, "tracker", clock=clock)
        out.emit_reading(reading(clock))
        assert list(tmp_path.iterdir()) == []
    finally:
        tmp_path.chmod(0o700)


@needs_permissions
def test_unwritable_files_are_logged_when_it_starts_and_ends(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="smartsolar.outputs.csv")
    clock = FakeClock(local(2026, 10, 19, 12).timestamp(), synced=True)
    tmp_path.chmod(0o500)
    try:
        out = CsvOutput(tmp_path, "tracker", clock=clock)
        for _ in range(3):
            out.emit_reading(reading(clock))
    finally:
        tmp_path.chmod(0o700)
    out.emit_reading(reading(clock, voltage=25.0))
    out.emit_reading(reading(clock, voltage=26.0))
    logged = [(r.levelname, r.getMessage().split(" (")[0].split(":")[0]) for r in caplog.records]
    assert logged == [
        ("WARNING", f"Cannot write CSV files to {out.dir}"),
        ("DEBUG", f"Cannot write CSV files to {out.dir} again"),
        ("DEBUG", f"Cannot write CSV files to {out.dir} again"),
        ("INFO", "CSV files can be written again"),
    ]
    # Rows from while it was unwritable are dropped, writing goes on with the next
    files = list(files_dir(tmp_path).glob("*.csv"))
    assert len(files) == 1
    rows = lines(files[0])
    assert len(rows) == 3
    assert rows[1].split(";")[VOLTAGE] == "25.0"
