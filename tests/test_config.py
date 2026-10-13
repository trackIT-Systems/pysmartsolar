from dataclasses import replace
from pathlib import Path

import pytest

from fakes import TEST_KEY
from smartsolar.config import DEFAULT_SOCKET_PATH, DEFAULT_STATE_DIR, AppConfig, load_config, parse_config
from smartsolar.errors import ConfigError


def test_defaults():
    config = parse_config({})
    assert config == AppConfig()
    assert config.gatt.mode == "off" and config.gatt.passkey == "000000"
    assert config.daemon.socket == DEFAULT_SOCKET_PATH and config.daemon.socket_mode == 0o660
    assert config.advert.publish_interval_s == 60 and config.daemon.state_dir == DEFAULT_STATE_DIR


def test_full_config():
    config = parse_config(
        {
            "ble": {"adapter": "hci1", "address": "f7:48:18:8a:77:9d", "select_window_s": 0, "switch_hysteresis_db": 6},
            "advert": {"publish_interval_s": 30, "stale_after_s": 90},
            "gatt": {"mode": "poll", "passkey": "123456", "poll_interval_s": 600},
            "keys": {"f7:48:18:8a:77:9d": TEST_KEY.hex()},
            "daemon": {"socket": "/tmp/s.sock", "socket_mode": "0600", "state_dir": "/tmp/st", "log_level": "debug", "csv": {"enabled": False}, "mqtt": {"cbor": False}},
        }
    )
    assert config.ble.address == "F7:48:18:8A:77:9D" and config.ble.adapter == "hci1" and config.ble.select_window_s == 0
    assert config.gatt.mode == "poll" and config.gatt.passkey == "123456"
    assert config.keys == {"F7:48:18:8A:77:9D": TEST_KEY}
    assert config.daemon.socket_mode == 0o600 and config.daemon.log_level == "DEBUG" and not config.daemon.csv.enabled and not config.daemon.mqtt.cbor


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"nope": 1}, "Unknown top-level"),
        ({"gatt": {"mode": "always"}}, "gatt.mode"),
        ({"gatt": {"passkey": "12345678"}}, "gatt.passkey"),
        ({"gatt": {"passkey": "abc"}}, "gatt.passkey"),
        ({"gatt": {"poll_interval_s": 0}}, "poll_interval_s"),
        ({"gatt": {"bogus": 1}}, "Unknown key"),
        ({"ble": {"switch_hysteresis_db": -1}}, "switch_hysteresis_db"),
        ({"advert": {"stale_after_s": "soon"}}, "must be a number"),
        ({"keys": {"not a mac": "00" * 16}}, "not a MAC address, a device UUID"),
        ({"keys": {"AA:BB:CC:DD:EE:FF": "1234"}}, "32 hex digits"),
        ({"keys": {"AA:BB:CC:DD:EE:FF": 12}}, "32 hex digits"),
        ({"keys": []}, "mapping"),
        ({"daemon": {"log_level": "loud"}}, "log_level"),
        ({"daemon": {"socket_mode": "rw"}}, "octal"),
    ],
)
def test_rejects(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_an_unquoted_passkey_is_taken_as_six_digits():
    assert parse_config({"gatt": {"passkey": 0}}).gatt.passkey == "000000"
    assert parse_config({"gatt": {"passkey": 42}}).gatt.passkey == "000042"


def test_key_errors_do_not_contain_the_key():
    bad = "zz" + TEST_KEY.hex()[2:]
    with pytest.raises(ConfigError) as err:
        parse_config({"keys": {"AA:BB:CC:DD:EE:FF": bad}})
    assert bad not in str(err.value) and TEST_KEY.hex() not in str(err.value)


def test_keys_are_not_in_the_repr():
    config = parse_config({"keys": {"AA:BB:CC:DD:EE:FF": TEST_KEY.hex()}})
    assert TEST_KEY.hex() not in repr(config) and str(TEST_KEY) not in repr(config)


def test_an_unquoted_off_is_off():
    assert parse_config({"gatt": {"mode": False}}).gatt.mode == "off"
    with pytest.raises(ConfigError, match="gatt.mode"):
        parse_config({"gatt": {"mode": True}})


def test_load_config(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("gatt:\n  mode: hold\n  passkey: 000000\n")
    assert load_config(path).gatt.mode == "hold" and load_config(path).gatt.passkey == "000000"
    path.write_text("- a\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)
    path.write_text("a: [")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(path)
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")


def test_the_example_config_is_valid_and_equals_the_defaults():
    example = Path(__file__).parents[1] / "config" / "smartsolar.yml"
    config = load_config(example)
    assert config.gatt.mode == "off" and config.keys == {}
    # the example shows the Linux (tsOS) paths; the defaults of other systems differ
    assert config == replace(AppConfig(path=example), daemon=replace(AppConfig().daemon, socket=Path("/run/smartsolar/smartsolar.sock"), state_dir=Path("/var/lib/smartsolar"), csv=replace(AppConfig().daemon.csv, path=Path("/data"))))


def test_the_serial_section_and_ble_off():
    config = parse_config({"ble": {"enabled": False}, "serial": {"enabled": True, "port": "/dev/ttyUSB1", "hex": False, "publish_interval_s": 30}})
    assert not config.ble.enabled and config.serial.enabled and config.serial.port == "/dev/ttyUSB1" and not config.serial.hex
    assert config.serial.baud == 19200 and config.serial.publish_interval_s == 30
    assert parse_config({}).serial.enabled is False and parse_config({}).daemon.registers == "auto"


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"ble": {"enabled": False}}, "nothing to do"),
        ({"serial": {"enabled": True, "baud": 0}}, "baud"),
        ({"serial": {"bogus": 1}}, "Unknown key"),
        ({"daemon": {"registers": "radio"}}, "daemon.registers"),
        ({"daemon": {"registers": "serial"}}, "serial.enabled and serial.hex"),
        ({"serial": {"enabled": True, "hex": False}, "daemon": {"registers": "serial"}}, "serial.enabled and serial.hex"),
        ({"ble": {"enabled": False}, "serial": {"enabled": True}, "daemon": {"registers": "gatt"}}, "ble.enabled is false"),
    ],
)
def test_rejects_serial_and_ble_combinations(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_default_paths_per_platform(tmp_path):
    from smartsolar.platform import default_paths

    linux = default_paths("linux")
    assert linux.config == Path("/boot/firmware/smartsolar.yml") and linux.socket == Path("/run/smartsolar/smartsolar.sock") and linux.data_dir == Path("/data")
    mac = default_paths("darwin", home=tmp_path)
    assert mac.state_dir == tmp_path / "Library" / "Application Support" / "smartsolar" and mac.socket == mac.state_dir / "smartsolar.sock" and mac.data_dir == mac.state_dir / "data"
    win = default_paths("win32", env={"PROGRAMDATA": "C:/ProgramData"})
    assert win.state_dir == Path("C:/ProgramData/smartsolar") and win.socket == "tcp://127.0.0.1:47821" and win.config.name == "smartsolar.yml"
    assert default_paths("win32", env={}, home=tmp_path).state_dir == tmp_path / "AppData" / "Local" / "smartsolar"


def test_the_control_socket_can_be_a_loopback_tcp_endpoint():
    assert parse_config({"daemon": {"socket": "tcp://127.0.0.1:5000"}}).daemon.socket == "tcp://127.0.0.1:5000"
    for bad, message in (("tcp://0.0.0.0:5000", "loopback"), ("tcp://[::1]:5000", "tcp://127.0.0.1:port"), ("tcp://example.org:5000", "tcp://127.0.0.1:port"), ("tcp://127.0.0.1:0", "port"), ("tcp://127.0.0.1", "tcp://127.0.0.1:port")):
        with pytest.raises(ConfigError, match=message):
            parse_config({"daemon": {"socket": bad}})


def test_devices_can_be_named_by_mac_serial_or_uuid():
    key = TEST_KEY.hex()
    uuid = "6D2F8A3C-1B4E-4F7A-9C0D-5E8B7A6F1234"
    config = parse_config({"keys": {"hq22294zgeu": key, uuid: key, "F7:48:18:8A:77:9D": key}, "ble": {"address": "HQ22294ZGEU"}})
    assert set(config.keys) == {"HQ22294ZGEU", uuid, "F7:48:18:8A:77:9D"} and config.ble.address == "HQ22294ZGEU"
    with pytest.raises(ConfigError, match="serial number"):
        parse_config({"ble": {"address": "kitchen"}})
