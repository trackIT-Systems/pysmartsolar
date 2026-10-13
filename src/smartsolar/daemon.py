"""smartsolard: follows a Victron SmartSolar over BLE and reports its values.

The Instant Readout advertisements are the main data source (no connection needed). GATT is
optional (``gatt.mode``) for the values that are not in the advertisements, and it fetches the
Instant Readout key when none is configured.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from smartsolar import __version__
from smartsolar.ble import BleBackend
from smartsolar.config import AppConfig, load_config
from smartsolar.errors import BadRequest, ConfigError, ConnectError, NoDataError, NotConnectedError, ProtocolError, SmartsolarError
from smartsolar.gatt_manager import GattManager, GattState
from smartsolar.keys import KeyStore
from smartsolar.outputs import Output, Outputs
from smartsolar.outputs.csv import CsvOutput, csv_session_path
from smartsolar.outputs.mqtt import MqttOutput
from smartsolar.reading import Event, Reading, device_id, local_iso, utc_iso
from smartsolar.server import ControlServer
from smartsolar.serial_manager import SerialManager
from smartsolar.state import BLOCKS, AdvertStats, Cache, GattStats, SerialStats
from smartsolar.tracker import DeviceTracker
from smartsolar.vreg import (
    PROFILE_CONTEXT_VREGS,
    VREG_BATTERY_VOLTAGE_ACTUAL,
    VREG_FIRMWARE,
    VREG_PRODUCT_ID,
    VREG_VOLTAGE_RANGE_MAX,
    VREG_VOLTAGE_RANGE_MIN,
    WRITABLE,
    build_write,
    firmware_version,
    product_id,
)

log = logging.getLogger("smartsolard")

SCAN_RETRY_S = 10.0
SCAN_WATCHDOG_S = 180.0
SCAN_CHECK_S = 5.0
SELECTION_INTERVAL_S = 1.0
POLL_TIMEOUT_MARGIN_S = 30.0


class NoBackend:
    """Stands in for the BLE stack when ble.enabled is false: nothing in the daemon starts it."""

    async def start_scan(self, on_advert) -> None:
        raise ConnectError("Bluetooth is disabled (ble.enabled is false)")

    async def stop_scan(self) -> None:
        return

    async def connect(self, *args: Any, **kwargs: Any):
        raise ConnectError("Bluetooth is disabled (ble.enabled is false)")


def build_outputs(config: AppConfig, hostname: str | None = None) -> Outputs:
    sinks: list[Output] = []
    if config.daemon.csv.enabled:
        path = csv_session_path(config.daemon.csv.path, hostname)
        log.info("csv file %s", path)
        sinks.append(CsvOutput(path))
    if config.daemon.mqtt.enabled:
        sinks.append(MqttOutput(config.daemon.mqtt, hostname))
    if not sinks:
        log.warning("no outputs enabled; readings are only available through the CLI")
    return Outputs(sinks)


def _blocks(args: dict[str, Any]) -> list[str]:
    name = args.get("block", "all")
    if name == "all":
        return list(BLOCKS)
    if name not in BLOCKS:
        raise BadRequest(f"unknown block {name!r}, expected one of: {', '.join(BLOCKS)}, all")
    return [name]


def reading_dict(reading: Reading) -> dict[str, Any]:
    return {"time": utc_iso(reading.time), "values": [v.as_dict() for v in reading.values]}


class Daemon:
    def __init__(
        self,
        config: AppConfig,
        *,
        backend: BleBackend | None = None,
        outputs: Outputs | None = None,
        hostname: str | None = None,
        keys: KeyStore | None = None,
        serial_opener: Any | None = None,
    ) -> None:
        self.config = config
        self.hostname = hostname or socket.gethostname()
        if backend is None and config.ble.enabled:
            from smartsolar.ble.bleak_backend import BleakBackend

            backend = BleakBackend(config.ble.adapter)
        self.backend = backend if backend is not None else NoBackend()
        self.outputs = outputs if outputs is not None else build_outputs(config, self.hostname)
        self.keys = keys if keys is not None else KeyStore(config.keys, config.daemon.state_dir)
        gatt = config.gatt
        gatt_stale = {"off": None, "hold": 3 * gatt.publish_interval_s, "poll": 3 * gatt.poll_interval_s}[gatt.mode]
        ble_on = config.ble.enabled
        self.cache = Cache(
            {
                "advert": config.advert.stale_after_s if ble_on else None,
                "gatt": gatt_stale if ble_on else None,
                "vedirect": config.serial.stale_after_s if config.serial.enabled else None,
            }
        )
        started = datetime.now().astimezone()
        self.advert_stats = AdvertStats(started=started)
        self.gatt_stats = GattStats()
        self.tracker = DeviceTracker(
            config.ble,
            config.advert,
            self.keys,
            self.cache,
            self._on_event,
            self.advert_stats,
            on_switch=self._on_switch,
            on_select=self._on_select,
        )
        self.gatt = GattManager(
            gatt,
            backend,
            lambda: self.tracker.current,
            lambda address: self.keys.get(address)[0] is not None,
            self.keys.store,
            self._on_event,
            self._on_gatt_reading,
            self.gatt_stats,
            on_state=self._on_gatt_state,
        )
        self.serial_stats = SerialStats()
        self.serial: SerialManager | None = None
        if config.serial.enabled:
            kwargs = {} if serial_opener is None else {"opener": serial_opener}
            self.serial = SerialManager(
                config.serial, cache=self.cache, emit_event=self._on_event, stats=self.serial_stats, on_serial=self._on_serial, **kwargs
            )
        self.server = ControlServer(config.daemon.socket, config.daemon.socket_mode, self._handlers())
        self.stop_event = asyncio.Event()
        self._started_mono = time.monotonic()
        self._last_published: dict[str, Reading | None] = {}

    # -- events and state ---------------------------------------------------

    def _device_id(self, address: str | None) -> str | None:
        """The id a device goes by in outputs: its serial number if known, else its MAC address."""
        if address is None:
            return None
        seen = self.tracker.seen.get(address)
        return device_id(address, seen.name if seen else None)

    def _current_id(self) -> str | None:
        if self.serial is not None and self.serial.serial:
            return self.serial.serial
        return self._device_id(self.tracker.current_address)

    def _on_serial(self, serial: str | None) -> None:
        if self.config.ble.enabled:
            self.tracker.set_serial(serial)
        self.outputs.emit_state(self.state_payload())

    def _on_event(self, event: Event) -> None:
        if event.device is not None and ":" in event.device:
            event = replace(event, device=self._device_id(event.device))
        self.outputs.emit_event(event)
        if event.kind == "key_rejected":
            self.gatt.poke()

    def _on_select(self) -> None:
        self.gatt.poke()
        self.outputs.emit_state(self.state_payload())

    def _on_switch(self) -> None:
        self.cache.clear("advert", "gatt")
        self.gatt.device_changed()

    def _on_gatt_state(self, state: GattState) -> None:
        self.outputs.emit_state(self.state_payload())

    def _on_gatt_reading(self, reading: Reading) -> None:
        self.cache.update(reading)
        self.outputs.emit_reading(reading)

    def state_payload(self) -> dict[str, Any]:
        current = self.tracker.current
        seen = self.tracker.seen.get(self.tracker.current_address or "")
        return {
            "_time": local_iso(datetime.now().astimezone()),
            "state": "running",
            "device": self._current_id(),
            "address": current.address if current else None,
            "rssi": round(seen.rssi) if seen else None,
            "gatt_mode": self.config.gatt.mode if self.config.ble.enabled else None,
            "gatt": self.gatt.state.value if self.config.ble.enabled else None,
            "serial": None if self.serial is None else self.serial.state,
        }

    # -- lifecycle ----------------------------------------------------------

    async def run(self, *, handle_signals: bool = True) -> int:
        loop = asyncio.get_running_loop()
        if handle_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.stop_event.set)
        self._on_event(
            Event(
                datetime.now().astimezone(),
                "daemon_started",
                None,
                {"version": __version__, "ble": self.config.ble.enabled, "gatt_mode": self.config.gatt.mode, "serial": self.serial is not None},
            )
        )
        self.outputs.emit_state(self.state_payload())
        await self.server.start()
        tasks = []
        if self.config.ble.enabled:
            tasks += [
                asyncio.create_task(self._scan_supervisor(), name="scan"),
                asyncio.create_task(self._selection_loop(), name="selection"),
                asyncio.create_task(self._publish_loop("advert", self.config.advert.publish_interval_s), name="publisher-advert"),
                asyncio.create_task(self.gatt.run(), name="gatt"),
            ]
        if self.serial is not None:
            tasks += [
                asyncio.create_task(self.serial.run(), name="serial"),
                asyncio.create_task(self._publish_loop("vedirect", self.config.serial.publish_interval_s), name="publisher-vedirect"),
            ]
        stopper = asyncio.create_task(self.stop_event.wait())
        exit_code = 0
        try:
            done, _ = await asyncio.wait({*tasks, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for task in done - {stopper}:
                log.error("task %s ended unexpectedly: %r", task.get_name(), task.exception() if not task.cancelled() else "cancelled")
                exit_code = 1
        finally:
            await self._shutdown(tasks, stopper)
        return exit_code

    async def _shutdown(self, tasks: list[asyncio.Task], stopper: asyncio.Task) -> None:
        log.info("shutting down")
        stopper.cancel()
        await self.server.stop()
        self.gatt.stop()
        for task in tasks:
            if task.get_name() != "gatt":
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.config.ble.enabled:
            try:
                await self.backend.stop_scan()
            except Exception as exc:
                log.warning("stopping the scan failed: %s", exc)
        self._on_event(Event(datetime.now().astimezone(), "daemon_stopped"))
        self.outputs.emit_state({**self.state_payload(), "state": "offline", "device": None, "rssi": None})
        self.outputs.close()
        log.info("stopped")

    async def _scan_supervisor(self) -> None:
        """Keep the permanent scan running: start it, retry if that fails, restart it if it goes quiet."""
        while True:
            try:
                await self.backend.start_scan(self.tracker.on_advert)
            except Exception as exc:
                log.warning("cannot start the scan: %s (retrying in %g s)", exc, SCAN_RETRY_S)
                await asyncio.sleep(SCAN_RETRY_S)
                continue
            log.info("scanning for Victron advertisements")
            while True:
                await asyncio.sleep(SCAN_CHECK_S)
                last = self.tracker.last_advert_mono
                if self.tracker.current_address is not None and last is not None and time.monotonic() - last > SCAN_WATCHDOG_S:
                    log.warning("no advertisement from %s for %g s, restarting the scan", self.tracker.current_address, SCAN_WATCHDOG_S)
                    self._on_event(Event(datetime.now().astimezone(), "scan_restarted", self.tracker.current_address))
                    self.tracker.last_advert_mono = time.monotonic()
                    try:
                        await self.backend.stop_scan()
                    except Exception as exc:
                        log.warning("stopping the scan failed: %s", exc)
                    break

    async def _selection_loop(self) -> None:
        while True:
            self.tracker.review()
            await asyncio.sleep(SELECTION_INTERVAL_S)

    async def _publish_loop(self, block: str, interval_s: float) -> None:
        """Hand the newest reading of a source to the outputs every interval (a source delivers about one a second)."""
        while True:
            await asyncio.sleep(interval_s)
            reading = self.cache.blocks[block].reading
            if reading is None or reading is self._last_published.get(block) or self.cache.is_stale(block):
                continue
            self._last_published[block] = reading
            self.outputs.emit_reading(reading)

    # -- commands -----------------------------------------------------------

    def _handlers(self) -> dict[str, Any]:
        return {
            "status": self._cmd_status,
            "cached": self._cmd_cached,
            "read": self._cmd_read,
            "reconnect": self._cmd_reconnect,
            "rescan": self._cmd_rescan,
            "scan": self._cmd_scan,
            "fetch_key": self._cmd_fetch_key,
            "write": self._cmd_write,
            "settings": self._cmd_settings,
        }

    def _key_status(self) -> str:
        address = self.tracker.current_address
        if address is None:
            return "unknown"
        key, _ = self.keys.get(address)
        if key is None:
            return "missing"
        return "mismatch" if self.tracker._mismatches else ("ok" if self.advert_stats.decoded else "unknown")

    async def _cmd_status(self, args: dict[str, Any]) -> dict[str, Any]:
        now = time.monotonic()
        tracker, gatt = self.tracker, self.gatt
        current = tracker.current
        seen = tracker.seen.get(tracker.current_address or "")
        device = None
        if current is not None:
            _, source = self.keys.get(current.address)
            device = {
                "address": current.address,
                "name": current.name,
                "rssi": None if seen is None else round(seen.rssi),
                "model": f"0x{tracker.current_header.model:04x}" if tracker.current_header else None,
                "pinned": self.config.ble.address is not None,
                "key": {"source": source, "state": self._key_status()},
                "last_advert_age_s": None if tracker.last_advert_mono is None else round(now - tracker.last_advert_mono, 1),
                "adverts_per_min": tracker.rate_per_min(),
            }
        stats, gstats = self.advert_stats, self.gatt_stats
        gatt_info: dict[str, Any] = {
            "mode": self.config.gatt.mode,
            "state": gatt.state.value,
            "connected": gatt.session is not None,
            "connected_since": utc_iso(gatt.connected_since) if gatt.connected_since else None,
            "connect_attempts": gstats.connect_attempts,
            "connects": gstats.connects,
            "disconnects": gstats.disconnects,
            "pairings": gstats.pairings,
            "polls": gstats.polls,
            "last_poll": utc_iso(gatt.last_poll) if gatt.last_poll else None,
            "next_poll_in_s": None if gatt.next_poll_mono is None else max(0, round(gatt.next_poll_mono - now)),
            "last_error": gstats.last_error,
            "backoff_s": None if gstats.backoff_s is None else round(gstats.backoff_s, 1),
            "last_key_fetch": utc_iso(gatt.last_key_fetch) if gatt.last_key_fetch else None,
        }
        return {
            "version": __version__,
            "uptime_s": round(now - self._started_mono),
            "id": self._current_id(),
            "ble": self.config.ble.enabled,
            "serial": None if self.serial is None else self.serial.describe(),
            "device": device,
            "devices_seen": len(tracker.seen),
            "blocks": {name: self.cache.describe(name) for name in BLOCKS},
            "advert": {
                "seen": stats.seen,
                "decoded": stats.decoded,
                "no_key": stats.no_key,
                "rejected": stats.rejected,
                "undecodable": stats.undecodable,
            },
            "gatt": gatt_info,
        }

    async def _cmd_cached(self, args: dict[str, Any]) -> dict[str, Any]:
        blocks: dict[str, Any] = {}
        for name in _blocks(args):
            state = self.cache.blocks[name]
            description = self.cache.describe(name)
            blocks[name] = {
                "enabled": description["enabled"],
                "time": utc_iso(state.reading.time) if state.reading else None,
                "age_s": description["age_s"],
                "stale": description["stale"],
                "values": [v.as_dict() for v in state.reading.values] if state.reading else [],
            }
        return {"device": self._current_id(), "blocks": blocks}

    async def _read_block(self, name: str) -> Reading:
        if name == "vedirect":
            reading = self.cache.blocks["vedirect"].reading
            if self.serial is None:
                raise NoDataError("serial.enabled is false")
            if reading is None or self.cache.is_stale("vedirect"):
                raise NoDataError(f"no text block received on {self.config.serial.port} in the last {self.config.serial.stale_after_s:g} s (cable? port busy?)")
            return reading
        if not self.config.ble.enabled:
            raise NoDataError("ble.enabled is false")
        if name == "advert":
            reading = self.cache.blocks["advert"].reading
            if reading is None or self.cache.is_stale("advert"):
                raise NoDataError(f"no advertisement decoded in the last {self.config.advert.stale_after_s:g} s (key known? device in range?)")
            return reading
        mode = self.config.gatt.mode
        if mode == "off":
            raise NoDataError("gatt.mode is off")
        if mode == "hold":
            reading = self.gatt.snapshot()
            if reading is None:
                raise NotConnectedError(f"no GATT connection (state={self.gatt.state.value})")
            return reading
        cfg = self.config.gatt
        timeout = cfg.connect_timeout_s + cfg.pair_timeout_s + cfg.collect_s + POLL_TIMEOUT_MARGIN_S
        return await asyncio.wait_for(self.gatt.request_poll(), timeout)

    async def _cmd_read(self, args: dict[str, Any]) -> dict[str, Any]:
        names = _blocks(args)
        blocks: dict[str, Any] = {}
        for name in names:
            try:
                blocks[name] = reading_dict(await self._read_block(name))
            except (NoDataError, NotConnectedError) as exc:
                if len(names) == 1:
                    raise
                blocks[name] = {"error": str(exc)}
        result: dict[str, Any] = {"device": self._current_id(), "blocks": blocks}
        if args.get("raw") and "gatt" in names and "values" in blocks.get("gatt", {}):
            result["raw"] = self.gatt.raw_registers()
        return result

    def _need_ble(self) -> None:
        if not self.config.ble.enabled:
            raise NoDataError("ble.enabled is false")

    async def _cmd_reconnect(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_ble()
        if self.config.gatt.mode == "off":
            raise NoDataError("gatt.mode is off, there is no connection to renew")
        self.gatt.request_reconnect()
        return {"requested": True}

    async def _cmd_rescan(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_ble()
        self.tracker.reselect()
        return await self._cmd_scan(args)

    async def _cmd_scan(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_ble()
        return {"devices": self.tracker.scan_table()}

    async def _cmd_fetch_key(self, args: dict[str, Any]) -> dict[str, Any]:
        self._need_ble()
        address = self.tracker.current_address
        if address is None:
            raise NoDataError("no device selected yet")
        cfg = self.config.gatt
        key = await asyncio.wait_for(self.gatt.request_key(), cfg.connect_timeout_s + cfg.pair_timeout_s + cfg.request_timeout_s + POLL_TIMEOUT_MARGIN_S)
        result: dict[str, Any] = {"address": address, "fetched": True}
        if args.get("show"):
            result["key"] = key.hex()
        return result


    # -- registers: over the cable or over GATT ------------------------------

    def _register_paths(self, args: dict[str, Any]) -> list[str]:
        """Which ways to the registers to try, in order (``via`` in the request, else daemon.registers)."""
        via = args.get("via") or self.config.daemon.registers
        if via not in ("auto", "serial", "gatt"):
            raise BadRequest(f"via must be auto, serial or gatt, not {via!r}")
        serial_ok = self.serial is not None and self.config.serial.hex
        ble_ok = self.config.ble.enabled
        if via == "serial":
            if not serial_ok:
                raise NoDataError("the VE.Direct cable with the HEX protocol is not enabled (serial.enabled, serial.hex)")
            return ["serial"]
        if via == "gatt":
            if not ble_ok:
                raise NoDataError("ble.enabled is false")
            return ["gatt"]
        paths = [p for p, ok in (("serial", serial_ok and self.serial is not None and self.serial.connected), ("gatt", ble_ok)) if ok]
        if not paths:
            raise NoDataError("no way to reach the registers: the VE.Direct cable is not connected (or serial.hex is off) and Bluetooth is disabled")
        return paths

    async def _via(self, args: dict[str, Any], run: Any) -> Any:
        """Run ``run(path)`` over the first way that works. Only a missing link or no answer moves on to the next one."""
        paths = self._register_paths(args)
        problems = []
        for path in paths:
            try:
                return await run(path)
            except (NotConnectedError, NoDataError, ProtocolError, asyncio.TimeoutError) as exc:
                if len(paths) == 1:
                    raise
                problems.append(f"{path}: {exc or type(exc).__name__}")
        raise NoDataError("; ".join(problems))

    def _timeout(self, path: str, requests: int) -> float:
        if path == "serial":
            return self.config.serial.request_timeout_s * (2 * requests + 2) + 5.0
        cfg = self.config.gatt
        return cfg.connect_timeout_s + cfg.pair_timeout_s + cfg.request_timeout_s * (requests + 2) + POLL_TIMEOUT_MARGIN_S

    async def _cmd_settings(self, args: dict[str, Any]) -> dict[str, Any]:
        """Every writable setting, read live (connects on demand), with what a profile is checked against."""
        names = args.get("names") or [n for n in WRITABLE if n != "device_mode"]
        unknown = [n for n in names if n not in WRITABLE]
        if unknown:
            raise BadRequest(f"not a writable register: {', '.join(map(str, unknown))}")
        vregs = [WRITABLE[n].vreg for n in names] + list(PROFILE_CONTEXT_VREGS)

        async def read(path: str) -> tuple[str, dict[int, Any]]:
            if path == "serial":
                assert self.serial is not None
                return path, await asyncio.wait_for(self.serial.read(vregs), self._timeout(path, len(vregs)))
            if self.tracker.current_address is None:
                raise NoDataError("no device selected yet")
            return path, await asyncio.wait_for(self.gatt.request_read(vregs), self._timeout(path, len(vregs)))

        path, raw = await self._via(args, read)
        values = []
        for name in names:
            writable = WRITABLE[name]
            if writable.vreg not in raw:
                continue  # this model or firmware does not have it
            values.append({"name": name, "friendly": writable.friendly, "unit": writable.unit, "value": writable.decode(raw[writable.vreg])})
        vmin, vmax = raw.get(VREG_VOLTAGE_RANGE_MIN), raw.get(VREG_VOLTAGE_RANGE_MAX)
        return {
            "device": self._current_id(),
            "via": path,
            "product_id": product_id(raw[VREG_PRODUCT_ID]) if VREG_PRODUCT_ID in raw else None,
            "firmware": firmware_version(raw[VREG_FIRMWARE]) if VREG_FIRMWARE in raw else None,
            "system_voltage": bytes(raw[VREG_BATTERY_VOLTAGE_ACTUAL])[0] if VREG_BATTERY_VOLTAGE_ACTUAL in raw else None,
            "voltage_range": [round(int.from_bytes(vmin, "little") * 0.01, 2), round(int.from_bytes(vmax, "little") * 0.01, 2)] if vmin is not None and vmax is not None else None,
            "values": values,
        }

    async def _cmd_write(self, args: dict[str, Any]) -> dict[str, Any]:
        values = args.get("values")
        if not isinstance(values, dict) or not values:
            raise BadRequest("args.values must be an object of name: value")
        items = build_write(values)  # InvalidWriteError before anything is sent

        async def write(path: str) -> tuple[str, list[dict[str, Any]]]:
            if path == "serial":
                assert self.serial is not None
                return path, await asyncio.wait_for(self.serial.write(items), self._timeout(path, 3 * len(items)))
            if self.tracker.current_address is None:
                raise NoDataError("no device selected yet")
            return path, await asyncio.wait_for(self.gatt.request_write(items), self._timeout(path, 3 * len(items)))

        path, results = await self._via(args, write)
        return {"device": self._current_id(), "via": path, "written": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartsolard", description="Follow a Victron SmartSolar over BLE and report its values (CSV, MQTT).")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="config file (default: /boot/firmware/smartsolar.yml)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more log output (-v: DEBUG)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    level = logging.DEBUG if args.verbose else getattr(logging, config.daemon.log_level)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        return asyncio.run(Daemon(config).run())
    except (SmartsolarError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
