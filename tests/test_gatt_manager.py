import asyncio

import pytest

from fakes import A, B, TEST_KEY, FakeBackend, advert, connect_error, run, until
from smartsolar.config import GattConfig
from smartsolar.errors import NoDataError, NotConnectedError, PairingError, WriteFailed
from smartsolar.gatt_manager import GattManager, GattState
from smartsolar.state import GattStats
from smartsolar.vreg import VREG_KEY, build_write


class Rig:
    def __init__(self, mode, *, have_key=True, backend=None, device=A, **cfg):
        self.backend = backend or FakeBackend()
        self.events, self.readings, self.keys = [], [], ({A: TEST_KEY, B: TEST_KEY} if have_key else {})
        self.current = {"adv": advert(device) if device else None}
        defaults = dict(keepalive_s=0.05, collect_s=2, connect_timeout_s=1, pair_timeout_s=1, request_timeout_s=1,
                        backoff_initial_s=0.01, backoff_max_s=0.02, publish_interval_s=0.1, poll_interval_s=0.3)
        self.cfg = GattConfig(mode=mode, **{**defaults, **cfg})
        self.stats = GattStats()
        self.mgr = GattManager(
            self.cfg, self.backend, lambda: self.current["adv"], lambda a: a in self.keys, self.keys.__setitem__,
            self.events.append, self.readings.append, self.stats, rng=lambda: 1.0,
        )
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.mgr.run())
        return self

    async def __aexit__(self, *exc):
        self.mgr.stop()
        await asyncio.wait_for(self.task, 3.0)

    def kinds(self):
        return [e.kind for e in self.events]


def test_mode_off_with_a_known_key_never_connects():
    async def go():
        async with Rig("off") as rig:
            await asyncio.sleep(0.3)
            assert rig.backend.links == [] and rig.mgr.state is GattState.IDLE

    run(go())


def test_mode_off_fetches_a_missing_key_once_and_disconnects():
    async def go():
        async with Rig("off", have_key=False) as rig:
            await until(lambda: "disconnected" in rig.kinds())
            assert rig.keys == {A: TEST_KEY}
            assert rig.kinds() == ["paired", "connected", "key_fetched", "disconnected"]
            (link,) = rig.backend.links
            assert link.disconnect_calls == 1 and rig.backend.pair_calls == ["000000"]
            await asyncio.sleep(0.3)
            assert len(rig.backend.links) == 1 and rig.readings == []  # nothing else to do, nothing published
            assert rig.stats.connects == 1 and rig.stats.pairings == 1
            assert all(TEST_KEY.hex() not in str(e.data) for e in rig.events)

    run(go())


def test_the_configured_passkey_is_used():
    async def go():
        async with Rig("off", have_key=False, passkey="123456") as rig:
            await until(lambda: "key_fetched" in rig.kinds())
            assert rig.backend.pair_calls == ["123456"] and rig.backend.connect_passkeys == ["123456"]

    run(go())


def test_request_key_fetches_even_when_the_key_is_known():
    async def go():
        async with Rig("off") as rig:
            key = await asyncio.wait_for(rig.mgr.request_key(), 3.0)
            assert key == TEST_KEY and rig.kinds().count("key_fetched") == 1
            await until(lambda: rig.mgr.state is GattState.IDLE)

    run(go())


def test_poll_mode_publishes_the_decoded_values_and_disconnects():
    async def go():
        async with Rig("poll") as rig:
            await until(lambda: len(rig.readings) >= 1)
            reading = rig.readings[0]
            values = {v.name: v.value for v in reading.values}
            assert reading.block == "gatt" and reading.device == A
            assert values["channel_1_voltage"] == 13.31 and values["firmware_version"] == "00640100"
            assert values["mac_address"] == "f7:48:18:8a:77:9d" and values["battery_temperature"] is None
            assert values["uptime"] == 1000
            assert "vreg_ec65" not in values and TEST_KEY.hex() not in str([v.value for v in reading.values])
            assert all(not n.startswith("vreg_") for n in values)  # unknown registers are not published
            await until(lambda: rig.backend.links[0].disconnect_calls == 1)
            await until(lambda: len(rig.readings) >= 2)  # the next poll after poll_interval_s
            assert len(rig.backend.links) >= 2 and rig.stats.polls >= 2

    run(go())


