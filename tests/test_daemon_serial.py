"""The daemon with the VE.Direct cable: alone, next to Bluetooth, and as a way to the registers."""

import asyncio
import csv
import json
from pathlib import Path

import pytest
from fakes import A, B, SERIAL_NUMBER, TEST_KEY, FakeSerial, advert, run, until
from test_daemon_e2e import Env

from smartsolar import cli
from smartsolar.errors import DaemonError

SAMPLE = str(Path(__file__).parent / "data" / "ve-mppt-75-15-lifepo4.vcsf")


def serial_env(dev=None, **kw):
    dev = dev or FakeSerial()
    return dev, Env(ble=False, serial=[dev], topic_id=SERIAL_NUMBER, **kw)


def test_the_cable_alone_publishes_under_the_serial_number_and_touches_no_bluetooth():
    async def go():
        dev, env = serial_env()
        async with env:
            dev.block(); dev.block()
            await until(lambda: env.published("json"))
            payload = json.loads(env.published("json")[-1])
            assert payload["source"] == "vedirect" and payload["Battery voltage (V)"] == 13.32 and payload["PV power (W)"] == 2
            assert not env.backend.scanning and env.backend.connect_passkeys == []
            status = await env.call("status")
            assert status["ble"] is False and status["id"] == SERIAL_NUMBER
            assert status["serial"]["connected"] and status["serial"]["hex"] and status["serial"]["firmware"] == "1.64"
            assert status["blocks"]["vedirect"]["enabled"] and not status["blocks"]["advert"]["enabled"]
            read = await env.call("read", block="vedirect")
            assert read["device"] == SERIAL_NUMBER and {v["name"] for v in read["blocks"]["vedirect"]["values"]} >= {"battery_voltage", "panel_voltage", "yield_total"}
            cached = await env.call("cached")
            assert cached["blocks"]["vedirect"]["values"] and cached["blocks"]["gatt"]["enabled"] is False
            with env.csv_path.open() as handle:
                rows = list(csv.DictReader(handle))
            assert any(r["device"] == SERIAL_NUMBER and r["block"] == "vedirect" and r["key"] == "battery_voltage" for r in rows)
            events = [json.loads(p) for t, p, _ in env.mqtt.published if t == "tracker/smartsolar/events/json"]
            assert any(e["event"] == "serial_opened" for e in events)

    run(go())


def test_a_stale_cable_reading_is_not_served():
    async def go():
        dev, env = serial_env()
        async with env:
            dev.block(); dev.block()
            await until(lambda: env.published("json"))
            await asyncio.sleep(1.2)  # stale_after_s is 1
            with pytest.raises(DaemonError, match="no text block"):
                await env.call("read", block="vedirect")

    run(go())


def test_ble_only_commands_explain_themselves_without_bluetooth():
    async def go():
        _, env = serial_env()
        async with env:
            for cmd in ("scan", "rescan", "reconnect", "fetch_key"):
                with pytest.raises(DaemonError, match="ble.enabled is false"):
                    await env.call(cmd)
            with pytest.raises(DaemonError, match="ble.enabled is false"):
                await env.call("read", block="advert")

    run(go())


def test_settings_and_writes_go_over_the_cable():
    async def go():
        dev, env = serial_env()
        async with env:
            dev.block(); dev.block()
            await until(lambda: env.daemon.serial.serial)
            settings = await env.call("settings")
            assert settings["via"] == "serial" and settings["device"] == SERIAL_NUMBER
            assert settings["product_id"] == 0xA053 and settings["firmware"] == 0x164 and settings["system_voltage"] == 12
            assert settings["voltage_range"] == [8.0, 17.4]
            by_name = {v["name"]: v["value"] for v in settings["values"]}
            assert by_name["float_voltage"] == 13.5 and by_name["load_output_control"] == "conventional1"
            result = await env.call("write", values={"float_voltage": 13.8})
            assert result["via"] == "serial" and result["written"][0]["ok"] and dev.registers[0xEDF6] == bytes.fromhex("6405")
            with pytest.raises(DaemonError, match="error 119"):
                await env.call("write", values={"float_voltage": 17.5})
            with pytest.raises(DaemonError, match="cut this connection"):
                await env.call("write", values={"rx_port_function": "load-config"})

    run(go())


