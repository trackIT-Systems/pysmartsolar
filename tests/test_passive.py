import asyncio

import pytest

pytest.importorskip("dbus_fast")  # bleak's BlueZ backend, installed with bleak on Linux

from bleak.backends.bluezdbus import defs, manager  # noqa: E402

from smartsolar.ble import passive  # noqa: E402

PATH = "/org/bluez/hci0/dev_FF_DB_92_0B_1C_8A"
PROPERTIES = {"Adapter": "/org/bluez/hci0", "Address": "FF:DB:92:0B:1C:8A", "ManufacturerData": {0x02E1: b"\x10\x02\x53\xa0"}}


@pytest.fixture
def unpatched(monkeypatch):
    """bleak's own monitor before each test, and again after it."""
    monkeypatch.setattr(manager, "AdvertisementMonitor", manager.AdvertisementMonitor)
    if getattr(manager.AdvertisementMonitor, "reports_every_advertisement", False):
        monkeypatch.setattr(manager, "AdvertisementMonitor", manager.AdvertisementMonitor.__mro__[1])


class FakeManager:
    def __init__(self, fail=False):
        self._properties = {PATH: {defs.DEVICE_INTERFACE: PROPERTIES}}
        self.reported = []
        self.fail = fail

    def _run_advertisement_callbacks(self, path, properties):
        if self.fail:
            raise RuntimeError("callback failed")
        self.reported.append((path, properties))


def found(monkeypatch, bluez, *paths):
    """Calls DeviceFound of a monitor made by bleak's manager, in a loop that has ``bluez`` as its manager."""

    async def go():
        monkeypatch.setitem(manager._global_instances, asyncio.get_running_loop(), bluez)
        monitor = manager.AdvertisementMonitor([(0, 0xFF, b"\xe1\x02")])
        for path in paths:
            monitor.DeviceFound(path)

    asyncio.run(go())


def test_device_found_reports_every_advertisement_through_bleaks_callbacks(monkeypatch, unpatched):
    assert passive.install() and passive.install()  # once is enough, twice does no harm
    assert manager.AdvertisementMonitor.reports_every_advertisement
    bluez = FakeManager()
    found(monkeypatch, bluez, PATH, PATH, "/org/bluez/hci0/dev_unknown")
    assert bluez.reported == [(PATH, PROPERTIES), (PATH, PROPERTIES)]


def test_device_found_keeps_errors_out_of_dbus(monkeypatch, unpatched):
    assert passive.install()
    found(monkeypatch, FakeManager(fail=True), PATH)  # does not raise


def test_the_d_bus_signature_survives_the_override(unpatched):
    from dbus_fast.service import ServiceInterface

    assert passive.install()
    monitor = manager.AdvertisementMonitor([(0, 0xFF, b"\xe1\x02")])
    assert [m.in_signature for m in ServiceInterface._get_methods(monitor) if m.name == "DeviceFound"] == ["o"]


def test_not_installed_if_bleaks_manager_has_changed(monkeypatch, unpatched):
    monkeypatch.delattr(manager.BlueZManager, "_run_advertisement_callbacks")
    assert not passive.install()
    assert not getattr(manager.AdvertisementMonitor, "reports_every_advertisement", False)


def test_the_warning_about_a_stopped_monitor_is_dropped(unpatched, monkeypatch, caplog):
    bus_log = __import__("logging").getLogger("dbus_fast.message_bus")
    monkeypatch.setattr(bus_log, "filters", [])
    assert passive.install()
    with caplog.at_level("WARNING", logger="dbus_fast.message_bus"):
        bus_log.warning('"org.bluez.AdvertisementMonitor1.DeviceFound" with signature "o" could not be found')
        bus_log.warning("something else")
    assert [r.getMessage() for r in caplog.records] == ["something else"]
