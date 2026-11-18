import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from smartsolar.ble import bleak_backend as bb
from smartsolar.errors import ConnectError, LinkError, NotConnectedError, PairingError
from smartsolar.vreg import CHAR_UUIDS

from fakes import A, advert_data, run, until

WATCH_BLUETOOTHD = bb.watch_bluetoothd  # before the fixture below stands in for it


def agent_module(monkeypatch):
    """smartsolar.ble.agent needs dbus-fast (Linux only): elsewhere a stand-in module takes its place."""
    try:
        import smartsolar.ble.agent as module
    except ModuleNotFoundError:
        import sys
        import types

        module = types.ModuleType("smartsolar.ble.agent")
        monkeypatch.setitem(sys.modules, "smartsolar.ble.agent", module)
    return module


@pytest.fixture(autouse=True)
def bluez(monkeypatch):
    """The tests below describe the BlueZ path (pairing agent); the other systems are tested at the end."""
    monkeypatch.setattr(bb, "HAS_AGENT", True)
    monkeypatch.setattr(bb.passive, "install", lambda: True)  # tested in test_passive.py
    watching = []

    async def watch_bluetoothd(on_start):
        watching.append(on_start)
        await asyncio.Event().wait()

    monkeypatch.setattr(bb, "watch_bluetoothd", watch_bluetoothd)
    return watching


def test_connect_error_mapping():
    assert bb.map_connect_error(BleakDeviceNotFoundError("x")).not_found
    busy = bb.map_connect_error(BleakDBusError("org.bluez.Error.InProgress", []))
    assert isinstance(busy, ConnectError) and busy.in_progress and not busy.not_found
    assert "timed out" in str(bb.map_connect_error(TimeoutError()))
    other = bb.map_connect_error(BleakError("boom"))
    assert not other.in_progress and "boom" in str(other)


def test_write_error_mapping():
    assert isinstance(bb.map_write_error(BleakError("x"), connected=False), NotConnectedError)
    assert isinstance(bb.map_write_error(BleakError("Not connected"), connected=True), NotConnectedError)
    assert isinstance(bb.map_write_error(BleakError("busy"), connected=True), LinkError)


def test_pair_error_mapping():
    err = bb.map_pair_error(BleakDBusError("org.bluez.Error.AuthenticationFailed", []))
    assert isinstance(err, PairingError) and "passkey" in str(err)
    assert "x" in str(bb.map_pair_error(BleakError("x")))


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.notify = {}
        self.writes = []
        self.pair_result = None
        self.disconnects = 0

    async def start_notify(self, uuid, callback):
        self.notify[uuid] = callback

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append((uuid, data, response))

    async def read_gatt_char(self, uuid):
        if not self.is_connected:
            raise BleakError("Not connected")
        return bytearray.fromhex("00040001de4a00") if uuid == CHAR_UUIDS["0021"] else bytearray()

    async def pair(self):
        if isinstance(self.pair_result, Exception):
            raise self.pair_result

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


def test_link_notifications_arrive_with_the_characteristic_name():
    async def go():
        client = FakeClient()
        link = bb.BleakLink(A, client)
        got = []
        await link.start_notify(lambda char, data: got.append((char, data)))
        assert set(client.notify) == set(CHAR_UUIDS.values())
        client.notify[CHAR_UUIDS["0024"]](None, bytearray(b"\x08\x00"))
        assert got == [("0024", b"\x08\x00")]
        await link.write("0021", b"\xf9\x80")
        assert client.writes == [(CHAR_UUIDS["0021"], b"\xf9\x80", False)]  # without response

    run(go())


def test_link_reads_the_control_characteristic():
    async def go():
        client = FakeClient()
        link = bb.BleakLink(A, client)
        assert await link.read("0021") == bytes.fromhex("00040001de4a00")
        client.is_connected = False
        with pytest.raises(NotConnectedError):
            await link.read("0021")

    run(go())


def test_link_write_failure_when_disconnected():
    async def go():
        client = FakeClient()

        async def broken(uuid, data, response=True):
            client.is_connected = False
            raise BleakError("gone")

        client.write_gatt_char = broken
        with pytest.raises(NotConnectedError):
            await bb.BleakLink(A, client).write("0024", b"x")

    run(go())


