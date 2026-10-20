import asyncio
import csv
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path

import cbor2
import pytest

from fakes import A, B, TEST_KEY, FakeBackend, FakeClock, FakeMqtt, FakeSerialOpener, advert, free_port, run, until
from smartsolar import daemon as daemon_module
from smartsolar import ipc
from smartsolar.config import AdvertConfig, AppConfig, BleConfig, CsvConfig, DaemonConfig, GattConfig, MqttConfig, SerialConfig
from smartsolar.daemon import Daemon
from smartsolar.errors import DaemonError
from smartsolar.outputs import Outputs
from smartsolar.outputs.csv import CsvOutput
from smartsolar.outputs.mqtt import MqttOutput

TOPIC = "tracker/smartsolar/f748188a779d"


class Env:
    """A daemon wired to the fake backend, with real socket and real CSV/MQTT sinks."""

    def __init__(self, *, mode="off", keys="default", ble=True, serial=None, registers="auto", hex=True, topic_id=None, tcp=False, **gatt):
        # a short directory: macOS limits the length of a Unix socket path
        self.dir = Path(tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="ss-"))
        self.tcp = tcp or sys.platform.startswith("win")  # Windows has no usable Unix sockets
        self.sock = f"tcp://127.0.0.1:{free_port()}" if self.tcp else self.dir / "run" / "s.sock"
        self.csv_dir = self.dir / "data" / "tracker" / "smartsolar"
        self.state_dir = self.dir / "state"
        self.mqtt = FakeMqtt()
        self.backend = FakeBackend()
        gatt_cfg = dict(keepalive_s=0.05, collect_s=2, connect_timeout_s=1, pair_timeout_s=1, request_timeout_s=1,
                        backoff_initial_s=0.01, backoff_max_s=0.02, publish_interval_s=0.1, poll_interval_s=30)
        gatt_cfg.update(gatt)
        self.opener = FakeSerialOpener(*(serial or []))
        self.ble = ble
        self.topic = f"tracker/smartsolar/{topic_id}" if topic_id else TOPIC
        self.config = AppConfig(
            ble=BleConfig(enabled=ble, select_window_s=0),
            advert=AdvertConfig(publish_interval_s=0.05, stale_after_s=1.0),
            gatt=GattConfig(mode=mode, **gatt_cfg),
            keys={A: TEST_KEY} if keys == "default" else keys,
            serial=SerialConfig(enabled=serial is not None, hex=hex, publish_interval_s=0.05, stale_after_s=1.0, request_timeout_s=0.5,
                                reopen_initial_s=0.01, reopen_max_s=0.02),
            daemon=DaemonConfig(socket=self.sock, state_dir=self.state_dir, registers=registers, csv=CsvConfig(path=self.dir), mqtt=MqttConfig(publish_interval_s=0)),
        )
        clock = FakeClock(synced=True)
        outputs = Outputs([
            CsvOutput(self.dir / "data", hostname="tracker", clock=clock),
            MqttOutput(self.config.daemon.mqtt, hostname="tracker", client=self.mqtt, clock=clock),
        ])
        self.daemon = Daemon(self.config, backend=self.backend if ble else None, outputs=outputs, hostname="tracker", serial_opener=self.opener)
        self.task = None
        self.exit_code = None
        self.nonce = 0

    async def __aenter__(self):
        self.task = asyncio.create_task(self.daemon.run(handle_signals=False))
        await until(lambda: self._listening() and (self.backend.scanning or not self.ble))
        return self

    def _listening(self) -> bool:
        if not self.tcp:
            return self.sock.exists()
        try:
            socket.create_connection((self.daemon.server.endpoint.host, self.daemon.server.endpoint.port), timeout=0.2).close()
            return True
        except OSError:
            return False

    async def __aexit__(self, *exc):
        await self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def stop(self):
        if self.exit_code is None:
            self.daemon.stop_event.set()
            self.exit_code = await asyncio.wait_for(self.task, 5)

    def advertise(self, address=A, n=1, **kw):
        for _ in range(n):
            self.nonce += 1
            self.backend.emit(advert(address, nonce=self.nonce, **kw))

    async def select(self, address=A, **kw):
        self.advertise(address, **kw)
        await until(lambda: self.daemon.tracker.current_address == address)

    async def call(self, cmd, **args):
        return await asyncio.to_thread(ipc.request, self.sock, cmd, args, timeout=5)

    def csv_rows(self, kind="readings"):
        """The rows of today's CSV files (readings, or the events)."""
        files = sorted((self.csv_dir / "events" if kind == "events" else self.csv_dir).glob("*.csv"))
        return [row for path in files for row in csv.DictReader(path.read_text().splitlines(), delimiter=";")]

    def published(self, suffix):
        return [p for t, p, _ in self.mqtt.published if t == f"{self.topic}/{suffix}"]


