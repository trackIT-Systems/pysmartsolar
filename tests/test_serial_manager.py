import asyncio
import dataclasses
import logging
from contextlib import asynccontextmanager

import pytest
from fakes import SERIAL_NUMBER, FakeSerial, FakeSerialOpener, run, until

from smartsolar.config import SerialConfig
from smartsolar.errors import InvalidWriteError, NoDataError, NotConnectedError, WriteFailed
from smartsolar.serial_manager import SerialManager
from smartsolar.state import Cache
from smartsolar.vreg import VREG_FIRMWARE, build_write, firmware_version, product_id

CFG = SerialConfig(enabled=True, request_timeout_s=0.2, reopen_initial_s=0.01, reopen_max_s=0.02)


class Setup:
    def __init__(self, manager, cache, events, serials, opener):
        self.manager, self.cache, self.events, self.serials, self.opener = manager, cache, events, serials, opener

    def kinds(self):
        return [e.kind for e in self.events]


@asynccontextmanager
async def running(*devices, cfg=CFG):
    cache = Cache({"advert": None, "gatt": None, "vedirect": 10.0})
    events, serials = [], []
    opener = FakeSerialOpener(*devices)
    manager = SerialManager(cfg, cache=cache, emit_event=events.append, opener=opener, on_serial=serials.append)
    task = asyncio.create_task(manager.run())
    try:
        if devices:
            await until(lambda: manager.connected)
        yield Setup(manager, cache, events, serials, opener)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def with_two_blocks(device):
    device.block()
    device.block()


def test_text_blocks_become_readings_of_the_serial_number():
    async def go():
        dev = FakeSerial()
        async with running(dev) as s:
            with_two_blocks(dev)
            await until(lambda: s.cache.blocks["vedirect"].reading is not None)
            reading = s.cache.blocks["vedirect"].reading
            assert reading.device == SERIAL_NUMBER and reading.block == "vedirect"
            values = {v.name: v.value for v in reading.values}
            assert values["battery_voltage"] == 13.32 and values["pv_power"] == 2 and values["device_state"] == "bulk"
            assert s.serials == [SERIAL_NUMBER]
            assert s.manager.product_id == 0xA053 and s.manager.firmware == 0x164
            assert "serial_opened" in s.kinds() and "serial_device" in s.kinds()
            d = s.manager.describe()
            assert d["connected"] and d["serial"] == SERIAL_NUMBER and d["firmware"] == "1.64" and d["blocks"] >= 1

    run(go())


def test_another_device_on_the_cable_clears_the_old_reading():
    async def go():
        a, b = FakeSerial(), FakeSerial()
        async with running(a, b) as s:
            with_two_blocks(a)
            await until(lambda: s.cache.blocks["vedirect"].reading is not None)
            a.unplug()
            await until(lambda: s.opener.current is b)
            b.push(__import__("fakes").text_block([(k, "HQ11111OTHR" if k == "SER#" else v) for k, v in __import__("fakes").TEXT_FIELDS]) * 2)
            await until(lambda: s.serials == [SERIAL_NUMBER, "HQ11111OTHR"])
            assert s.cache.blocks["vedirect"].reading.device == "HQ11111OTHR"

    run(go())


def test_the_port_is_reopened_after_it_goes_away():
    async def go():
        a, b = FakeSerial(), FakeSerial()
        async with running(a, b) as s:
            with_two_blocks(a)
            await until(lambda: s.manager.connected)
            a.unplug()
            await until(lambda: s.opener.current is b and s.manager.connected)
            assert a.closed and s.manager.stats.losses == 1 and s.manager.stats.opens == 2
            assert "serial_lost" in s.kinds()
            b.block(); b.block()
            await until(lambda: s.manager.last_block_mono is not None)

    run(go())


def test_a_missing_port_is_retried_until_it_appears():
    async def go():
        async with running() as s:
            await until(lambda: len(s.opener.opened) >= 3)
            assert s.manager.state == "backoff" and not s.manager.connected and "serial_lost" in s.kinds()
            dev = FakeSerial()
            s.opener.devices.append(dev)
            await until(lambda: s.manager.connected)

    run(go())


def test_a_missing_cable_is_reported_once_not_at_every_retry(caplog):
    async def go():
        with caplog.at_level("INFO"):
            async with running() as s:
                await until(lambda: len(s.opener.opened) >= 5)
                assert s.kinds().count("serial_lost") == 1
                assert sum("cannot open" in r.message for r in caplog.records) == 1  # the retries are DEBUG
                dev = FakeSerial()
                s.opener.devices.append(dev)
                await until(lambda: s.manager.connected)
                dev.unplug()
                await until(lambda: s.kinds().count("serial_lost") == 3)  # the loss, then the port missing: each reported once
                await until(lambda: len(s.opener.opened) >= 9)
                assert s.kinds().count("serial_lost") == 3

    run(go())


def serial_logs(caplog, text):
    return [r.levelname for r in caplog.records if r.name == "smartsolar.serial_manager" and text in r.getMessage()]


@pytest.mark.parametrize(("port", "level"), [("/dev/ttyUSBvd*", "INFO"), ("/dev/ttyUSB0", "WARNING")])
def test_a_missing_cable_is_info_for_a_pattern_and_a_warning_for_a_fixed_port(caplog, port, level):
    async def go():
        with caplog.at_level(logging.DEBUG, logger="smartsolar.serial_manager"):
            async with running(cfg=dataclasses.replace(CFG, port=port)) as s:
                await until(lambda: len(s.opener.opened) >= 4)
                dev = FakeSerial()
                s.opener.devices.append(dev)
                await until(lambda: s.manager.connected)
        assert serial_logs(caplog, "is not available")[:4] == [level, "DEBUG", "DEBUG", "DEBUG"]
        assert serial_logs(caplog, "works again") == ["INFO"]
        assert serial_logs(caplog, "Opened serial port") == []  # the recovery says it

    run(go())