def test_pair_treats_an_existing_bond_as_success(monkeypatch):
    async def go():
        client = FakeClient()
        client.pair_result = BleakDBusError("org.bluez.Error.AlreadyExists", [])
        link = bb.BleakLink(A, client)

        class NoAgent:
            def __init__(self, passkey):
                self.passkey = passkey

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(agent_module(monkeypatch), "registered_agent", NoAgent, raising=False)
        await link.pair("000000", 1.0)  # no error
        client.pair_result = BleakDBusError("org.bluez.Error.AuthenticationFailed", [])
        with pytest.raises(PairingError, match="passkey"):
            await link.pair("000000", 1.0)

        async def hang():
            await asyncio.sleep(5)

        client.pair = hang
        with pytest.raises(PairingError, match="did not finish"):
            await link.pair("000000", 0.05)

    run(go())


def test_the_agent_answers_the_passkey_request():
    pytest.importorskip("dbus_fast")
    from smartsolar.ble.agent import PasskeyAgent

    # dbus-fast wraps the methods; the plain functions are behind __wrapped__
    passkey = PasskeyAgent.RequestPasskey.__wrapped__
    pin = PasskeyAgent.RequestPinCode.__wrapped__
    assert passkey(PasskeyAgent(0), "/org/bluez/hci0/dev_x") == 0 and pin(PasskeyAgent(0), "/x") == "000000"
    assert passkey(PasskeyAgent(123456), "/x") == 123456 and pin(PasskeyAgent(42), "/x") == "000042"


def test_the_agent_accepts_the_other_requests():
    pytest.importorskip("dbus_fast")
    from smartsolar.ble.agent import PasskeyAgent

    agent = PasskeyAgent(0)
    for name, args in [("Release", ()), ("DisplayPasskey", ("/x", 1, 0)), ("RequestConfirmation", ("/x", 1)), ("RequestAuthorization", ("/x",)), ("Cancel", ())]:
        assert getattr(PasskeyAgent, name).__wrapped__(agent, *args) is None


class FakeAgentBus:
    """The system bus as registered_agent uses it: export, introspect, AgentManager1 calls, disconnect."""

    def __init__(self, unregister_error=None):
        self.calls = []
        self.unregister_error = unregister_error

    def __call__(self, bus_type=None):
        return self

    async def connect(self):
        self.calls.append("connect")
        return self

    def export(self, path, interface):
        self.calls.append(("export", path, interface.passkey if hasattr(interface, "passkey") else interface._passkey))

    async def introspect(self, service, path):
        self.calls.append(("introspect", service, path))
        return "xml"

    def get_proxy_object(self, service, path, introspection):
        bus = self

        class Manager:
            async def call_register_agent(self, path, capability):
                bus.calls.append(("register", path, capability))

            async def call_request_default_agent(self, path):
                bus.calls.append(("default", path))

            async def call_unregister_agent(self, path):
                bus.calls.append(("unregister", path))
                if bus.unregister_error is not None:
                    raise bus.unregister_error

        return SimpleNamespace(get_interface=lambda name: bus.calls.append(("interface", name)) or Manager())

    def disconnect(self):
        self.calls.append("disconnect")


@pytest.mark.parametrize("unregister_error", [None, OSError("bluetoothd gone")], ids=["unregistered", "unregister fails"])
def test_the_agent_is_registered_as_default_and_unregistered_after(monkeypatch, unregister_error):
    pytest.importorskip("dbus_fast")
    from smartsolar.ble import agent

    bus = FakeAgentBus(unregister_error)
    monkeypatch.setattr(agent, "MessageBus", bus)

    async def go():
        async with agent.registered_agent(123456):
            assert bus.calls == [
                "connect",
                ("export", agent.AGENT_PATH, 123456),
                ("introspect", "org.bluez", "/org/bluez"),
                ("interface", "org.bluez.AgentManager1"),
                ("register", agent.AGENT_PATH, "KeyboardDisplay"),
                ("default", agent.AGENT_PATH),
            ]
        assert bus.calls[-2:] == [("unregister", agent.AGENT_PATH), "disconnect"]  # also when unregistering fails

    run(go())