def test_poll_mode_does_not_poll_before_the_interval():
    async def go():
        async with Rig("poll", poll_interval_s=60) as rig:
            await until(lambda: len(rig.readings) == 1)
            await asyncio.sleep(0.4)
            assert len(rig.readings) == 1 and rig.mgr.next_poll_mono is not None

    run(go())


def test_request_poll_connects_now():
    async def go():
        async with Rig("poll", poll_interval_s=60) as rig:
            await until(lambda: len(rig.readings) == 1)
            reading = await asyncio.wait_for(rig.mgr.request_poll(), 3.0)
            assert reading.block == "gatt" and len(rig.readings) == 2 and len(rig.backend.links) == 2

    run(go())


def test_request_poll_needs_poll_mode():
    async def go():
        for mode in ("off", "hold"):
            async with Rig(mode) as rig:
                with pytest.raises(NoDataError, match="needs mode poll"):
                    rig.mgr.request_poll()

    run(go())


def test_hold_mode_keeps_one_link_and_publishes_regularly():
    async def go():
        backend = FakeBackend()
        backend.keepalive_timeout = 0.5
        async with Rig("hold", backend=backend) as rig:
            await until(lambda: len(rig.readings) >= 4)
            assert len(backend.links) == 1 and backend.links[0].is_connected and rig.mgr.state is GattState.CONNECTED
            assert rig.mgr.snapshot().block == "gatt"
            backend.links[0].push(0xED8D, (1400).to_bytes(2, "little"))
            await until(lambda: any({v.name: v.value for v in r.values}.get("channel_1_voltage") == 14.0 for r in rig.readings))

    run(go())


def test_hold_mode_reconnects_after_the_device_closed_the_link():
    async def go():
        async with Rig("hold") as rig:
            await until(lambda: len(rig.backend.links) == 1 and rig.mgr.state is GattState.CONNECTED)
            rig.backend.links[0].drop_remote()
            await until(lambda: len(rig.backend.links) == 2 and rig.mgr.state is GattState.CONNECTED)
            assert rig.kinds().count("connected") == 2 and "disconnected" in rig.kinds() and "gatt_failed" in rig.kinds()

    run(go())


def test_request_reconnect_skips_the_backoff():
    async def go():
        async with Rig("hold", backoff_initial_s=5, backoff_max_s=5) as rig:
            await until(lambda: rig.mgr.state is GattState.CONNECTED)
            rig.mgr.request_reconnect()
            await until(lambda: len(rig.backend.links) == 2 and rig.mgr.state is GattState.CONNECTED, timeout=2.0)
            assert rig.backend.links[0].disconnect_calls == 1 and "gatt_failed" not in rig.kinds()

    run(go())


def test_a_changed_device_moves_the_connection():
    async def go():
        async with Rig("hold") as rig:
            await until(lambda: rig.mgr.state is GattState.CONNECTED)
            rig.current["adv"] = advert(B)
            rig.mgr.device_changed()
            await until(lambda: len(rig.backend.links) == 2 and rig.mgr.state is GattState.CONNECTED)
            assert [l.address for l in rig.backend.links] == [A, B]

    run(go())


def test_failed_connects_are_retried_with_backoff():
    async def go():
        backend = FakeBackend()
        backend.connect_errors = [connect_error(), connect_error("InProgress")]
        async with Rig("poll", backend=backend) as rig:
            await until(lambda: len(rig.readings) >= 1)
            assert rig.stats.connect_attempts == 3 and rig.kinds().count("gatt_failed") == 2
            assert rig.stats.last_error == "InProgress" and rig.stats.connects == 1
            assert rig.stats.backoff_s is None  # reset after the success

    run(go())


def test_a_pairing_failure_is_reported_and_retried():
    async def go():
        backend = FakeBackend()
        backend.pair_error = PairingError("pairing rejected, is the passkey right?")
        async with Rig("off", have_key=False, backend=backend) as rig:
            await until(lambda: rig.kinds().count("gatt_failed") >= 2)
            assert rig.keys == {} and rig.stats.connects == 0
            assert "passkey" in rig.stats.last_error and rig.stats.pairings == 0
            assert all(l.disconnect_calls == 1 for l in backend.links[:-1])  # no link left behind
            backend.pair_error = None
            await until(lambda: rig.keys == {A: TEST_KEY})

    run(go())


