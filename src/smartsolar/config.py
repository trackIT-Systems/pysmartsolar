from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from smartsolar.errors import ConfigError
from smartsolar.instant import KEY_LEN

from smartsolar.platform import DEFAULTS

DEFAULT_CONFIG_PATH = DEFAULTS.config
DEFAULT_SOCKET_PATH = DEFAULTS.socket  # a path, or tcp://127.0.0.1:port where there are no Unix sockets
DEFAULT_STATE_DIR = DEFAULTS.state_dir
DEFAULT_DATA_DIR = DEFAULTS.data_dir
GATT_MODES = ("off", "hold", "poll")
REGISTER_PATHS = ("auto", "serial", "gatt")
DEFAULT_SERIAL_PORT = "/dev/ttyUSBvd*"  # the symlinks of the udev rule of tsOS (ttyUSBvd<n> for ttyUSB<n>)


@dataclass(frozen=True)
class BleConfig:
    enabled: bool = True  # false: no scan and no GATT, the Bluetooth stack is not touched
    adapter: str | None = None
    address: str | None = None  # pin a device; otherwise the strongest SmartSolar is used
    select_window_s: float = 10.0  # listen this long before the first device selection
    rescan_interval_s: float = 3600.0  # how often the selection is reviewed
    switch_hysteresis_db: float = 10.0


@dataclass(frozen=True)
class AdvertConfig:
    publish_interval_s: float = 60.0
    stale_after_s: float = 120.0


@dataclass(frozen=True)
class GattConfig:
    mode: str = "off"  # off: GATT only to fetch a missing key; hold: keep the link; poll: connect now and then
    passkey: str = "000000"
    poll_interval_s: float = 900.0
    collect_s: float = 15.0
    keepalive_s: float = 5.0
    connect_timeout_s: float = 30.0
    pair_timeout_s: float = 20.0
    request_timeout_s: float = 10.0
    backoff_initial_s: float = 5.0
    backoff_max_s: float = 300.0
    publish_interval_s: float = 60.0  # hold mode


@dataclass(frozen=True)
class SerialConfig:
    """The VE.Direct cable. Off unless enabled."""

    enabled: bool = False
    port: str = DEFAULT_SERIAL_PORT
    baud: int = 19200
    publish_interval_s: float = 60.0  # the device sends a text block every second
    stale_after_s: float = 10.0
    hex: bool = True  # also use the HEX protocol (settings, writes); false: the daemon never transmits
    request_timeout_s: float = 2.0
    reopen_initial_s: float = 2.0
    reopen_max_s: float = 60.0


@dataclass(frozen=True)
class CsvConfig:
    enabled: bool = True
    path: Path = DEFAULT_DATA_DIR


@dataclass(frozen=True)
class MqttConfig:
    enabled: bool = True
    host: str = "localhost"
    port: int = 1883
    qos: int = 0
    csv: bool = True
    json: bool = True
    cbor: bool = True
    # At most one reading per device in this many seconds, the best source's newest; 0 publishes every reading
    publish_interval_s: float = 300.0


@dataclass(frozen=True)
class DaemonConfig:
    socket: Path | str = DEFAULT_SOCKET_PATH
    socket_mode: int = 0o660
    state_dir: Path = DEFAULT_STATE_DIR
    registers: str = "auto"  # how settings are read and written: auto (the cable if it answers, else GATT), serial, gatt
    log_level: str = "INFO"
    csv: CsvConfig = field(default_factory=CsvConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)