def test_scanner_callback_filters_victron_adverts_only():
    backend = bb.BleakBackend()
    got = []
    backend._on_advert = got.append
    device = SimpleNamespace(address="f7:48:18:8a:77:9d", name="dev")
    backend._detected(device, SimpleNamespace(manufacturer_data={0x004C: b"\x10\x06"}, local_name=None, rssi=-50))
    backend._detected(device, SimpleNamespace(manufacturer_data={0x02E1: b"\x11" + bytes(10)}, local_name=None, rssi=-50))
    backend._detected(device, SimpleNamespace(manufacturer_data={0x02E1: advert_data()}, local_name="SmartSolar", rssi=-47))
    assert len(got) == 1
    adv = got[0]
    assert adv.address == A and adv.name == "SmartSolar" and adv.rssi == -47 and adv.data == advert_data() and adv.handle is device


def test_connect_stops_the_scan_and_restarts_it(monkeypatch):
    async def go():
        events = []
        backend = bb.BleakBackend()
        backend._on_advert = lambda adv: None

        class FakeScanner:
            def __init__(self, **kw):
                events.append("scanner created")

            async def start(self):
                events.append("scan start")

            async def stop(self):
                events.append("scan stop")

        class FakeBleakClient(FakeClient):
            def __init__(self, handle, **kw):
                super().__init__()
                events.append("client created")

            async def connect(self):
                events.append("connect")

        async def find(address, timeout=None, **kw):
            events.append("lookup")
            return object()

        FakeScanner.find_device_by_address = staticmethod(find)
        monkeypatch.setattr(bb, "BleakScanner", FakeScanner)
        monkeypatch.setattr(bb, "BleakClient", FakeBleakClient)
        await backend.start_scan(lambda adv: None)
        events.clear()
        from fakes import advert

        link = await backend.connect(advert(), timeout_s=1.0, on_disconnect=lambda: None)
        assert events == ["scan stop", "lookup", "client created", "connect", "scanner created", "scan start"]
        assert isinstance(link, bb.BleakLink)

    run(go())


def test_a_failed_connect_cleans_up_and_still_restarts_the_scan(monkeypatch):
    async def go():
        events = []
        backend = bb.BleakBackend()

        class FakeScanner:
            def __init__(self, **kw): ...

            async def start(self):
                events.append("scan start")

            async def stop(self):
                events.append("scan stop")

        async def find(address, timeout=None, **kw):
            return object()

        FakeScanner.find_device_by_address = staticmethod(find)

        class FailingClient(FakeClient):
            def __init__(self, handle, **kw):
                super().__init__()
                self.outer = self

            async def connect(self):
                events.append("connect")
                raise BleakDBusError("org.bluez.Error.InProgress", [])

            async def disconnect(self):
                events.append("cleanup disconnect")

        monkeypatch.setattr(bb, "BleakScanner", FakeScanner)
        monkeypatch.setattr(bb, "BleakClient", FailingClient)
        await backend.start_scan(lambda adv: None)
        events.clear()
        from fakes import advert

        with pytest.raises(ConnectError) as err:
            await backend.connect(advert(), timeout_s=1.0, on_disconnect=lambda: None)
        assert err.value.in_progress
        assert events == ["scan stop", "connect", "cleanup disconnect", "scan start"]

        async def not_found(address, timeout=None, **kw):
            return None

        FakeScanner.find_device_by_address = staticmethod(not_found)
        events.clear()
        with pytest.raises(ConnectError) as err:
            await backend.connect(advert(), timeout_s=1.0, on_disconnect=lambda: None)
        assert err.value.not_found and events[-1] == "scan start"

    run(go())