def test_a_lost_cable_is_logged_once_and_its_return_once(caplog):
    async def go():
        a, b = FakeSerial(), FakeSerial()
        with caplog.at_level(logging.DEBUG, logger="smartsolar.serial_manager"):
            async with running(a, b) as s:
                with_two_blocks(a)
                await until(lambda: s.serials == [SERIAL_NUMBER])
                a.unplug()
                await until(lambda: s.opener.current is b and s.manager.connected)
                b.block(); b.block()
                await until(lambda: s.manager.stats.opens == 2 and s.manager.last_reading is not None)
        assert serial_logs(caplog, "Opened serial port") == ["INFO"]
        assert serial_logs(caplog, "Lost serial port") == ["INFO"]  # the default port is a pattern: a cable if there is one
        assert serial_logs(caplog, "works again") == ["INFO"]
        assert serial_logs(caplog, "Serial number") == ["INFO"]  # the same device again

    run(go())


def test_registers_are_read_in_the_ble_layout():
    async def go():
        dev = FakeSerial()
        async with running(dev) as s:
            got = await s.manager.read([0x0100, VREG_FIRMWARE, 0xEDF6, 0x1234])
            assert product_id(got[0x0100]) == 0xA053 and firmware_version(got[VREG_FIRMWARE]) == 0x164
            assert got[0xEDF6] == bytes.fromhex("4605") and 0x1234 not in got  # the unknown register is left out

    run(go())


def test_reading_nothing_is_an_error():
    async def go():
        async with running(FakeSerial()) as s:
            with pytest.raises(NoDataError):
                await s.manager.read([0x1234])

    run(go())


def test_a_lost_answer_is_asked_for_again_but_silence_is_not_an_answer():
    async def go():
        dev = FakeSerial()
        async with running(dev) as s:
            dev.swallow = 1
            assert (await s.manager.read([0xEDF6]))[0xEDF6] == bytes.fromhex("4605")
            assert s.manager.stats.hex_timeouts == 1
            dev.silent = True
            with pytest.raises(NoDataError):
                await s.manager.read([0xEDF6])

    run(go())


def test_reads_do_not_disturb_the_text_blocks():
    async def go():
        dev = FakeSerial()
        async with running(dev) as s:
            async def blocks():
                for _ in range(20):
                    dev.block()
                    await asyncio.sleep(0.005)
            producer = asyncio.create_task(blocks())
            got = await s.manager.read([0xEDF6, 0xEDF7])
            await producer
            assert set(got) == {0xEDF6, 0xEDF7}
            await until(lambda: s.manager.describe()["blocks"] >= 15)

    run(go())


def test_a_write_is_checked_written_and_read_back():
    async def go():
        dev = FakeSerial()
        async with running(dev) as s:
            results = await s.manager.write(build_write({"float_voltage": 13.8}))
            assert results == [{"name": "float_voltage", "requested": 13.8, "old": 13.5, "value": 13.8, "ok": True}]
            assert dev.registers[0xEDF6] == bytes.fromhex("6405")
            assert "register_written" in s.kinds()

    run(go())


def test_a_refused_write_is_reported():
    async def go():
        dev = FakeSerial()
        dev.refuse_flags = 0x04
        async with running(dev) as s:
            with pytest.raises(WriteFailed, match="refused"):
                await s.manager.write(build_write({"float_voltage": 13.8}))
            assert dev.registers[0xEDF6] == bytes.fromhex("4605")

    run(go())


def test_writes_that_must_not_happen_send_no_set_frame():
    async def go():
        dev = FakeSerial()
        async with running(dev) as s:
            with pytest.raises(WriteFailed, match="cut this connection"):
                await s.manager.write(build_write({"tx_port_function": "communication"}))
            dev.registers[0xEDF1] = b"\x01"  # a preset battery type
            with pytest.raises(WriteFailed, match="preset"):
                await s.manager.write(build_write({"float_voltage": 13.8}))
            dev.registers[0xEDF1] = b"\xff"
            with pytest.raises(InvalidWriteError, match="error 119"):
                await s.manager.write(build_write({"float_voltage": 17.5}))
            assert not any(w.startswith(b":8") for w in dev.writes)

    run(go())


def test_text_only_mode_never_transmits():
    async def go():
        dev = FakeSerial()
        async with running(dev, cfg=SerialConfig(enabled=True, hex=False, reopen_initial_s=0.01, reopen_max_s=0.02)) as s:
            with_two_blocks(dev)
            await until(lambda: s.cache.blocks["vedirect"].reading is not None)
            with pytest.raises(NotConnectedError, match="read only"):
                await s.manager.read([0xEDF6])
            with pytest.raises(NotConnectedError):
                await s.manager.write(build_write({"float_voltage": 13.8}))
            assert dev.writes == []

    run(go())


def test_requests_fail_when_the_port_is_closed():
    async def go():
        async with running() as s:
            with pytest.raises(NotConnectedError, match="not open"):
                await s.manager.read([0xEDF6])

    run(go())


def test_a_request_in_flight_fails_when_the_cable_is_pulled():
    async def go():
        dev = FakeSerial()
        dev.silent = True
        async with running(dev, cfg=SerialConfig(enabled=True, request_timeout_s=2.0, reopen_initial_s=0.01, reopen_max_s=0.02)) as s:
            await until(lambda: s.manager.connected)
            task = asyncio.create_task(s.manager.read([0xEDF6]))
            await asyncio.sleep(0.05)
            dev.unplug()
            with pytest.raises(NotConnectedError):
                await task

    run(go())