@dataclass(frozen=True)
class AppConfig:
    path: Path | None = None
    ble: BleConfig = field(default_factory=BleConfig)
    advert: AdvertConfig = field(default_factory=AdvertConfig)
    gatt: GattConfig = field(default_factory=GattConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    keys: dict[str, bytes] = field(default_factory=dict, repr=False)  # secrets: never in a repr
    daemon: DaemonConfig = field(default_factory=DaemonConfig)


def resolve_config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> AppConfig:
    resolved = resolve_config_path(path)
    if not resolved.is_file():
        raise ConfigError(f"Config file not found: {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {resolved}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping: {resolved}")
    return parse_config(raw, resolved)


def parse_config(raw: dict[str, Any], path: Path | None = None) -> AppConfig:
    unknown = set(raw) - {"ble", "advert", "gatt", "serial", "keys", "daemon"}
    if unknown:
        raise ConfigError(f"Unknown top-level key(s): {', '.join(sorted(unknown))}")
    config = AppConfig(
        path=path,
        ble=_parse_ble(_mapping(raw.get("ble"), "ble")),
        advert=_parse_advert(_mapping(raw.get("advert"), "advert")),
        gatt=_parse_gatt(_mapping(raw.get("gatt"), "gatt")),
        serial=_parse_serial(_mapping(raw.get("serial"), "serial")),
        keys=parse_keys(_mapping(raw.get("keys"), "keys")),
        daemon=_parse_daemon(_mapping(raw.get("daemon"), "daemon")),
    )
    if not config.ble.enabled and not config.serial.enabled:
        raise ConfigError("nothing to do: ble.enabled is false and serial.enabled is false")
    if config.daemon.registers == "serial" and not (config.serial.enabled and config.serial.hex):
        raise ConfigError("daemon.registers is serial, but serial.enabled and serial.hex are not both true")
    if config.daemon.registers == "gatt" and not config.ble.enabled:
        raise ConfigError("daemon.registers is gatt, but ble.enabled is false")
    return config


def normalise_address(address: str) -> str:
    return address.strip().upper()


def parse_key(text: Any, where: str) -> bytes:
    """A 32 digit hex key. The error never contains the value."""
    if not isinstance(text, str) or not re.fullmatch(r"[0-9a-fA-F]{%d}" % (KEY_LEN * 2), text.strip()):
        raise ConfigError(f"{where} must be {KEY_LEN * 2} hex digits")
    return bytes.fromhex(text.strip())


_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")  # what macOS uses instead of a MAC address
_SERIAL_RE = re.compile(r"[A-Za-z]{2}\d{4}[A-Za-z0-9]{5}")  # HQ22294ZGEU


def is_device_ident(text: Any) -> bool:
    """A MAC address, a macOS device UUID or a serial number: the ways to name a device."""
    return isinstance(text, str) and any(r.fullmatch(text.strip()) for r in (_MAC_RE, _UUID_RE, _SERIAL_RE))


def parse_keys(raw: dict[str, Any]) -> dict[str, bytes]:
    keys = {}
    for address, text in raw.items():
        if not is_device_ident(address):
            raise ConfigError(f"keys: {address!r} is not a MAC address, a device UUID or a serial number")
        keys[normalise_address(address)] = parse_key(text, f"keys.{address}")
    return keys


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _check_keys(raw: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown key(s) in {name}: {', '.join(sorted(unknown))}")


def _float(raw: dict[str, Any], key: str, default: float, name: str, *, minimum: float = 0.0, allow_zero: bool = False) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name}.{key} must be a number")
    value = float(value)
    if value < minimum or (value == minimum and not allow_zero):
        raise ConfigError(f"{name}.{key} must be {'>=' if allow_zero else '>'} {minimum:g}")
    return value


def _int(raw: dict[str, Any], key: str, default: int, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name}.{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"{name}.{key} must be {bound}")
    return value


def _bool(raw: dict[str, Any], key: str, default: bool, name: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name}.{key} must be true or false")
    return value


def _str(raw: dict[str, Any], key: str, default: str | None, name: str) -> str | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name}.{key} must be a non-empty string")
    return value


def _parse_ble(raw: dict[str, Any]) -> BleConfig:
    d = BleConfig()
    _check_keys(raw, {f.name for f in fields(BleConfig)}, "ble")
    address = _str(raw, "address", d.address, "ble")
    if address and not is_device_ident(address):
        raise ConfigError("ble.address must be a MAC address, a device UUID (macOS) or a serial number")
    return BleConfig(
        enabled=_bool(raw, "enabled", d.enabled, "ble"),
        adapter=_str(raw, "adapter", d.adapter, "ble"),
        address=normalise_address(address) if address else None,
        select_window_s=_float(raw, "select_window_s", d.select_window_s, "ble", allow_zero=True),
        rescan_interval_s=_float(raw, "rescan_interval_s", d.rescan_interval_s, "ble"),
        switch_hysteresis_db=_float(raw, "switch_hysteresis_db", d.switch_hysteresis_db, "ble", allow_zero=True),
    )


def _parse_advert(raw: dict[str, Any]) -> AdvertConfig:
    d = AdvertConfig()
    _check_keys(raw, {f.name for f in fields(AdvertConfig)}, "advert")
    return AdvertConfig(
        publish_interval_s=_float(raw, "publish_interval_s", d.publish_interval_s, "advert"),
        stale_after_s=_float(raw, "stale_after_s", d.stale_after_s, "advert"),
    )


def _parse_serial(raw: dict[str, Any]) -> SerialConfig:
    d = SerialConfig()
    _check_keys(raw, {f.name for f in fields(SerialConfig)}, "serial")
    return SerialConfig(
        enabled=_bool(raw, "enabled", d.enabled, "serial"),
        port=_str(raw, "port", d.port, "serial") or d.port,
        baud=_int(raw, "baud", d.baud, "serial"),
        publish_interval_s=_float(raw, "publish_interval_s", d.publish_interval_s, "serial"),
        stale_after_s=_float(raw, "stale_after_s", d.stale_after_s, "serial"),
        hex=_bool(raw, "hex", d.hex, "serial"),
        request_timeout_s=_float(raw, "request_timeout_s", d.request_timeout_s, "serial"),
        reopen_initial_s=_float(raw, "reopen_initial_s", d.reopen_initial_s, "serial"),
        reopen_max_s=_float(raw, "reopen_max_s", d.reopen_max_s, "serial"),
    )