def patch_connect_env(monkeypatch, events, *, connect_error=None):
    """Fake scanner, client and agent that log what the backend does with them."""

    class FakeScanner:
        def __init__(self, **kw): ...

        async def start(self):
            events.append("scan start")

        async def stop(self):
            events.append("scan stop")

    async def find(address, timeout=None, **kw):
        events.append("lookup")
        return object()

    FakeScanner.find_device_by_address = staticmethod(find)

    class Client(FakeClient):
        def __init__(self, handle, **kw):
            super().__init__()

        async def connect(self):
            events.append("connect")
            if connect_error is not None:
                raise connect_error

        async def pair(self):
            events.append("pair")

        async def disconnect(self):
            events.append("client disconnect")

    class Agent:
        def __init__(self, passkey):
            self.passkey = passkey

        async def __aenter__(self):
            events.append(f"agent up {self.passkey}")
            return self

        async def __aexit__(self, *exc):
            events.append("agent down")
            return False

    monkeypatch.setattr(bb, "BleakScanner", FakeScanner)
    monkeypatch.setattr(bb, "BleakClient", Client)
    monkeypatch.setattr(agent_module(monkeypatch), "registered_agent", Agent, raising=False)


def test_the_pairing_agent_is_there_before_the_connection_opens_and_until_pairing_is_done(monkeypatch):
    async def go():
        events = []
        patch_connect_env(monkeypatch, events)
        backend = bb.BleakBackend()
        from fakes import advert

        link = await backend.connect(advert(), timeout_s=1.0, on_disconnect=lambda: None, passkey="000000")
        assert events == ["agent up 0", "lookup", "connect"]  # no scanner was running
        await link.pair("000000", 1.0)
        assert events[-2:] == ["pair", "agent down"]
        await link.disconnect()
        assert events.count("agent down") == 1  # released once

    run(go())


def test_the_agent_is_released_by_disconnect_when_pairing_never_ran(monkeypatch):
    async def go():
        events = []
        patch_connect_env(monkeypatch, events)
        from fakes import advert

        link = await bb.BleakBackend().connect(advert(), timeout_s=1.0, on_disconnect=lambda: None, passkey="123456")
        assert events[0] == "agent up 123456"
        await link.disconnect()
        assert events[-2:] == ["client disconnect", "agent down"]

    run(go())


def test_the_agent_is_released_when_the_connect_fails(monkeypatch):
    async def go():
        events = []
        patch_connect_env(monkeypatch, events, connect_error=BleakDBusError("org.bluez.Error.Failed", []))
        from fakes import advert

        with pytest.raises(ConnectError):
            await bb.BleakBackend().connect(advert(), timeout_s=1.0, on_disconnect=lambda: None, passkey="000000")
        assert events == ["agent up 0", "lookup", "connect", "client disconnect", "agent down"]

    run(go())


def test_without_a_passkey_no_agent_is_used(monkeypatch):
    async def go():
        events = []
        patch_connect_env(monkeypatch, events)
        from fakes import advert

        await bb.BleakBackend().connect(advert(), timeout_s=1.0, on_disconnect=lambda: None)
        assert events == ["lookup", "connect"]

    run(go())


# -- macOS and Windows: no agent, the system pairs ----------------------------------


def test_without_an_agent_the_system_pairs(monkeypatch):
    monkeypatch.setattr(bb, "HAS_AGENT", False)

    async def go():
        client = FakeClient()
        link = bb.BleakLink(A, client)
        await link.pair("000000", 1.0)  # the system's own pairing: no error

        async def not_implemented():
            raise NotImplementedError("Pairing is not available in Core Bluetooth")

        client.pair = not_implemented
        await link.pair("000000", 1.0)  # CoreBluetooth cannot pair on request: left to the system, no error
        client.pair_result = BleakError("Authentication failed")

        async def fails():
            raise BleakError("Authentication failed")

        client.pair = fails
        with pytest.raises(PairingError, match="Authentication failed"):
            await link.pair("000000", 1.0)

        async def already():
            raise BleakError("Device already paired")

        client.pair = already
        await link.pair("000000", 1.0)

        async def hang():
            await asyncio.sleep(5)

        client.pair = hang
        with pytest.raises(PairingError, match="system dialog"):
            await link.pair("000000", 0.05)

    run(go())


def test_without_an_agent_no_agent_is_used_to_connect(monkeypatch):
    monkeypatch.setattr(bb, "HAS_AGENT", False)
    events = []
    patch_connect_env(monkeypatch, events)

    async def go():
        backend = bb.BleakBackend(adapter="hci1")
        link = await backend.connect(SimpleNamespace(address=A), timeout_s=1.0, on_disconnect=lambda: None, passkey="000000")
        assert events == ["lookup", "connect"] and link._agent is None
        assert backend._kwargs() == {}  # an adapter name means nothing outside BlueZ

    run(go())


