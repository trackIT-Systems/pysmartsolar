import asyncio
import json
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import TEST_KEY, advert_data
from smartsolar import cli
from smartsolar.errors import NoDataError
from smartsolar.server import ControlServer

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
VALUE = {"name": "battery_voltage", "friendly": "Battery voltage", "unit": "V", "value": 13.31}
BLOCK_OK = {"interval": None, "enabled": True, "stale_after_s": 120, "last_ok": None, "age_s": 3.2, "stale": False, "last_error": None}
STATUS = {
    "version": "1.1.0",
    "uptime_s": 7500,
    "device": {
        "address": "F7:48:18:8A:77:9D", "name": "SmartSolar HQ22294ZGEU", "rssi": -47, "model": "0xa053", "pinned": False,
        "key": {"source": "config", "state": "ok"}, "last_advert_age_s": 0.4, "adverts_per_min": 245.0,
    },
    "devices_seen": 3,
    "blocks": {"advert": BLOCK_OK, "gatt": {**BLOCK_OK, "enabled": False, "age_s": None, "stale": False}},
    "advert": {"seen": 1200, "decoded": 1195, "no_key": 0, "rejected": 5, "undecodable": 0},
    "gatt": {
        "mode": "off", "state": "idle", "connected": False, "connected_since": None, "connect_attempts": 0, "connects": 0,
        "disconnects": 0, "pairings": 0, "polls": 0, "last_poll": None, "next_poll_in_s": None, "last_error": None,
        "backoff_s": None, "last_key_fetch": None,
    },
}


def status(**changes):
    out = json.loads(json.dumps(STATUS))
    for path, value in changes.items():
        target = out
        *parents, last = path.split(".")
        for p in parents:
            target = target[p]
        target[last] = value
    return out


# -- formatting ---------------------------------------------------------------


def lines_of(text):
    return {l.split("  ", 1)[0].strip(): l.split("  ", 1)[1].strip() for l in text.splitlines()}


def test_format_status_advert_only():
    lines = lines_of(cli.format_status(STATUS, NOW))
    assert lines["device"] == "F7:48:18:8A:77:9D  SmartSolar HQ22294ZGEU  rssi -47 dBm  model 0xa053"
    assert lines["key"] == "config, ok"
    assert lines["advert"] == "ok 3s ago   245/min, 1195 decoded, 5 rejected"
    assert lines["gatt"] == "off"
    assert "gatt data" not in lines and lines["daemon"] == "smartsolard 1.1.0, up 2h05m"


def test_format_status_poll_and_hold():
    poll = status(**{"gatt.mode": "poll", "gatt.polls": 4, "gatt.next_poll_in_s": 540, "gatt.connect_attempts": 6, "gatt.connects": 4, "gatt.disconnects": 4, "blocks.gatt.enabled": True, "blocks.gatt.age_s": 120.0})
    lines = lines_of(cli.format_status(poll, NOW))
    assert lines["gatt"] == "poll, idle, 4 poll(s), next in 9m   6 attempts, 4 connects, 4 disconnects"
    assert lines["gatt data"] == "ok 2m ago"
    hold = status(**{"gatt.mode": "hold", "gatt.state": "connected", "gatt.connected": True, "gatt.connected_since": (NOW - timedelta(minutes=5)).isoformat()})
    assert lines_of(cli.format_status(hold, NOW))["gatt"] == "hold, connected since 5m"


def test_format_status_failures_and_missing_data():
    failing = status(**{"gatt.mode": "hold", "gatt.state": "backoff", "gatt.connect_attempts": 9, "gatt.last_error": "connect timed out", "gatt.backoff_s": 12.3})
    assert lines_of(cli.format_status(failing, NOW))["gatt"] == "hold, backoff   9 attempts, 0 connects, 0 disconnects, last error: connect timed out, retrying in 12.3s"
    quiet = status(device=None)
    assert lines_of(cli.format_status(quiet, NOW))["device"] == "none (3 Victron device(s) seen)"
    nodata = status(**{"blocks.advert.age_s": None, "advert.no_key": 7, "device.key": {"source": "missing", "state": "missing"}})
    lines = lines_of(cli.format_status(nodata, NOW))
    assert lines["advert"].startswith("no data yet") and "7 no key" in lines["advert"] and lines["key"] == "missing, missing"