def test_a_key_fetch_that_gets_no_answer_fails_and_retries():
    async def go():
        backend = FakeBackend()
        del backend.values[VREG_KEY]
        async with Rig("off", have_key=False, backend=backend, request_timeout_s=0.2) as rig:
            await until(lambda: rig.kinds().count("gatt_failed") >= 2, timeout=4.0)
            assert "0xec65" in rig.stats.last_error
            backend.values[VREG_KEY] = TEST_KEY
            await until(lambda: rig.keys == {A: TEST_KEY}, timeout=4.0)

    run(go())


def test_waits_for_a_selected_device():
    async def go():
        async with Rig("poll", device=None) as rig:
            await asyncio.sleep(0.2)
            assert rig.backend.links == [] and rig.mgr.state is GattState.WAITING
            rig.current["adv"] = advert(A)
            rig.mgr.poke()
            await until(lambda: rig.readings, timeout=4.0)  # picks it up within the idle wait

    run(go())


def test_stop_aborts_a_connect_and_fails_pending_requests():
    async def go():
        backend = FakeBackend()
        backend.connect_delay = 10
        rig = Rig("off", have_key=False, backend=backend)
        async with rig:
            future = rig.mgr.request_key()
            await until(lambda: rig.mgr.state is GattState.CONNECTING)
        assert rig.task.done() and rig.mgr.state is GattState.STOPPED
        with pytest.raises(NotConnectedError):
            future.result()

    run(go())


def test_stop_closes_an_open_link():
    async def go():
        async with Rig("hold") as rig:
            await until(lambda: rig.mgr.state is GattState.CONNECTED)
        assert rig.backend.links[0].disconnect_calls == 1 and rig.kinds()[-1] == "disconnected"
        assert rig.events[-1].data["reason"] == "shutdown"

    run(go())


def test_unknown_registers_are_only_available_raw():
    async def go():
        backend = FakeBackend()
        backend.values[0xED8C] = bytes.fromhex("d8ffffff")
        async with Rig("poll", backend=backend) as rig:
            await until(lambda: rig.readings)
            assert "vreg_ed8c" not in {v.name for v in rig.readings[0].values}
            raw = rig.mgr.raw_registers()  # still there after the session closed
            assert raw["0xed8c"] == "d8ffffff" and raw["0xed8d"] == "3305"
            assert "0xec65" not in raw and TEST_KEY.hex() not in str(raw)

    run(go())


def test_poll_mode_publishes_the_registers_read_after_the_init_sequence():
    async def go():
        async with Rig("poll") as rig:
            await until(lambda: rig.readings)
            values = {v.name: v.value for v in rig.readings[0].values}
            assert values["battery_voltage"] == 13.31 and values["device_mode"] == "on" and values["device_state"] == "off"
            assert values["load_output_control"] == "conventional1" and values["float_voltage"] == 13.5 and values["battery_max_current"] == 15.0
            assert values["yield_total"] == 98.47 and values["charger_temperature"] == 28.8 and values["charger_error"] == "none"

    run(go())


def test_registers_the_device_refuses_are_not_asked_for_again():
    async def go():
        backend = FakeBackend()
        del backend.registers[0xEDD7]
        async with Rig("hold", backend=backend) as rig:
            await until(lambda: len(rig.readings) >= 3)
            assert backend.get_log.count(0xEDD7) == 1 and backend.get_log.count(0xEDD5) >= 3

    run(go())