def test_a_device_the_lookup_does_not_find_again_is_connected_by_its_last_advert(monkeypatch):
    async def go():
        handles = []
        backend = bb.BleakBackend()

        class FakeScanner:
            def __init__(self, **kw): ...

            async def start(self): ...

            async def stop(self): ...

        async def find(address, timeout=None, **kw):
            return None  # e.g. no scan responses while the Wi-Fi access point is up

        FakeScanner.find_device_by_address = staticmethod(find)

        class FakeBleakClient(FakeClient):
            def __init__(self, handle, **kw):
                super().__init__()
                handles.append(handle)

            async def connect(self): ...

        monkeypatch.setattr(bb, "BleakScanner", FakeScanner)
        monkeypatch.setattr(bb, "BleakClient", FakeBleakClient)
        from fakes import advert, product_advert

        known = object()
        link = await backend.connect(product_advert(handle=known), timeout_s=1.0, on_disconnect=lambda: None)
        assert handles == [known] and isinstance(link, bb.BleakLink)
        with pytest.raises(ConnectError) as err:
            await backend.connect(advert(), timeout_s=1.0, on_disconnect=lambda: None)  # no handle to fall back to
        assert err.value.not_found

    run(go())
# -- passive scanning ----------------------------------------------------------

NO_MONITOR = BleakError("passive scanning on Linux requires BlueZ >= 5.56 with --experimental enabled and Linux kernel >= 5.10")


def recording_scanner(monkeypatch, created, fail_passive=None):
    """A BleakScanner that records its arguments; a passive one raises ``fail_passive`` on start."""

    class FakeScanner:
        def __init__(self, **kw):
            self.kw = kw
            created.append(kw)

        async def start(self):
            if fail_passive is not None and self.kw.get("scanning_mode") == "passive":
                raise fail_passive

        async def stop(self): ...

    async def find(address, timeout=None, **kw):
        created.append({"lookup": True, **kw})
        return object()

    FakeScanner.find_device_by_address = staticmethod(find)
    monkeypatch.setattr(bb, "BleakScanner", FakeScanner)

    class ConnectingClient(FakeClient):
        def __init__(self, handle, **kw):
            super().__init__()

        async def connect(self): ...

    monkeypatch.setattr(bb, "BleakClient", ConnectingClient)


def test_scans_passively_for_victron_manufacturer_data_on_bluez(monkeypatch):
    created = []
    recording_scanner(monkeypatch, created)

    async def go():
        backend = bb.BleakBackend(adapter="hci0")
        await backend.start_scan(lambda adv: None)
        await backend.connect(SimpleNamespace(address=A), timeout_s=1.0, on_disconnect=lambda: None)

    run(go())
    # the scan, the lookup before connecting and the scan again afterwards: all passive
    assert len(created) == 3
    for kw in created:
        assert kw["scanning_mode"] == "passive" and kw["adapter"] == "hci0"
        [(start, ad_type, content)] = kw["bluez"]["or_patterns"]
        assert (start, int(ad_type), content) == (0, 0xFF, b"\xe1\x02")


def test_falls_back_to_active_scanning_once_if_bluez_cannot_scan_passively(monkeypatch, caplog):
    created = []
    recording_scanner(monkeypatch, created, fail_passive=NO_MONITOR)

    async def go():
        backend = bb.BleakBackend()
        await backend.start_scan(lambda adv: None)
        await backend.connect(SimpleNamespace(address=A), timeout_s=1.0, on_disconnect=lambda: None)

    with caplog.at_level("WARNING"):
        run(go())
    assert [kw.get("scanning_mode") for kw in created] == ["passive", None, None, None]
    assert [r.message for r in caplog.records if "passively" in r.message] == [
        f"BlueZ cannot scan passively, scanning actively: set Experimental = true in /etc/bluetooth/main.conf ({NO_MONITOR})"
    ]