def test_advertisements_are_decoded_published_and_the_daemon_stops_cleanly():
    async def go():
        async with Env() as env:
            await env.select(voltage=13.31, state=5, pv_w=40)
            status = await env.call("status")
            assert status["device"]["address"] == A and status["device"]["key"] == {"source": "config", "state": "unknown"}
            env.advertise(n=3, voltage=13.32, state=5, pv_w=41)
            await until(lambda: len(env.published("json")) >= 1)
            assert (await env.call("status"))["device"]["key"]["state"] == "ok"

            cached = await env.call("cached")
            values = {v["name"]: v["value"] for v in cached["blocks"]["advert"]["values"]}
            assert values["battery_voltage"] == 13.32 and values["device_state"] == "float" and values["pv_power"] == 41
            assert cached["blocks"]["gatt"]["enabled"] is False and cached["blocks"]["gatt"]["values"] == []
            assert list((await env.call("cached", block="advert"))["blocks"]) == ["advert"]

            live = await env.call("read", block="advert")
            assert live["blocks"]["advert"]["values"] == cached["blocks"]["advert"]["values"]
            allblocks = await env.call("read")
            assert "error" in allblocks["blocks"]["gatt"] and "values" in allblocks["blocks"]["advert"]
        assert env.exit_code == 0
        assert env.backend.links == [] and not env.backend.scanning

    run(go())


def test_outputs_have_the_expected_content():
    async def go():
        env = Env()
        async with env:
            await env.select(voltage=13.31)
            env.advertise(n=2, voltage=13.4)
            await until(lambda: len(env.published("cbor")) >= 1)
            rows = env.csv_rows()
            events = env.csv_rows("events")
            await env.stop()
            states = [json.loads(p) for t, p, _ in env.mqtt.published if t == "tracker/smartsolar/state/json"]
            assert states[0]["state"] == "running" and states[-1]["state"] == "offline"
            kinds = {json.loads(p)["event"] for t, p, _ in env.mqtt.published if t == "tracker/smartsolar/events/json"}
            assert {"daemon_started", "device_selected", "daemon_stopped"} <= kinds
            as_json, as_cbor = json.loads(env.published("json")[0]), cbor2.loads(env.published("cbor")[0])
            assert list(as_json.values())[1:] == as_cbor[1:] and as_json["battery_voltage"] in (13.31, 13.4)
            assert "source" not in as_json and "block" not in as_json
            assert any(r["block"] == "advert" and r["battery_voltage"] in ("13.31", "13.4") for r in rows)
            assert any(r["event"] == "device_selected" for r in events)
            assert not any("key" in str(p).lower() and TEST_KEY.hex() in str(p) for _, p, _ in env.mqtt.published)

    run(go())


def test_the_same_reading_is_not_published_twice():
    async def go():
        async with Env() as env:
            await env.select()
            env.advertise()
            await until(lambda: len(env.published("json")) == 1)
            await asyncio.sleep(0.3)  # many publish intervals, but no new advertisement
            assert len(env.published("json")) == 1
            env.advertise()
            await until(lambda: len(env.published("json")) == 2)

    run(go())


def test_a_stale_reading_is_not_published_or_served():
    async def go():
        async with Env() as env:
            await env.select()
            env.advertise()
            await until(lambda: len(env.published("json")) == 1)
            await asyncio.sleep(1.2)  # stale_after_s is 1
            status = await env.call("status")
            assert status["blocks"]["advert"]["stale"] is True
            with pytest.raises(DaemonError) as err:
                await env.call("read", block="advert")
            assert err.value.type == "no_data" and "no advertisement" in err.value.message

    run(go())