def _parse_gatt(raw: dict[str, Any]) -> GattConfig:
    d = GattConfig()
    _check_keys(raw, {f.name for f in fields(GattConfig)}, "gatt")
    mode = raw.get("mode", d.mode)
    if mode is False:
        mode = "off"  # YAML reads an unquoted off as false
    if mode not in GATT_MODES:
        raise ConfigError(f"gatt.mode must be one of: {', '.join(GATT_MODES)}")
    passkey = raw.get("passkey", d.passkey)
    if isinstance(passkey, int) and not isinstance(passkey, bool):
        passkey = f"{passkey:06d}"  # YAML turns an unquoted 000000 into 0
    if not isinstance(passkey, str) or not re.fullmatch(r"\d{1,6}", passkey):
        raise ConfigError("gatt.passkey must be up to 6 digits")
    return GattConfig(
        mode=mode,
        passkey=passkey,
        poll_interval_s=_float(raw, "poll_interval_s", d.poll_interval_s, "gatt"),
        collect_s=_float(raw, "collect_s", d.collect_s, "gatt"),
        keepalive_s=_float(raw, "keepalive_s", d.keepalive_s, "gatt"),
        connect_timeout_s=_float(raw, "connect_timeout_s", d.connect_timeout_s, "gatt"),
        pair_timeout_s=_float(raw, "pair_timeout_s", d.pair_timeout_s, "gatt"),
        request_timeout_s=_float(raw, "request_timeout_s", d.request_timeout_s, "gatt"),
        backoff_initial_s=_float(raw, "backoff_initial_s", d.backoff_initial_s, "gatt"),
        backoff_max_s=_float(raw, "backoff_max_s", d.backoff_max_s, "gatt"),
        publish_interval_s=_float(raw, "publish_interval_s", d.publish_interval_s, "gatt"),
    )


def _parse_socket(value: Any) -> Path | str:
    """A Unix socket path, or tcp://127.0.0.1:port (the default on Windows, which has no usable Unix sockets)."""
    text = str(value)
    if text.startswith("tcp://"):
        from smartsolar.ipc import parse_endpoint

        parse_endpoint(text)  # raises ConfigError for a bad or non-loopback address
        return text
    return Path(text).expanduser()


def _parse_mode(value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError("daemon.socket_mode must be an octal mode")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.lower().removeprefix("0o")
        try:
            return int(text, 8)
        except ValueError:
            pass
    raise ConfigError("daemon.socket_mode must be an octal mode such as \"0660\"")


def _parse_daemon(raw: dict[str, Any]) -> DaemonConfig:
    d = DaemonConfig()
    _check_keys(raw, {"socket", "socket_mode", "state_dir", "registers", "log_level", "csv", "mqtt"}, "daemon")
    level = str(raw.get("log_level", d.log_level)).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("daemon.log_level must be DEBUG, INFO, WARNING or ERROR")
    registers = raw.get("registers", d.registers)
    if registers not in REGISTER_PATHS:
        raise ConfigError(f"daemon.registers must be one of: {', '.join(REGISTER_PATHS)}")
    csv_raw = _mapping(raw.get("csv"), "daemon.csv")
    _check_keys(csv_raw, {"enabled", "path"}, "daemon.csv")
    mqtt_raw = _mapping(raw.get("mqtt"), "daemon.mqtt")
    _check_keys(mqtt_raw, {f.name for f in fields(MqttConfig)}, "daemon.mqtt")
    md = MqttConfig()
    return DaemonConfig(
        socket=_parse_socket(_str(raw, "socket", str(d.socket), "daemon") or d.socket),
        socket_mode=_parse_mode(raw.get("socket_mode", d.socket_mode)),
        state_dir=Path(_str(raw, "state_dir", str(d.state_dir), "daemon") or d.state_dir).expanduser(),
        registers=registers,
        log_level=level,
        csv=CsvConfig(
            enabled=_bool(csv_raw, "enabled", True, "daemon.csv"),
            path=Path(_str(csv_raw, "path", str(CsvConfig().path), "daemon.csv") or CsvConfig().path).expanduser(),
        ),
        mqtt=MqttConfig(
            enabled=_bool(mqtt_raw, "enabled", md.enabled, "daemon.mqtt"),
            host=_str(mqtt_raw, "host", md.host, "daemon.mqtt") or md.host,
            port=_int(mqtt_raw, "port", md.port, "daemon.mqtt", minimum=1, maximum=65535),
            qos=_int(mqtt_raw, "qos", md.qos, "daemon.mqtt", minimum=0, maximum=2),
            csv=_bool(mqtt_raw, "csv", md.csv, "daemon.mqtt"),
            json=_bool(mqtt_raw, "json", md.json, "daemon.mqtt"),
            cbor=_bool(mqtt_raw, "cbor", md.cbor, "daemon.mqtt"),
            publish_interval_s=_float(mqtt_raw, "publish_interval_s", md.publish_interval_s, "daemon.mqtt", allow_zero=True),
        ),
    )