@pytest.mark.parametrize(
    "scanning, error",
    [("passive", NO_MONITOR), ("auto", BleakError("adapter 'hci1' not found"))],
)
def test_other_scan_errors_and_a_forced_passive_scan_do_not_fall_back(monkeypatch, scanning, error):
    created = []
    recording_scanner(monkeypatch, created, fail_passive=error)

    async def go():
        backend = bb.BleakBackend(scanning=scanning)
        with pytest.raises(BleakError):
            await backend.start_scan(lambda adv: None)
        assert backend._scanner is None and backend._passive  # tried again passively by the scan supervisor

    run(go())


def test_active_scanning_when_configured_or_without_bluez(monkeypatch, caplog):
    created = []
    recording_scanner(monkeypatch, created)

    async def go():
        await bb.BleakBackend(scanning="active").start_scan(lambda adv: None)
        monkeypatch.setattr(bb, "HAS_AGENT", False)
        await bb.BleakBackend().start_scan(lambda adv: None)
        with caplog.at_level("WARNING"):
            await bb.BleakBackend(scanning="passive").start_scan(lambda adv: None)

    run(go())
    assert [kw.get("scanning_mode") for kw in created] == [None, None, None]
    assert "ble.scanning: passive is only possible with BlueZ on Linux" in caplog.text


@pytest.mark.parametrize(
    "scanning, mode, warning",
    [
        ("auto", None, "Cannot make bleak report every advertisement in a passive scan, scanning actively"),
        ("passive", "passive", "Cannot make bleak report every advertisement in a passive scan: unchanged ones are reported once"),
    ],
)
def test_without_the_monitor_patch_auto_scans_actively(monkeypatch, caplog, scanning, mode, warning):
    monkeypatch.setattr(bb.passive, "install", lambda: False)
    created = []
    recording_scanner(monkeypatch, created)
    with caplog.at_level("WARNING"):
        run(bb.BleakBackend(scanning=scanning).start_scan(lambda adv: None))
    assert [kw.get("scanning_mode") for kw in created] == [mode]
    assert [r.message for r in caplog.records] == [warning]


def test_a_bluetoothd_restart_drops_the_scanner_and_marks_the_scan_lost(monkeypatch, bluez):
    created = []
    recording_scanner(monkeypatch, created)
    stopped = []

    async def go():
        backend = bb.BleakBackend()
        await backend.start_scan(lambda adv: None)
        await asyncio.sleep(0)
        [restarted] = bluez
        scanner = backend._scanner
        scanner.stop = lambda: stopped.append(scanner) or asyncio.sleep(0)
        assert not backend.scan_lost()
        restarted()
        assert backend.scan_lost() and backend._scanner is None
        await asyncio.sleep(0.01)
        assert stopped == [scanner]  # bleak forgets its callbacks
        await backend.start_scan(lambda adv: None)
        assert not backend.scan_lost() and backend._scanner is not None and len(created) == 2
        assert len(bluez) == 1  # watched once

        await backend.stop_scan()
        restarted()  # not scanning: nothing lost
        assert not backend.scan_lost()

    run(go())


def test_a_scanner_that_cannot_be_restarted_after_connecting_counts_as_lost(monkeypatch):
    created = []
    recording_scanner(monkeypatch, created)

    async def go():
        backend = bb.BleakBackend()
        await backend.start_scan(lambda adv: None)
        start = bb.BleakScanner.start

        async def refuse(self):
            raise BleakError("adapter not ready")

        bb.BleakScanner.start = refuse
        await backend.connect(SimpleNamespace(address=A), timeout_s=1.0, on_disconnect=lambda: None)
        assert backend.scan_lost() and backend._scanner is None
        bb.BleakScanner.start = start
        await backend.start_scan(lambda adv: None)
        assert not backend.scan_lost()

    run(go())


def test_a_watch_that_cannot_start_is_told_and_scanning_goes_on(monkeypatch, caplog):
    created = []
    recording_scanner(monkeypatch, created)

    async def no_bus(on_start):
        raise FileNotFoundError("no system bus")

    monkeypatch.setattr(bb, "watch_bluetoothd", no_bus)

    async def go():
        backend = bb.BleakBackend()
        await backend.start_scan(lambda adv: None)
        await asyncio.sleep(0.01)
        assert backend._scanner is not None

    with caplog.at_level("WARNING"):
        run(go())
    assert [r.message for r in caplog.records if "bluetoothd" in r.message] == [
        "Cannot watch for bluetoothd restarts, the scan does not survive one: no system bus"
    ]


