"""Readings taken before the clock is synced wait in a spool and are re-timed for the files."""

from datetime import datetime

from fakes import FakeClock
from smartsolar.clock import Clock
from smartsolar.columns import COLUMNS
from smartsolar.outputs.csv import CsvOutput
from smartsolar.reading import Reading, Value

MAC = "F7:48:18:8A:77:9D"
# Position of battery_voltage in a file row (after time, synced, device, block)
VOLTAGE = 4 + COLUMNS.index("battery_voltage")


def local(*args):
    return datetime(*args).astimezone()


def reading(clock, voltage=24.0):
    """A reading stamped like the sources stamp it, with the fake clock's times."""
    value = Value("battery_voltage", "Battery voltage", "V", voltage)
    return Reading(MAC, "vedirect", datetime.fromtimestamp(clock.time()).astimezone(), (value,), boottime=clock.boottime())


def files_dir(tmp_path):
    return tmp_path / "tracker" / "smartsolar"


def lines(path):
    return path.read_text().splitlines()


def test_unsynced_readings_wait_in_the_spool(tmp_path):
    clock = FakeClock(local(1970, 1, 1, 0, 5).timestamp())
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock))
    assert out.spool.exists() and not list(files_dir(tmp_path).glob("*.csv"))


def test_the_sync_retimes_the_spool_by_boottime_and_writes_it_first(tmp_path):
    clock = FakeClock(local(1970, 1, 1, 0, 5).timestamp(), boottime=100)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock, 1.0))
    clock.advance(60)
    out.emit_reading(reading(clock, 2.0))
    clock.advance(30)
    clock.sync(local(2026, 10, 19, 12, 0, 30).timestamp())
    out.emit_reading(reading(clock, 3.0))

    assert not out.spool.exists()
    rows = [[*line.split(";")[:3], line.split(";")[VOLTAGE]] for line in lines(files_dir(tmp_path) / "tracker_2026-10-19_smartsolar.csv")[1:]]
    assert rows == [
        [local(2026, 10, 19, 11, 59).isoformat(), "False", MAC, "1.0"],
        [local(2026, 10, 19, 12, 0).isoformat(), "False", MAC, "2.0"],
        [local(2026, 10, 19, 12, 0, 30).isoformat(), "True", MAC, "3.0"],
    ]


def test_a_spool_spanning_midnight_is_split_by_day(tmp_path):
    clock = FakeClock(0.0, boottime=10)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock))
    clock.advance(120)
    out.emit_reading(reading(clock))
    clock.sync(local(2026, 10, 20, 0, 1).timestamp())
    out.flush_spool()
    assert lines(files_dir(tmp_path) / "tracker_2026-10-19_smartsolar.csv")[1].startswith(local(2026, 10, 19, 23, 59).isoformat())
    assert lines(files_dir(tmp_path) / "tracker_2026-10-20_smartsolar.csv")[1].startswith(local(2026, 10, 20, 0, 1).isoformat())


def test_a_restart_before_the_sync_continues_the_same_spool(tmp_path):
    clock = FakeClock(0.0)
    CsvOutput(tmp_path, "tracker", clock=clock).emit_reading(reading(clock))
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock))
    assert len(lines(out.spool)) == 2


def test_spools_of_earlier_boots_become_unsynced_files(tmp_path):
    old = FakeClock(local(1970, 1, 1).timestamp(), boot_id="boot-a")
    CsvOutput(tmp_path, "tracker", clock=old).emit_reading(reading(old))

    out = CsvOutput(tmp_path, "tracker", clock=FakeClock(0.0, boot_id="boot-b"))
    assert not list(out.spool_dir.glob("*.jsonl"))
    rows = lines(files_dir(tmp_path) / "tracker_unsynced_boot-a_smartsolar.csv")
    assert rows[0].startswith("time;synced;device;")
    assert rows[1].startswith(f"{local(1970, 1, 1).isoformat()};False;{MAC};") and rows[1].split(";")[VOLTAGE] == "24.0"


def test_an_unreadable_spool_line_is_skipped(tmp_path):
    clock = FakeClock(0.0)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock))
    with out.spool.open("a") as handle:
        handle.write('{"key": "rea')  # cut off by a power loss
    clock.sync(local(2026, 10, 19, 12).timestamp())
    out.emit_reading(reading(clock))
    assert len(lines(files_dir(tmp_path) / "tracker_2026-10-19_smartsolar.csv")) == 3


def test_the_synced_latch_survives_a_restart_through_its_marker(tmp_path, monkeypatch):
    import smartsolar.clock as clock_module

    monkeypatch.setattr(clock_module.Clock, "boot_id", lambda self: "boot-a")
    monkeypatch.setattr(clock_module, "kernel_clock_synced", lambda: True)
    assert Clock(tmp_path).synced()
    assert (tmp_path / "boot-a.synced").exists()
    monkeypatch.setattr(clock_module, "kernel_clock_synced", lambda: False)
    assert Clock(tmp_path).synced()  # same boot: the marker says it was synced
    assert not Clock(tmp_path / "elsewhere").synced()