def test_a_missing_key_is_fetched_over_gatt_stored_and_then_used():
    async def go():
        async with Env(keys={}) as env:
            await env.select()
            await until(lambda: (env.state_dir / "keys.json").exists())
            assert sys.platform.startswith("win") or stat.S_IMODE((env.state_dir / "keys.json").stat().st_mode) == 0o600
            assert json.loads((env.state_dir / "keys.json").read_text()) == {A: TEST_KEY.hex()}
            assert env.backend.pair_calls == ["000000"] and len(env.backend.links) == 1
            env.advertise(n=2, voltage=13.5)
            await until(lambda: env.daemon.cache.blocks["advert"].reading is not None)
            status = await env.call("status")
            assert status["device"]["key"]["source"] == "fetched" and status["gatt"]["last_key_fetch"] is not None
            assert status["gatt"]["mode"] == "off" and status["gatt"]["connects"] == 1
            await asyncio.sleep(0.2)
            assert len(env.backend.links) == 1  # not again

    run(go())


def test_a_fetched_key_survives_a_restart():
    async def go():
        env = Env(keys={})
        async with env:
            await env.select()
            await until(lambda: (env.state_dir / "keys.json").exists())
            await env.stop()
            from smartsolar.keys import KeyStore

            assert KeyStore({}, env.state_dir).get(A)[0] == TEST_KEY

    run(go())


def test_fetch_key_command():
    async def go():
        async with Env() as env:
            await env.select()
            result = await env.call("fetch_key")
            assert result == {"address": A, "fetched": True}  # the key is not in the answer
            shown = await env.call("fetch_key", show=True)
            assert shown["key"] == TEST_KEY.hex()

    run(go())


def test_fetch_key_needs_a_selected_device():
    async def go():
        async with Env() as env:
            with pytest.raises(DaemonError) as err:
                await env.call("fetch_key")
            assert err.value.type == "no_data"

    run(go())


def test_poll_mode_serves_gatt_reads_by_connecting():
    async def go():
        async with Env(mode="poll") as env:
            await env.select()
            await until(lambda: env.daemon.cache.blocks["gatt"].reading is not None)  # the first poll
            first_links = len(env.backend.links)
            live = await env.call("read", block="gatt")
            names = {v["name"]: v["value"] for v in live["blocks"]["gatt"]["values"]}
            assert names["channel_1_voltage"] == 13.31 and len(env.backend.links) == first_links + 1
            cached = await env.call("cached", block="gatt")
            assert cached["blocks"]["gatt"]["enabled"] and cached["blocks"]["gatt"]["values"]
            raw = await env.call("read", block="gatt", raw=True)
            assert raw["raw"]["0xed8d"] == "3305" and "0xec65" not in raw["raw"]
            assert "raw" not in await env.call("read", block="gatt")
            status = await env.call("status")
            assert status["gatt"]["mode"] == "poll" and status["gatt"]["polls"] >= 2 and status["gatt"]["next_poll_in_s"] is not None
            published = [json.loads(p) for p in env.published("json")]
            assert any(m["channel_1_voltage"] == 13.31 for m in published)

    run(go())


def test_hold_mode_keeps_the_link_and_reconnect_renews_it():
    async def go():
        env = Env(mode="hold")
        env.backend.keepalive_timeout = 0.5
        async with env:
            await env.select()
            await until(lambda: env.daemon.gatt.session is not None and env.daemon.cache.blocks["gatt"].reading is not None)
            status = await env.call("status")
            assert status["gatt"]["connected"] and status["gatt"]["state"] == "connected" and len(env.backend.links) == 1
            live = await env.call("read", block="gatt")
            assert live["blocks"]["gatt"]["values"]
            assert await env.call("reconnect") == {"requested": True}
            await until(lambda: len(env.backend.links) == 2 and env.daemon.gatt.session is not None)
            assert env.backend.links[0].disconnect_calls == 1
        assert env.backend.links[1].disconnect_calls == 1  # closed on shutdown

    run(go())


def test_gatt_commands_in_mode_off_explain_themselves():
    async def go():
        async with Env() as env:
            await env.select()
            for cmd, args in (("reconnect", {}), ("read", {"block": "gatt"})):
                with pytest.raises(DaemonError) as err:
                    await env.call(cmd, **args)
                assert err.value.type == "no_data" and "gatt.mode is off" in err.value.message

    run(go())


def test_scan_and_rescan_list_victron_devices_and_rescan_can_switch():
    async def go():
        async with Env(keys={A: TEST_KEY, B: TEST_KEY}) as env:
            await env.select(A, rssi=-70)
            for _ in range(30):
                env.advertise(B, rssi=-45)
                env.advertise(A, rssi=-70)
            scan = await env.call("scan")
            assert [d["address"] for d in scan["devices"]] == [B, A] and [d["current"] for d in scan["devices"]] == [False, True]
            rescan = await env.call("rescan")
            assert [d["current"] for d in rescan["devices"]] == [True, False]
            assert env.daemon.cache.blocks["advert"].reading is None  # the old device's reading is gone

    run(go())


