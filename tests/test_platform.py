"""The parts that behave differently per operating system, exercised on any of them."""

import asyncio
import signal

import pytest
from fakes import run

from smartsolar import serial_link
from smartsolar.config import AppConfig
from smartsolar.daemon import Daemon
from smartsolar.outputs import Outputs


def test_signal_handlers_fall_back_to_signal_signal_where_the_loop_has_none(monkeypatch):
    installed = {}

    class WindowsLoop:
        def add_signal_handler(self, sig, callback):
            raise NotImplementedError

        def call_soon_threadsafe(self, callback):
            callback()

    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))

    async def go():
        daemon = Daemon(AppConfig(), backend=object(), outputs=Outputs([]))
        daemon._install_signal_handlers(WindowsLoop())
        assert signal.SIGINT in installed
        installed[signal.SIGINT](signal.SIGINT, None)
        assert daemon.stop_event.is_set()

    run(go())


def test_signal_handlers_use_the_loop_where_it_can(monkeypatch):
    seen = []

    class Loop:
        def add_signal_handler(self, sig, callback):
            seen.append(sig)

    async def go():
        Daemon(AppConfig(), backend=object(), outputs=Outputs([]))._install_signal_handlers(Loop())

    run(go())
    assert signal.SIGINT in seen


@pytest.mark.parametrize("platform, exclusive", [("linux", True), ("darwin", True), ("win32", None)])
def test_the_serial_port_is_exclusive_where_the_system_allows_it(monkeypatch, platform, exclusive):
    import serial

    opened = {}

    class Port:
        def __init__(self, name, baud, **options):
            opened.update(name=name, baud=baud, **options)

    monkeypatch.setattr(serial, "Serial", Port)
    monkeypatch.setattr(serial_link.sys, "platform", platform)
    run(serial_link.open_pyserial("COM3" if platform == "win32" else "/dev/does-not-exist", 19200))
    assert opened.get("exclusive") is exclusive and opened["baud"] == 19200
    assert opened["name"] == ("COM3" if platform == "win32" else "/dev/does-not-exist")


def test_a_missing_port_is_reported_as_not_found(monkeypatch):
    from smartsolar.errors import ConnectError

    async def go():
        with pytest.raises(ConnectError) as err:
            await serial_link.open_pyserial("/dev/definitely-not-there", 19200)
        assert err.value.not_found

    run(go())


def test_a_port_pattern_picks_the_lowest_index_and_is_looked_up_again(tmp_path):
    from smartsolar.errors import ConnectError

    pattern = str(tmp_path / "ttyUSBvd*")
    with pytest.raises(ConnectError, match="no serial port matches") as err:
        serial_link.resolve_port(pattern)
    assert err.value.not_found
    for name in ("ttyUSBvd10", "ttyUSBvd2"):
        (tmp_path / name).touch()
    assert serial_link.resolve_port(pattern) == str(tmp_path / "ttyUSBvd2")  # 2 before 10
    (tmp_path / "ttyUSBvd1").touch()
    assert serial_link.resolve_port(pattern) == str(tmp_path / "ttyUSBvd1")  # a later cable is found
    assert serial_link.resolve_port("COM3") == "COM3" and serial_link.resolve_port("/dev/cu.usbserial-1") == "/dev/cu.usbserial-1"


def test_opening_a_pattern_opens_the_match(monkeypatch, tmp_path):
    import serial

    opened = {}

    class Port:
        def __init__(self, name, baud, **options):
            opened["name"] = name

    monkeypatch.setattr(serial, "Serial", Port)
    (tmp_path / "ttyUSBvd3").touch()
    run(serial_link.open_pyserial(str(tmp_path / "ttyUSBvd*"), 19200))
    assert opened["name"] == str((tmp_path / "ttyUSBvd3").resolve())