def test_format_values_shows_not_available():
    assert cli.format_values([VALUE, {**VALUE, "name": "pv_power", "unit": "W", "value": None}, {"name": "device_state", "unit": "", "value": "float"}]).splitlines() == [
        "NAME             VALUE  UNIT",
        "battery_voltage  13.31  V",
        "pv_power         n/a    W",
        "device_state     float",
    ]


def test_format_cached_and_read():
    cached = {"device": "AA", "blocks": {"advert": {"enabled": True, "time": "t", "age_s": 61, "stale": True, "values": [VALUE]}, "gatt": {"enabled": False, "time": None, "age_s": None, "stale": False, "values": []}}}
    text = cli.format_cached(cached)
    assert "advert (1m old, stale)" in text and "gatt: not in use (gatt.mode is off)" in text
    empty = {"blocks": {"advert": {"enabled": True, "time": None, "age_s": None, "stale": True, "values": []}}}
    assert "advert: no data yet" in cli.format_cached(empty)
    text = cli.format_read({"blocks": {"advert": {"time": "t", "values": [VALUE]}, "gatt": {"error": "gatt.mode is off"}}})
    assert "advert (live)" in text and "gatt: gatt.mode is off" in text


def test_format_scan():
    scan = {"devices": [
        {"address": "AA", "name": "SmartSolar", "rssi": -50, "model": "0xa053", "record_type": 1, "solar_charger": True, "key": "config", "age_s": 0.3, "current": True},
        {"address": "BB", "name": None, "rssi": -80, "model": None, "record_type": None, "solar_charger": False, "key": "missing", "age_s": 12.0, "current": False},
    ]}
    text = cli.format_scan(scan)
    assert "solar charger" in text and "type None" in text and text.splitlines()[1].rstrip().endswith("*")
    assert cli.format_scan({"devices": []}) == "no Victron devices seen yet"


# -- commands against a stub daemon -----------------------------------------------


class StubDaemon:
    """A real ControlServer with canned handlers, in a background thread."""

    def __init__(self, handlers):
        self.dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="ss-"))
        self.sock = self.dir / "s.sock"
        self.calls = []
        self.handlers = {name: self._wrap(name, h) for name, h in handlers.items()}
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _wrap(self, name, handler):
        async def wrapped(args):
            self.calls.append((name, args))
            if isinstance(handler, Exception):
                raise handler
            return handler

        return wrapped

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.server = ControlServer(self.sock, 0o600, self.handlers)
        self.loop.run_until_complete(self.server.start())
        self.ready.set()
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        return self

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def run_cli(daemon, *argv):
    return cli.main(["--socket", str(daemon.sock), *argv])


def test_status_command(capsys):
    with StubDaemon({"status": STATUS}) as d:
        assert run_cli(d, "status") == 0
    out = capsys.readouterr().out
    assert "F7:48:18:8A:77:9D" in out and "smartsolard 1.1.0" in out


def test_json_flag_works_before_and_after_the_command(capsys):
    with StubDaemon({"status": STATUS}) as d:
        assert cli.main(["status", "--json", "--socket", str(d.sock)]) == 0
        after = json.loads(capsys.readouterr().out)
        assert run_cli(d, "--json", "status") == 0
        before = json.loads(capsys.readouterr().out)
    assert after == before == STATUS


def test_cached_read_and_the_block_argument(capsys):
    cached = {"device": "AA", "blocks": {"advert": {"enabled": True, "time": "t", "age_s": 5, "stale": False, "values": [VALUE]}}}
    live = {"device": "AA", "blocks": {"advert": {"time": "t", "values": [VALUE]}}}
    with StubDaemon({"cached": cached, "read": live}) as d:
        assert run_cli(d, "cached", "advert") == 0
        assert d.calls[-1] == ("cached", {"block": "advert"}) and "advert (5s old)" in capsys.readouterr().out
        assert run_cli(d, "read") == 0
        assert d.calls[-1] == ("read", {"block": "all", "raw": False}) and "advert (live)" in capsys.readouterr().out
        with pytest.raises(SystemExit):
            run_cli(d, "read", "status")  # not a block