def test_the_sample_profile_is_compared_and_loaded_over_the_cable(tmp_path, capsys):
    from smartsolar import profiles

    async def go():
        dev, env = serial_env()
        async with env:
            def cli_run(*argv):
                return asyncio.to_thread(cli.main, ["--socket", str(env.sock), "profile", *argv])

            assert await cli_run("diff", SAMPLE, "--via", "serial") == 0
            assert "nothing to change" in capsys.readouterr().out and not any(w.startswith(b":8") for w in dev.writes)
            changed = tmp_path / "changed.vcsf"
            live = profiles.load_profile(Path(SAMPLE)).values | {"float_voltage": 13.8, "tail_current": 0.5}
            changed.write_bytes(profiles.dump_profile(live, product_id=0xA053, firmware=0x164, system_voltage=12))
            assert await cli_run("load", str(changed), "--yes") == 0
            assert dev.registers[0xEDF6] == bytes.fromhex("6405")
            assert "over the VE.Direct cable" in capsys.readouterr().out
            assert await cli_run("load", SAMPLE, "--yes") == 0  # back
            assert dev.registers[0xEDF6] == bytes.fromhex("4605")

    run(go())


def test_text_only_mode_has_no_way_to_the_registers():
    async def go():
        dev, env = serial_env(hex=False)
        async with env:
            dev.block(); dev.block()
            await until(lambda: env.published("json"))
            with pytest.raises(DaemonError, match="no way to reach the registers"):
                await env.call("settings")
            with pytest.raises(DaemonError, match="not enabled"):
                await env.call("settings", via="serial")
            assert dev.writes == []

    run(go())


def test_auto_falls_back_to_gatt_when_the_cable_is_gone_but_serial_is_strict():
    async def go():
        dev = FakeSerial()
        async with Env(mode="poll", serial=[dev]) as env:
            await env.select()
            dev.block(); dev.block()
            await until(lambda: env.daemon.serial.connected)
            assert (await env.call("settings"))["via"] == "serial"
            dev.unplug()
            await until(lambda: not env.daemon.serial.connected)
            assert (await env.call("settings"))["via"] == "gatt"
            with pytest.raises(DaemonError):
                await env.call("settings", via="serial")
            result = await env.call("write", values={"float_voltage": 13.8})
            assert result["via"] == "gatt" and env.backend.registers[0xEDF6] == bytes.fromhex("6405")

    run(go())


def test_both_interfaces_describe_the_same_device_by_its_serial_number():
    async def go():
        dev = FakeSerial()
        async with Env(serial=[dev], keys={A: TEST_KEY, B: TEST_KEY}, topic_id=SERIAL_NUMBER) as env:
            env.backend.emit(advert(A, rssi=-40, name="SmartSolar HQ99999OTHR", nonce=1))
            env.backend.emit(advert(B, rssi=-70, name=f"SmartSolar {SERIAL_NUMBER}", nonce=1))
            await until(lambda: env.daemon.tracker.current_address == A)
            dev.block(); dev.block()
            await until(lambda: env.daemon.tracker.current_address == B)  # the cable says which one it is
            env.advertise(B, n=3, name=f"SmartSolar {SERIAL_NUMBER}")
            await until(lambda: any(json.loads(p)["source"] == "advert" for p in env.published("json")))
            await until(lambda: any(json.loads(p)["source"] == "vedirect" for p in env.published("json")))
            status = await env.call("status")
            assert status["id"] == SERIAL_NUMBER and status["device"]["address"] == B

    run(go())
