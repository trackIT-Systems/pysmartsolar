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
    "version": "0.2.0",
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
    assert "gatt data" not in lines and lines["daemon"] == "smartsolard 0.2.0, up 2h05m"


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
    assert "F7:48:18:8A:77:9D" in out and "smartsolard 0.2.0" in out


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
    assert "device_mode" in out and "on, off" in out and "float_voltage" in out and "5..70" in out
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


# -- profiles ---------------------------------------------------------------------

SETTINGS = {"device": "AA", "values": [
    {"name": "float_voltage", "friendly": "Float voltage", "unit": "V", "value": 13.5},
    {"name": "absorption_voltage", "friendly": "Absorption voltage", "unit": "V", "value": 14.5},
    {"name": "battery_max_current", "friendly": "Battery maximum current", "unit": "A", "value": 15.0},
]}


def write_profile(path, **values):
    import yaml

    path.write_text(yaml.safe_dump({"name": "t", "values": values}))
    return str(path)


def test_profile_list_and_show_work_offline(capsys, tmp_path):
    assert cli.main(["--socket", str(tmp_path / "none.sock"), "profile", "list"]) == 0
    out = capsys.readouterr().out
    assert "lifepo4-12v" in out and "absorption_voltage=14.2" in out
    assert cli.main(["profile", "show", "lifepo4-24v", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["values"] == {"absorption_voltage": 28.4, "float_voltage": 27.0}
    assert cli.main(["profile", "show", "nope"]) == 2 and "no premade profile" in capsys.readouterr().err


def test_profile_save(capsys, tmp_path):
    out = tmp_path / "p.yml"
    with StubDaemon({"settings": SETTINGS}) as d:
        assert run_cli(d, "profile", "save", str(out), "--description", "mine") == 0
        text = out.read_text()
        assert "# from device AA" in text and "float_voltage: 13.5" in text and "description: mine" in text
        assert run_cli(d, "profile", "save", str(out)) == 2 and "--force" in capsys.readouterr().err
        assert run_cli(d, "profile", "save", "-") == 0 and "values:" in capsys.readouterr().out.split("saved", 1)[-1]


def test_profile_diff_dry_run_and_nothing_to_change(capsys, tmp_path):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8, absorption_voltage=14.5)
    with StubDaemon({"settings": SETTINGS, "write": {"written": []}}) as d:
        assert run_cli(d, "profile", "diff", path) == 0
        out = capsys.readouterr().out
        assert "1 of 2 value(s) differ" in out and "13.5  ->  13.8" in out and "absorption_voltage" not in out
        assert run_cli(d, "profile", "load", path, "--dry-run") == 0
        same = write_profile(tmp_path / "s.yml", float_voltage=13.5)
        assert run_cli(d, "profile", "load", same, "--yes") == 0 and "nothing to change" in capsys.readouterr().out
        assert [c[0] for c in d.calls] == ["settings", "settings", "settings"]


def test_profile_load_writes_only_the_differences_and_verifies(capsys, tmp_path):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8, absorption_voltage=14.5)
    ok = {"written": [{"name": "float_voltage", "requested": 13.8, "old": 13.5, "value": 13.8, "ok": True}]}
    with StubDaemon({"settings": SETTINGS, "write": ok}) as d:
        assert run_cli(d, "profile", "load", path, "--yes") == 0
        assert d.calls[-1] == ("write", {"values": {"float_voltage": 13.8}})
        assert "written and verified: 1 value(s)" in capsys.readouterr().out


def test_profile_load_reports_a_mismatch(capsys, tmp_path):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8)
    bad = {"written": [{"name": "float_voltage", "requested": 13.8, "old": 13.5, "value": 13.5, "ok": False}]}
    with StubDaemon({"settings": SETTINGS, "write": bad}) as d:
        assert run_cli(d, "profile", "load", path, "--yes") == 1
    assert "float_voltage (wanted 13.8, got 13.5)" in capsys.readouterr().err


def test_profile_load_asks_for_confirmation(capsys, tmp_path, monkeypatch):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8)
    ok = {"written": [{"name": "float_voltage", "requested": 13.8, "old": 13.5, "value": 13.8, "ok": True}]}
    with StubDaemon({"settings": SETTINGS, "write": ok}) as d:
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
        assert run_cli(d, "profile", "load", path) == 2
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert run_cli(d, "profile", "load", path) == 1 and "write" not in [c[0] for c in d.calls]
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert run_cli(d, "profile", "load", path) == 0
    assert "float_voltage = 13.8 V" in capsys.readouterr().out


def test_profile_files_are_validated_before_contacting_the_daemon(capsys, tmp_path):
    with StubDaemon({"settings": SETTINGS}) as d:
        assert run_cli(d, "profile", "load", write_profile(tmp_path / "a.yml", device_mode="off"), "--yes") == 2
        assert run_cli(d, "profile", "load", write_profile(tmp_path / "b.yml", float_voltage=99), "--yes") == 2
        assert d.calls == []
    assert "not a profile setting" in capsys.readouterr().err