def test_read_raw_lists_the_registers(capsys):
    live = {"device": "AA", "blocks": {"gatt": {"time": "t", "values": [VALUE]}}, "raw": {"0xed8d": "3305", "0x0093": "1027"}}
    with StubDaemon({"read": live}) as d:
        assert run_cli(d, "read", "gatt", "--raw") == 0
        assert d.calls[-1] == ("read", {"block": "gatt", "raw": True})
        out = capsys.readouterr().out
        assert "raw registers (hex)" in out and "0xed8d  3305" in out
        assert run_cli(d, "read", "gatt") == 0 and d.calls[-1][1]["raw"] is False


def test_scan_rescan_reconnect_and_fetch_key(capsys):
    scan = {"devices": [{"address": "AA", "name": "S", "rssi": -50, "model": "0xa053", "record_type": 1, "solar_charger": True, "key": "config", "age_s": 1.0, "current": True}]}
    with StubDaemon({"scan": scan, "rescan": scan, "reconnect": {"requested": True}, "fetch_key": {"address": "AA", "fetched": True, "key": "00" * 16}}) as d:
        assert run_cli(d, "scan") == 0 and "AA" in capsys.readouterr().out
        assert run_cli(d, "rescan") == 0 and d.calls[-1][0] == "rescan"
        capsys.readouterr()
        assert run_cli(d, "reconnect") == 0 and "reconnect requested" in capsys.readouterr().out
        assert run_cli(d, "fetch-key") == 0
        out = capsys.readouterr().out
        assert d.calls[-1] == ("fetch_key", {"show": False}) and "fetched and stored" in out and "00" * 16 not in out
        assert run_cli(d, "fetch-key", "--show") == 0
        assert d.calls[-1] == ("fetch_key", {"show": True}) and "key: " + "00" * 16 in capsys.readouterr().out


def test_daemon_errors_give_exit_code_1(capsys):
    with StubDaemon({"read": NoDataError("gatt.mode is off")}) as d:
        assert run_cli(d, "read", "gatt") == 1
        assert "gatt.mode is off" in capsys.readouterr().err


def test_unreachable_daemon_gives_exit_code_3(capsys, tmp_path):
    assert cli.main(["--socket", "/tmp/smartsolar-test-none.sock", "status"]) == 3
    assert "is smartsolard running" in capsys.readouterr().err
    assert cli.main(["--socket", str(tmp_path / ("x" * 120) / "none.sock"), "status"]) == 3
    assert "cannot connect" in capsys.readouterr().err