def test_bad_requests_get_clean_errors():
    async def go():
        async with Env() as env:
            for cmd, args, kind in (("cached", {"block": "nope"}, "bad_request"), ("nope", {}, "unknown_command")):
                with pytest.raises(DaemonError) as err:
                    await env.call(cmd, **args)
                assert err.value.type == kind

    run(go())


def test_the_scan_is_restarted_when_the_device_goes_quiet(monkeypatch):
    async def go():
        monkeypatch.setattr(daemon_module, "SCAN_WATCHDOG_S", 0.2)
        async with Env() as env:
            await env.select()
            await until(lambda: env.backend.scan_starts >= 2, timeout=3.0)
            kinds = {json.loads(p)["event"] for t, p, _ in env.mqtt.published if t == "tracker/smartsolar/events/json"}
            assert "scan_restarted" in kinds

    run(go())


def test_a_scan_that_cannot_start_is_retried():
    async def go():
        env = Env()
        original = env.backend.start_scan
        calls = []

        async def flaky(callback):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("adapter is off")
            await original(callback)

        env.backend.start_scan = flaky
        env.task = asyncio.create_task(env.daemon.run(handle_signals=False))
        await until(lambda: env.backend.scanning, timeout=3.0)
        assert len(calls) == 3
        await env.stop()
        shutil.rmtree(env.dir, ignore_errors=True)

    run(go())


def test_write_command_changes_a_register_and_logs_it():
    async def go():
        async with Env() as env:
            await env.select()
            result = await env.call("write", values={"device_mode": "off", "float_voltage": 13.51})
            assert result["device"] == A
            assert [(w["name"], w["old"], w["value"], w["ok"]) for w in result["written"]] == [("device_mode", "on", "off", True), ("float_voltage", 13.5, 13.51, True)]
            assert env.backend.registers[0x0200] == b"\x04"
            events = [json.loads(p) for t, p, _ in env.mqtt.published if t == "tracker/smartsolar/events/json"]
            written = [e for e in events if e["event"] == "register_written"]
            details = [e["detail"] for e in written]
            assert len(details) == 2 and all("ok=True" in d for d in details)
            assert "name=device_mode" in details[0] and "new=off" in details[0]
            assert "name=float_voltage" in details[1] and "new=13.51" in details[1]
            assert any(r["event"] == "register_written" and "name=device_mode" in r["detail"] for r in env.csv_rows("events"))

    run(go())


def test_write_command_errors():
    async def go():
        async with Env() as env:
            with pytest.raises(DaemonError) as err:
                await env.call("write", values={"device_mode": "off"})
            assert err.value.type == "no_data"  # no device selected yet
            await env.select()
            for values, kind in (({"device_mode": "sleep"}, "invalid_write"), ({"battery_voltage": 12}, "invalid_write"), ({}, "bad_request"), ("x", "bad_request")):
                with pytest.raises(DaemonError) as err:
                    await env.call("write", values=values)
                assert err.value.type == kind
            assert env.backend.links == []  # nothing was sent, no connection was opened
            env.backend.refuse_writes = True
            with pytest.raises(DaemonError) as err:
                await env.call("write", values={"device_mode": "off"})
            assert err.value.type == "write_failed" and "did not confirm" in err.value.message

    run(go())


def test_settings_command_reads_the_settings_and_what_a_profile_is_checked_against():
    async def go():
        async with Env() as env:
            with pytest.raises(DaemonError) as err:
                await env.call("settings")
            assert err.value.type == "no_data"
            await env.select()
            result = await env.call("settings")
            values = {v["name"]: v["value"] for v in result["values"]}
            assert values["float_voltage"] == 13.5 and values["load_output_control"] == "conventional1" and values["low_temperature_current"] is None
            assert values["rebulk_voltage_offset"] == 0.1 and values["load_user_high_voltage"] == 14.0 and values["battery_type"] == "user"
            assert result["product_id"] == 0xA053 and result["system_voltage"] == 12 and result["voltage_range"] == [8.0, 17.4]
            assert "device_mode" not in values
            with pytest.raises(DaemonError) as err:
                await env.call("settings", names=["battery_voltage"])
            assert err.value.type == "bad_request"

    run(go())