# -- watching bluetoothd ---------------------------------------------------------------


@pytest.fixture
def system_bus(monkeypatch):
    """dbus-fast's MessageBus, Message and types as stand-ins (also where dbus-fast is not installed)."""
    import sys
    import types

    buses = []
    kinds = SimpleNamespace(SIGNAL="signal", ERROR="error", METHOD_RETURN="method_return")

    class FakeBus:
        reply = SimpleNamespace(message_type=kinds.METHOD_RETURN)

        def __init__(self, bus_type=None):
            self.bus_type = bus_type
            self.handlers = []
            self.calls = []
            self.closed = asyncio.Event()

        async def connect(self):
            buses.append(self)
            return self

        def add_message_handler(self, handler):
            self.handlers.append(handler)

        async def call(self, message):
            self.calls.append(message)
            return self.reply

        async def wait_for_disconnect(self):
            await self.closed.wait()

        def disconnect(self):
            self.closed.set()

        def owner_changed(self, name, old, new, member="NameOwnerChanged", kind=kinds.SIGNAL):
            for handler in self.handlers:
                handler(SimpleNamespace(message_type=kind, member=member, body=[name, old, new]))

    dbus_fast = types.ModuleType("dbus_fast")
    dbus_fast.BusType = SimpleNamespace(SYSTEM="system")
    dbus_fast.MessageType = kinds
    dbus_fast.Message = lambda **kw: SimpleNamespace(**kw)
    aio = types.ModuleType("dbus_fast.aio")
    aio.MessageBus = FakeBus
    monkeypatch.setitem(sys.modules, "dbus_fast", dbus_fast)
    monkeypatch.setitem(sys.modules, "dbus_fast.aio", aio)
    return SimpleNamespace(buses=buses, kinds=kinds, FakeBus=FakeBus)


def test_watch_bluetoothd_reports_only_bluetoothd_taking_its_name(system_bus):
    async def go():
        started = []
        task = asyncio.create_task(WATCH_BLUETOOTHD(lambda: started.append(1)))
        await until(lambda: system_bus.buses and system_bus.buses[0].calls)
        [bus] = system_bus.buses
        [match] = bus.calls
        assert bus.bus_type == "system" and match.member == "AddMatch" and match.destination == "org.freedesktop.DBus"
        assert "member='NameOwnerChanged'" in match.body[0] and "arg0='org.bluez'" in match.body[0]

        bus.owner_changed("org.bluez", ":1.5", "")  # bluetoothd stops
        bus.owner_changed("org.freedesktop.hostname1", "", ":1.9")  # another service starts
        bus.owner_changed("org.bluez", "", ":1.7", member="NameLost")
        bus.owner_changed("org.bluez", "", ":1.7", kind=system_bus.kinds.METHOD_RETURN)
        assert started == []
        bus.owner_changed("org.bluez", "", ":1.7")  # bluetoothd is back
        assert started == [1]

        bus.disconnect()  # the system bus goes away: the watch ends
        await asyncio.wait_for(task, 1.0)

    run(go())


def test_watch_bluetoothd_fails_when_the_bus_refuses_the_match_and_closes_it(system_bus):
    system_bus.FakeBus.reply = SimpleNamespace(message_type=system_bus.kinds.ERROR, error_name="org.freedesktop.DBus.Error.AccessDenied", body=["no"])

    async def go():
        with pytest.raises(OSError, match="AccessDenied"):
            await WATCH_BLUETOOTHD(lambda: None)
        assert system_bus.buses[0].closed.is_set()

    run(go())


def test_a_watch_that_ends_is_told(monkeypatch, caplog):
    created = []
    recording_scanner(monkeypatch, created)

    async def ends(on_start):
        return None

    monkeypatch.setattr(bb, "watch_bluetoothd", ends)

    async def go():
        await bb.BleakBackend().start_scan(lambda adv: None)
        await asyncio.sleep(0.01)

    with caplog.at_level("WARNING"):
        run(go())
    assert [r.message for r in caplog.records if "bluetoothd" in r.message] == ["The system bus closed: bluetoothd restarts are no longer noticed"]