def test_a_write_in_mode_off_connects_writes_reads_back_and_disconnects():
    async def go():
        async with Rig("off") as rig:
            results = await asyncio.wait_for(rig.mgr.request_write(build_write({"device_mode": "off", "float_voltage": 13.51})), 3.0)
            assert [(r["name"], r["old"], r["value"], r["ok"]) for r in results] == [("device_mode", "on", "off", True), ("float_voltage", 13.5, 13.51, True)]
            assert rig.backend.registers[0x0200] == b"\x04" and rig.backend.registers[0xEDF6] == bytes.fromhex("4705")
            await until(lambda: rig.mgr.state is GattState.IDLE)
            assert len(rig.backend.links) == 1 and rig.backend.links[0].disconnect_calls == 1
            written = [e for e in rig.events if e.kind == "register_written"]
            assert [(e.data["name"], e.data["old"], e.data["new"], e.data["ok"]) for e in written] == [("device_mode", "on", "off", True), ("float_voltage", 13.5, 13.51, True)]
            assert rig.readings == []  # a write does not publish a reading in mode off

    run(go())


def test_a_write_in_hold_mode_uses_the_open_link():
    async def go():
        async with Rig("hold") as rig:
            await until(lambda: rig.mgr.state is GattState.CONNECTED)
            (result,) = await asyncio.wait_for(rig.mgr.request_write(build_write({"load_output_control": "off"})), 3.0)
            assert result["old"] == "conventional1" and result["value"] == "off" and result["ok"]
            assert len(rig.backend.links) == 1 and rig.backend.links[0].is_connected
            await until(lambda: any({v.name: v.value for v in r.values}.get("load_output_control") == "off" for r in rig.readings))

    run(go())


def test_a_refused_write_fails_the_request_but_keeps_the_link():
    async def go():
        backend = FakeBackend()
        backend.refuse_writes = True
        async with Rig("hold", backend=backend) as rig:
            await until(lambda: rig.mgr.state is GattState.CONNECTED)
            with pytest.raises(WriteFailed, match="did not confirm"):
                await asyncio.wait_for(rig.mgr.request_write(build_write({"device_mode": "off"})), 5.0)
            assert rig.mgr.state is GattState.CONNECTED and len(backend.links) == 1
            backend.refuse_writes = False
            (result,) = await asyncio.wait_for(rig.mgr.request_write(build_write({"device_mode": "off"})), 5.0)
            assert result["ok"]

    run(go())


def test_a_write_the_device_reads_back_differently_is_reported():
    async def go():
        backend = FakeBackend()
        async with Rig("hold", backend=backend) as rig:
            await until(lambda: rig.mgr.state is GattState.CONNECTED)
            link = backend.links[0]
            original = link._answer_requests

            def clamp(data):  # the device stores another value than the one it echoes
                original(data)
                if backend.set_log and backend.set_log[-1][0] == 0xEDF6:
                    backend.registers[0xEDF6] = bytes.fromhex("4605")

            link._answer_requests = clamp
            with pytest.raises(WriteFailed, match="reports 13.5"):
                await asyncio.wait_for(rig.mgr.request_write(build_write({"float_voltage": 13.6})), 5.0)
            assert [e.data["ok"] for e in rig.events if e.kind == "register_written"] == [False]

    run(go())


def test_pending_writes_fail_when_the_manager_stops():
    async def go():
        backend = FakeBackend()
        backend.connect_delay = 10
        rig = Rig("off", backend=backend)
        async with rig:
            future = rig.mgr.request_write(build_write({"device_mode": "off"}))
            await until(lambda: rig.mgr.state is GattState.CONNECTING)
        with pytest.raises(NotConnectedError):
            future.result()

    run(go())


def test_request_read_connects_on_demand_and_returns_raw_values():
    async def go():
        async with Rig("off") as rig:
            values = await asyncio.wait_for(rig.mgr.request_read([0xEDF6, 0xEDF7]), 3.0)
            assert values == {0xEDF6: bytes.fromhex("4605"), 0xEDF7: bytes.fromhex("aa05")}
            await until(lambda: rig.mgr.state is GattState.IDLE)
            assert len(rig.backend.links) == 1 and rig.backend.links[0].disconnect_calls == 1 and rig.backend.set_log == []

    run(go())


def test_request_read_of_an_unavailable_register_fails_the_request_only():
    async def go():
        async with Rig("hold") as rig:
            await until(lambda: rig.mgr.state is GattState.CONNECTED)
            with pytest.raises(Exception, match="not available"):
                await asyncio.wait_for(rig.mgr.request_read([0xBEEF]), 3.0)
            assert rig.mgr.state is GattState.CONNECTED

    run(go())
