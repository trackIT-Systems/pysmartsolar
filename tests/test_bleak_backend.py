import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from smartsolar.ble import bleak_backend as bb
from smartsolar.errors import ConnectError, LinkError, NotConnectedError, PairingError
from smartsolar.vreg import CHAR_UUIDS

from fakes import A, advert_data, run


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

        import smartsolar.ble.agent as agent

        monkeypatch.setattr(agent, "registered_agent", NoAgent)
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
    from smartsolar.ble.agent import PasskeyAgent

    # dbus-fast wraps the methods; the plain functions are behind __wrapped__
    passkey = PasskeyAgent.RequestPasskey.__wrapped__
    pin = PasskeyAgent.RequestPinCode.__wrapped__
    assert passkey(PasskeyAgent(0), "/org/bluez/hci0/dev_x") == 0 and pin(PasskeyAgent(0), "/x") == "000000"
    assert passkey(PasskeyAgent(123456), "/x") == 123456 and pin(PasskeyAgent(42), "/x") == "000042"


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