def test_decode_works_offline(capsys, tmp_path):
    data = advert_data(nonce=77, state=3, voltage=13.5, pv_w=60).hex()
    assert cli.main(["decode", "--key", TEST_KEY.hex(), data]) == 0
    out = capsys.readouterr().out
    assert "model 0xa053" in out and "nonce 77" in out and "battery_voltage" in out and "13.5" in out and "bulk" in out
    keyfile = tmp_path / "key"
    keyfile.write_text(TEST_KEY.hex() + "\n")
    assert cli.main(["decode", "--key-file", str(keyfile), "--json", data]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {v["name"]: v["value"] for v in result["values"]}["pv_power"] == 60


def test_decode_errors(capsys):
    data = advert_data().hex()
    assert cli.main(["decode", data]) == 2 and "--key" in capsys.readouterr().err
    assert cli.main(["decode", "--key", "zz", data]) == 2
    assert cli.main(["decode", "--key", TEST_KEY.hex(), "nothex"]) == 2 and "hex" in capsys.readouterr().err
    assert cli.main(["decode", "--key", "aa" + TEST_KEY.hex()[2:], data]) == 1 and "does not match" in capsys.readouterr().err
    assert cli.main(["decode", "--key", TEST_KEY.hex(), "1002"]) == 1


def test_vregs_works_offline(capsys):
    assert cli.main(["vregs"]) == 0
    out = capsys.readouterr().out
    assert "0xed8d" in out and "channel_1_voltage" in out and "VERIFIED" in out
    assert cli.main(["vregs", "--json"]) == 0 and json.loads(capsys.readouterr().out)[0]["id"].startswith("0x")


WRITE_OK = {"device": "AA", "written": [{"name": "device_mode", "requested": "off", "old": "on", "value": "off", "ok": True}]}


def test_write_with_yes_sends_the_request_and_shows_old_and_new(capsys):
    with StubDaemon({"write": WRITE_OK}) as d:
        assert run_cli(d, "write", "device_mode=off", "--yes") == 0
        assert d.calls[-1] == ("write", {"values": {"device_mode": "off"}})
        assert capsys.readouterr().out.split() == ["device_mode", "on", "->", "off", "ok"]
        assert run_cli(d, "write", "device_mode=off", "-y", "--json") == 0
        assert json.loads(capsys.readouterr().out)["written"][0]["ok"] is True


def test_write_reports_a_mismatch_with_exit_code_1(capsys):
    bad = {"device": "AA", "written": [{"name": "float_voltage", "requested": 13.6, "old": 13.5, "value": 13.5, "ok": False}]}
    with StubDaemon({"write": bad}) as d:
        assert run_cli(d, "write", "float_voltage=13.6", "--yes") == 1
        assert "MISMATCH" in capsys.readouterr().out


def test_write_asks_for_confirmation(capsys, monkeypatch):
    with StubDaemon({"write": WRITE_OK}) as d:
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
        assert run_cli(d, "write", "device_mode=off") == 2 and d.calls == []  # not a terminal and no --yes
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert run_cli(d, "write", "device_mode=off") == 1 and d.calls == []
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert run_cli(d, "write", "device_mode=off") == 0 and len(d.calls) == 1
    assert "device_mode = off" in capsys.readouterr().out


def test_write_is_validated_before_contacting_the_daemon(capsys):
    with StubDaemon({"write": WRITE_OK}) as d:
        for argv, message in (
            (["device_mode=sleep"], "is not one of"),
            (["battery_voltage=12"], "read-only"),
            (["nonsense=1"], "unknown register"),
            (["float_voltage=lots"], "not a number"),
            (["float_voltage=500"], "outside"),
            (["oops"], "NAME=VALUE"),
        ):
            assert run_cli(d, "write", *argv, "--yes") == 2
            assert message in capsys.readouterr().err
        assert d.calls == []


def test_write_errors_from_the_daemon(capsys):
    from smartsolar.errors import WriteFailed

    with StubDaemon({"write": WriteFailed("register 0xedab: the device did not confirm the write")}) as d:
        assert run_cli(d, "write", "load_output_control=off", "--yes") == 1
        assert "did not confirm" in capsys.readouterr().err


def test_vregs_writable_lists_the_allowed_values(capsys):
    assert cli.main(["vregs", "--writable"]) == 0
    out = capsys.readouterr().out
    assert "device_mode" in out and "on, off" in out and "conventional1" in out and "float_voltage" in out and "8..70" in out
    assert cli.main(["vregs", "--writable", "--json"]) == 0
    assert {r["name"] for r in json.loads(capsys.readouterr().out)} >= {"device_mode", "load_output_control"}


def test_socket_comes_from_an_explicit_config(tmp_path):
    config = tmp_path / "c.yml"
    config.write_text("daemon:\n  socket: /tmp/from-config.sock\n")
    args = cli.build_parser().parse_args(["--config", str(config), "status"])
    assert cli.resolve_socket(args) == Path("/tmp/from-config.sock")
    assert cli.main(["--config", str(tmp_path / "missing.yml"), "status"]) == 2


def test_help_lists_the_commands(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    for command in ("status", "cached", "read", "reconnect", "rescan", "scan", "fetch-key", "decode", "vregs", "write"):
        assert command in out
    with pytest.raises(SystemExit) as exit_:
        cli.main([])
    assert exit_.value.code == 2


# -- profiles (VictronConnect settings files) -------------------------------------

from pathlib import Path as _P  # noqa: E402

from smartsolar import profiles  # noqa: E402

DATA = _P(__file__).parent / "data" / "ve-mppt-75-15-lifepo4.vcsf"
CONTEXT = {"product_id": 0xA053, "firmware": 0x164, "system_voltage": 12, "voltage_range": [8.0, 17.4]}


def settings(**changes):
    values = {
        "battery_type": "user", "battery_max_current": 15.0, "absorption_voltage": 14.5, "float_voltage": 13.5, "equalisation_voltage": 0.0,
        "temperature_compensation": 0.0, "absorption_time_limit": 2.0, "adaptive_mode": "off", "auto_equalise_days": 0,
        "equalisation_current_percent": 0, "equalisation_duration": 2.0, "equalisation_auto_stop": "off", "rebulk_voltage_offset": 0.1,
        "tail_current": 0.0, "low_temperature_level": 5.0, "low_temperature_current": None, "load_output_control": "conventional1",
        "load_user_high_voltage": 14.0, "load_user_low_voltage": 10.0, "load_aes_timer": 30, "rx_port_function": "load-config", "tx_port_function": "communication",
    }
    values.update(changes)
    return {"device": "AA", **CONTEXT, "values": [{"name": n, "friendly": n, "unit": "", "value": v} for n, v in values.items()]}


def test_profile_show_works_offline(capsys, tmp_path):
    assert cli.main(["--socket", str(tmp_path / "none.sock"), "profile", "show", str(DATA)]) == 0
    out = capsys.readouterr().out
    assert "product 0xA053, firmware from 0x163, 12 V system" in out and "float_voltage" in out and "conventional1" in out
    assert "not supported and not imported" in out
    assert cli.main(["profile", "show", str(DATA), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["product_id"] == 0xA053 and data["values"]["absorption_voltage"] == 14.5 and data["system_voltage"] == 12
    assert cli.main(["profile", "show", str(tmp_path / "missing.vcsf")]) == 2 and "cannot read" in capsys.readouterr().err
    (tmp_path / "bad.vcsf").write_bytes(b"not a settings file")
    assert cli.main(["profile", "show", str(tmp_path / "bad.vcsf")]) == 2 and "VCCF" in capsys.readouterr().err


def test_profile_diff_and_dry_run_write_nothing(capsys):
    with StubDaemon({"settings": settings(float_voltage=13.8, load_output_control="on"), "write": {"written": []}}) as d:
        assert run_cli(d, "profile", "diff", str(DATA)) == 0
        out = capsys.readouterr().out
        import re

        assert "2 of 22 value(s) differ" in out
        assert re.search(r"float_voltage\s+13\.8\s+->\s+13\.5", out) and re.search(r"load_output_control\s+on\s+->\s+conventional1", out)
        assert run_cli(d, "profile", "load", str(DATA), "--dry-run") == 0
        assert [c[0] for c in d.calls] == ["settings", "settings"]


def test_profile_with_nothing_to_change(capsys):
    with StubDaemon({"settings": settings()}) as d:
        assert run_cli(d, "profile", "load", str(DATA), "--yes") == 0
        assert "nothing to change" in capsys.readouterr().out and [c[0] for c in d.calls] == ["settings"]


def test_profile_load_writes_only_the_differences_in_order_and_verifies(capsys):
    ok = {"written": [{"name": "battery_type", "requested": "user", "old": "preset1", "value": "user", "ok": True}]}
    with StubDaemon({"settings": settings(battery_type="preset1", float_voltage=13.8, tail_current=0.5), "write": ok}) as d:
        assert run_cli(d, "profile", "load", str(DATA), "--yes") == 0
        values = d.calls[-1][1]["values"]
        assert list(values) == ["battery_type", "float_voltage", "tail_current"]  # the battery type first
        assert values == {"battery_type": "user", "float_voltage": 13.5, "tail_current": 0.0}
        assert "written and verified: 3 value(s)" in capsys.readouterr().out


def test_profile_load_for_another_product_needs_ignore_device(capsys):
    other = {**settings(float_voltage=13.8), "product_id": 0xA042, "firmware": 0x100}
    ok = {"written": [{"name": "float_voltage", "requested": 13.5, "old": 13.8, "value": 13.5, "ok": True}]}
    with StubDaemon({"settings": other, "write": ok}) as d:
        assert run_cli(d, "profile", "load", str(DATA), "--yes") == 2
        err = capsys.readouterr().err
        assert "product 0xA053" in err and "--ignore-device" in err
        assert "write" not in [c[0] for c in d.calls]
        assert run_cli(d, "profile", "load", str(DATA), "--yes", "--ignore-device") == 0
        assert "warning: the profile is for product" in capsys.readouterr().err
        assert d.calls[-1][0] == "write"


def test_profile_load_reports_a_mismatch(capsys):
    bad = {"written": [{"name": "float_voltage", "requested": 13.5, "old": 13.8, "value": 13.8, "ok": False}]}
    with StubDaemon({"settings": settings(float_voltage=13.8), "write": bad}) as d:
        assert run_cli(d, "profile", "load", str(DATA), "--yes") == 1
    assert "float_voltage (wanted 13.5, got 13.8)" in capsys.readouterr().err


def test_profile_load_asks_for_confirmation(capsys, monkeypatch):
    ok = {"written": [{"name": "float_voltage", "requested": 13.5, "old": 13.8, "value": 13.5, "ok": True}]}
    with StubDaemon({"settings": settings(float_voltage=13.8), "write": ok}) as d:
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
        assert run_cli(d, "profile", "load", str(DATA)) == 2
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert run_cli(d, "profile", "load", str(DATA)) == 1 and "write" not in [c[0] for c in d.calls]
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert run_cli(d, "profile", "load", str(DATA)) == 0
    assert "float_voltage = 13.5 V" in capsys.readouterr().out


def test_profile_for_another_device_is_refused_before_anything_is_written(capsys, tmp_path):
    other_product = {**settings(float_voltage=13.8), "product_id": 0xA042}
    old_firmware = {**settings(float_voltage=13.8), "firmware": 0x150}
    other_system = {**settings(float_voltage=13.8), "system_voltage": 24}
    for live, message in ((other_product, "is for product 0xA053, the device is 0xA042"), (old_firmware, "needs firmware 0x163 or newer"), (other_system, "a 12 V system, the device runs at 24 V")):
        with StubDaemon({"settings": live, "write": {"written": []}}) as d:
            assert run_cli(d, "profile", "load", str(DATA), "--yes") == 2
            assert message in capsys.readouterr().err and "write" not in [c[0] for c in d.calls]


def test_a_preset_battery_type_leaves_out_the_charge_voltages(capsys, tmp_path):
    path = tmp_path / "preset.vcsf"
    values = {"battery_type": "preset1", "absorption_voltage": 14.3, "float_voltage": 13.8, "tail_current": 0.5}
    path.write_bytes(profiles.dump_profile(values, product_id=0xA053, firmware=0x164, system_voltage=12))
    with StubDaemon({"settings": settings(), "write": {"written": []}}) as d:
        assert run_cli(d, "profile", "diff", str(path)) == 0
        out = capsys.readouterr().out
        assert "note: the battery type is the preset preset1" in out and "absorption_voltage, float_voltage" in out
        assert "battery_type" in out and "tail_current" in out and "13.8" not in out.split("note:")[1].split("\n", 1)[1]


def test_profile_save_writes_a_settings_file_that_can_be_read_back(capsys, tmp_path):
    out = tmp_path / "saved.vcsf"
    with StubDaemon({"settings": settings(float_voltage=13.7)}) as d:
        assert run_cli(d, "profile", "save", str(out), "--description", "mine") == 0
        assert "not yet checked with VictronConnect" in capsys.readouterr().out
        assert run_cli(d, "profile", "save", str(out)) == 2 and "--force" in capsys.readouterr().err
        assert run_cli(d, "profile", "save", str(out), "--force") == 0
    p = profiles.load_profile(out)
    assert p.values["float_voltage"] == 13.7 and p.values["load_output_control"] == "conventional1" and p.product_id == 0xA053 and p.system_voltage == 12
    assert p.description == "" and p.min_firmware == 0x164


def test_format_status_with_the_cable_and_without_bluetooth():
    serial = {
        "port": "/dev/ttyUSBvd0", "state": "open", "connected": True, "hex": True, "serial": "HQ22294ZGEU", "product_id": "0xA053", "firmware": "1.64",
        "last_block_age_s": 0.4, "opens": 1, "losses": 0, "blocks": 120, "bad_checksum": 2, "resyncs": 0, "hex_frames": 5, "bad_hex": 0,
        "hex_requests": 3, "hex_timeouts": 0, "last_error": None, "backoff_s": None, "unknown_fields": {},
    }
    lines = lines_of(cli.format_status(status(serial=serial, ble=False), NOW))
    assert lines["ble"] == "disabled" and "gatt" not in lines and "device" not in lines
    assert lines["vedirect"] == "/dev/ttyUSBvd0 open, HQ22294ZGEU product 0xA053 firmware 1.64, hex on   last block 0s ago, 120 blocks, 2 bad checksum"
    assert lines_of(cli.format_status(status(serial={**serial, "hex": False, "state": "backoff", "last_error": "cannot open", "last_block_age_s": None}), NOW))["vedirect"].startswith("/dev/ttyUSBvd0 backoff")
    assert "vedirect" not in lines_of(cli.format_status(STATUS, NOW))