def test_a_voltage_outside_the_devices_range_is_refused_before_it_is_written():
    async def go():
        async with Env() as env:
            await env.select()
            for name, value in (("float_voltage", 30.0), ("absorption_voltage", 17.5), ("temperature_compensation", 25.0)):
                with pytest.raises(DaemonError) as err:
                    await env.call("write", values={name: value})
                assert err.value.type == "invalid_write" and "error 119" in err.value.message, name
            with pytest.raises(DaemonError) as err:
                await env.call("write", values={"absorption_voltage": 7.9})  # below the lowest value of any system: refused offline
            assert err.value.type == "invalid_write" and "outside 8..70" in err.value.message
            assert env.backend.set_log == []
            await env.call("write", values={"equalisation_voltage": 0})  # 0 means not used
            await env.call("write", values={"float_voltage": 17.4})  # the edge of the range

    run(go())


def test_charge_settings_need_the_user_battery_type():
    async def go():
        async with Env() as env:
            env.backend.registers[0xEDF1] = b"\x01"
            await env.select()
            with pytest.raises(DaemonError) as err:
                await env.call("write", values={"float_voltage": 13.6})
            assert err.value.type == "write_failed" and "battery_type=user" in err.value.message
            assert env.backend.set_log == []
            result = await env.call("write", values={"battery_type": "user", "float_voltage": 13.6})  # in this order the device takes it
            assert [w["ok"] for w in result["written"]] == [True, True]

    run(go())


def test_profile_load_through_the_cli_changes_the_device_and_restores_it(tmp_path, capsys):
    from smartsolar import cli, profiles

    async def go():
        async with Env() as env:
            await env.select()

            def cli_run(*argv):
                return asyncio.to_thread(cli.main, ["--socket", str(env.sock), "profile", *argv])

            original = tmp_path / "original.vcsf"
            assert await cli_run("save", str(original)) == 0
            changed = tmp_path / "changed.vcsf"
            live = profiles.load_profile(original).values | {"float_voltage": 13.8, "tail_current": 0.5, "load_output_control": "conventional2", "adaptive_mode": "on", "low_temperature_current": 5.0}
            changed.write_bytes(profiles.dump_profile(live, product_id=0xA053, firmware=0x164, system_voltage=12))

            assert await cli_run("diff", str(changed)) == 0 and env.backend.set_log == []
            capsys.readouterr()
            assert await cli_run("load", str(changed), "--yes") == 0
            assert [v for v, _ in env.backend.set_log] == [0xEDF6, 0xEDFE, 0xEDE7, 0xEDE6, 0xEDAB]  # the order of the settings table
            assert env.backend.registers[0xEDF6] == bytes.fromhex("6405") and env.backend.registers[0xEDAB] == b"\x03"
            assert env.backend.registers[0xEDE6] == bytes.fromhex("3200")  # 5.0 A in 0.1 A
            assert await cli_run("diff", str(changed)) == 0
            assert "nothing to change" in capsys.readouterr().out.rsplit("written and verified", 1)[-1]

            assert await cli_run("load", str(original), "--yes") == 0  # restore
            assert env.backend.registers[0xEDF6] == bytes.fromhex("4605") and env.backend.registers[0xEDE6] == b"\xff\xff" and env.backend.registers[0xEDAB] == b"\x02"

    run(go())


def test_the_sample_profile_loads_on_the_fake_device_without_changes(capsys):
    from smartsolar import cli

    async def go():
        async with Env() as env:
            await env.select()
            sample = str(Path(__file__).parent / "data" / "ve-mppt-75-15-lifepo4.vcsf")
            assert await asyncio.to_thread(cli.main, ["--socket", str(env.sock), "profile", "diff", sample]) == 0
            assert "nothing to change" in capsys.readouterr().out and env.backend.set_log == []

    run(go())


def test_the_daemon_and_the_cli_work_over_loopback_tcp(capsys):
    from smartsolar import cli

    async def go():
        async with Env(tcp=True) as env:
            await env.select()
            assert str(env.sock).startswith("tcp://127.0.0.1:")
            assert (await env.call("status"))["device"]["address"] == A
            result = await env.call("write", values={"float_voltage": 13.8})
            assert result["written"][0]["ok"] and env.backend.registers[0xEDF6] == bytes.fromhex("6405")
            assert await asyncio.to_thread(cli.main, ["--socket", str(env.sock), "status"]) == 0
            assert A in capsys.readouterr().out

    run(go())