def test_the_scanner_of_an_earlier_bluetoothd_is_dropped_although_stopping_it_fails(monkeypatch, bluez, caplog):
    created = []
    recording_scanner(monkeypatch, created)

    async def go():
        backend = bb.BleakBackend()
        await backend.start_scan(lambda adv: None)
        await asyncio.sleep(0)

        async def gone():
            raise BleakDBusError("org.bluez.Error.DoesNotExist", [])

        backend._scanner.stop = gone
        bluez[0]()
        await asyncio.sleep(0.01)
        assert backend.scan_lost() and backend._discards == set()

    with caplog.at_level("DEBUG", logger="smartsolar.ble.bleak_backend"):
        run(go())
    assert [r.levelname for r in caplog.records if "earlier bluetoothd" in r.message] == ["DEBUG"]


# -- links and connects that go wrong ---------------------------------------------------


def test_link_notifications_that_cannot_be_enabled_fail_the_link():
    async def go():
        client = FakeClient()

        async def refuse(uuid, callback):
            raise BleakError("not permitted")

        client.start_notify = refuse
        with pytest.raises(LinkError, match="cannot enable notifications on .*: BleakError: not permitted"):
            await bb.BleakLink(A, client).start_notify(lambda char, data: None)

    run(go())


def test_a_pairing_error_of_the_client_and_a_link_already_gone(monkeypatch):
    async def go():
        client = FakeClient()
        client.pair_result = PairingError("refused")
        link = bb.BleakLink(A, client, agent=SimpleNamespace(__aexit__=lambda *exc: asyncio.sleep(0)))
        with pytest.raises(PairingError, match="refused"):
            await link.pair("000000", 1.0)

        async def gone():
            raise EOFError

        client.disconnect = gone
        await link.disconnect()  # no error

    run(go())


def test_a_cancelled_connect_closes_the_client_and_releases_the_agent(monkeypatch, caplog):
    events = []
    patch_connect_env(monkeypatch, events)

    class Hanging(bb.BleakClient):
        async def connect(self):
            events.append("connect")
            await asyncio.sleep(10)

        async def disconnect(self):
            events.append("client disconnect")
            raise BleakError("not connected")

    monkeypatch.setattr(bb, "BleakClient", Hanging)

    class StuckAgent:
        def __init__(self, passkey): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            events.append("agent down")
            raise BleakError("agent gone")

    monkeypatch.setattr(agent_module(monkeypatch), "registered_agent", StuckAgent, raising=False)

    async def go():
        from fakes import advert

        task = asyncio.create_task(bb.BleakBackend().connect(advert(), timeout_s=5.0, on_disconnect=lambda: None, passkey="000000"))
        await until(lambda: "connect" in events)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level("DEBUG", logger="smartsolar.ble.bleak_backend"):
        run(go())
    assert events == ["lookup", "connect", "client disconnect", "agent down"]
    debug = [r.message for r in caplog.records if r.levelname == "DEBUG"]
    assert any(m.startswith("Cannot close the BLE connection after a failed connect") for m in debug)
    assert any(m.startswith("Cannot unregister the pairing agent after a failed connection") for m in debug)


def test_a_second_start_keeps_the_scanner_and_a_failed_stop_is_told(monkeypatch, caplog):
    created = []
    recording_scanner(monkeypatch, created)

    async def go():
        backend = bb.BleakBackend()
        await backend.start_scan(lambda adv: None)
        await backend.start_scan(lambda adv: None)
        assert len(created) == 1

        async def refuse():
            raise BleakDBusError("org.bluez.Error.Failed", ["No discovery started"])

        backend._scanner.stop = refuse
        await backend.stop_scan()
        assert backend._scanner is None

    with caplog.at_level("WARNING"):
        run(go())
    assert [r.message for r in caplog.records if "stop" in r.message] == ["Cannot stop the BLE scanner: [org.bluez.Error.Failed] No discovery started"]
