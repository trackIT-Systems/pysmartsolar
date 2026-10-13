"""Follows the Victron advertisements: device selection and Instant Readout decoding.

Every advertisement of a Victron device updates the signal strength of that device. The
selected device (the pinned address, or the strongest solar charger) is decoded with its
key, and the result goes into the cache. The selection is reviewed at a long interval, and
a device only replaces the current one if it is clearly stronger (hysteresis).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from smartsolar import instant
from smartsolar.ble import Advertisement
from smartsolar.config import AdvertConfig, BleConfig
from smartsolar.errors import AdvertError, KeyMismatch
from smartsolar.keys import SOURCE_FETCHED, KeyStore
from smartsolar.reading import Event, Reading, Value, device_id, serial_from_name
from smartsolar.state import AdvertStats, Cache

log = logging.getLogger(__name__)

RSSI_ALPHA = 0.2
ELIGIBLE_MAX_AGE_S = 30.0
MISMATCHES_BEFORE_FORGETTING = 5
RATE_WINDOW_S = 60.0

ADVERT_FIELDS = (
    ("device_state", "Device state", ""),
    ("device_state_code", "Device state code", ""),
    ("charger_error", "Charger error", ""),
    ("charger_error_code", "Charger error code", ""),
    ("battery_voltage", "Battery voltage", "V"),
    ("battery_current", "Battery current", "A"),
    ("yield_today", "Yield today", "Wh"),
    ("pv_power", "PV power", "W"),
    ("load_current", "Load current", "A"),
)


@dataclass
class Seen:
    address: str
    name: str | None
    rssi: float
    first_seen: float
    last_seen: float
    count: int
    adv: Advertisement
    header: instant.Header | None


def advert_values(values: dict[str, Any]) -> tuple[Value, ...]:
    return tuple(Value(name, friendly, unit, values.get(name)) for name, friendly, unit in ADVERT_FIELDS)


class DeviceTracker:
    def __init__(
        self,
        ble: BleConfig,
        advert: AdvertConfig,
        keys: KeyStore,
        cache: Cache,
        emit_event: Callable[[Event], None],
        stats: AdvertStats,
        *,
        on_switch: Callable[[], None] | None = None,
        on_select: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ble = ble
        self.advert = advert
        self.keys = keys
        self.cache = cache
        self.stats = stats
        self._emit_event = emit_event
        self._on_switch = on_switch
        self._on_select = on_select
        self._clock = clock
        self.seen: dict[str, Seen] = {}
        self.current_address: str | None = None
        self.current: Advertisement | None = None
        self.current_header: instant.Header | None = None
        self.serial: str | None = None  # the serial number of the device on the VE.Direct cable: prefer the BLE device with it
        self.last_advert_mono: float | None = None
        self._started = clock()
        self._next_review = 0.0
        self._mismatches = 0
        self._times: deque[float] = deque()

    # -- advertisements -----------------------------------------------------

    def on_advert(self, adv: Advertisement) -> None:
        now = self._clock()
        try:
            header: instant.Header | None = instant.parse_header(adv.data)
        except AdvertError:
            header = None
        s = self.seen.get(adv.address)
        if s is None:
            self.seen[adv.address] = Seen(adv.address, adv.name, adv.rssi, now, now, 1, adv, header)
        else:
            s.rssi += RSSI_ALPHA * (adv.rssi - s.rssi)
            s.name = adv.name or s.name
            s.last_seen = now
            s.count += 1
            s.adv = adv
            s.header = header
        if adv.address == self.current_address:
            self.current = adv
            self.current_header = header
            self.last_advert_mono = now
            self.stats.seen += 1
            self._times.append(now)
            self._decode(adv)

    def rate_per_min(self) -> float:
        now = self._clock()
        while self._times and now - self._times[0] > RATE_WINDOW_S:
            self._times.popleft()
        if not self._times:
            return 0.0
        window = min(RATE_WINDOW_S, now - self._started)
        return round(len(self._times) * 60.0 / window, 1) if window > 0 else 0.0

    def _decode(self, adv: Advertisement) -> None:
        key, source = self.keys.get(adv.address)
        if key is None:
            self.stats.no_key += 1
            return
        try:
            decoded = instant.decode(key, adv.data)
        except KeyMismatch as exc:
            self.stats.rejected += 1
            self._mismatches += 1
            if self._mismatches == 1:
                log.warning("%s: %s (key from %s)", adv.address, exc, source)
            if source == SOURCE_FETCHED and self._mismatches >= MISMATCHES_BEFORE_FORGETTING:
                # The device was reset or re-keyed: the fetched key is outdated, fetch it again.
                self.keys.forget(adv.address)
                self._mismatches = 0
                self._event("key_rejected", adv.address, reason="check byte does not match the fetched key")
            return
        except AdvertError as exc:
            self.stats.undecodable += 1
            if self.stats.undecodable == 1:
                log.warning("%s: cannot decode the advertisement: %s", adv.address, exc)
            return
        self._mismatches = 0
        self.stats.decoded += 1
        self.cache.update(Reading(device_id(adv.address, adv.name), "advert", datetime.now().astimezone(), advert_values(decoded.values)))

    # -- selection ----------------------------------------------------------

    def eligible(self) -> list[Seen]:
        """Solar chargers seen recently (other Victron devices are listed by ``scan`` but never selected).

        With a serial number known from the cable, the device that carries it in its name is the
        only candidate as long as it is in range; otherwise the others stay candidates.
        """
        now = self._clock()
        found = [
            s
            for s in self.seen.values()
            if s.header is not None
            and s.header.record_type == instant.RECORD_SOLAR_CHARGER
            and now - s.last_seen <= ELIGIBLE_MAX_AGE_S
            and (self.ble.address is None or s.address == self.ble.address)
        ]
        if self.serial is not None and self.ble.address is None:
            mine = [s for s in found if serial_from_name(s.name) == self.serial]
            if mine:
                return mine
        return found

    def set_serial(self, serial: str | None) -> None:
        """The cable reported its serial number: use the BLE device with the same one."""
        if serial != self.serial:
            self.serial = serial
            self._prefer_serial()

    def _prefer_serial(self) -> None:
        """Switch to the device that carries the cable's serial number, whatever its signal strength."""
        if self.serial is None or self.ble.address is not None or self.current_address is None:
            return
        current = self.seen.get(self.current_address)
        if current is not None and serial_from_name(current.name) == self.serial:
            return
        mine = [s for s in self.eligible() if serial_from_name(s.name) == self.serial]
        if not mine:
            return
        old = self.current_address
        self._select(max(mine, key=lambda s: s.rssi), "serial", previous=old, rssi_from=None if current is None else round(current.rssi))
        if self._on_switch is not None:
            self._on_switch()

    def review(self) -> None:
        """Called about once a second."""
        now = self._clock()
        candidates = self.eligible()
        self._prefer_serial()
        if self.current_address is None:
            if not candidates:
                return
            if self.ble.address is None and now - self._started < self.ble.select_window_s:
                return
            self._select(max(candidates, key=lambda s: s.rssi), "initial")
            return
        if self.ble.address is not None or now < self._next_review:
            return
        self._next_review = now + self.ble.rescan_interval_s
        self._consider_switch(candidates, self.ble.switch_hysteresis_db)

    def reselect(self) -> None:
        """Choose again now, without hysteresis (CLI ``rescan``)."""
        candidates = self.eligible()
        if not candidates:
            return
        if self.current_address is None:
            self._select(max(candidates, key=lambda s: s.rssi), "rescan")
        else:
            self._consider_switch(candidates, 0.0)
        self._next_review = self._clock() + self.ble.rescan_interval_s

    def _consider_switch(self, candidates: list[Seen], hysteresis_db: float) -> None:
        if not candidates:
            return
        current = self.seen.get(self.current_address or "")
        best = max(candidates, key=lambda s: s.rssi)
        if best.address == self.current_address:
            return
        if current is not None and best.rssi < current.rssi + hysteresis_db:
            return
        old = self.current_address
        self._select(best, "switch", previous=old, rssi_from=None if current is None else round(current.rssi))
        if self._on_switch is not None:
            self._on_switch()

    def _select(self, seen: Seen, reason: str, **extra: Any) -> None:
        self.current_address = seen.address
        self.current = seen.adv
        self.current_header = seen.header
        self._mismatches = 0
        self._times.clear()
        self.last_advert_mono = self._clock()
        self._next_review = self._clock() + self.ble.rescan_interval_s
        _, source = self.keys.get(seen.address)
        kind = "device_switched" if reason in ("switch", "serial") else "device_selected"
        self._event(kind, seen.address, rssi=round(seen.rssi), name=seen.name, key=source, reason=reason, **extra)
        if self._on_select is not None:
            self._on_select()

    def scan_table(self) -> list[dict[str, Any]]:
        now = self._clock()
        rows = []
        for s in sorted(self.seen.values(), key=lambda s: s.rssi, reverse=True):
            _, source = self.keys.get(s.address)
            rows.append(
                {
                    "address": s.address,
                    "name": s.name,
                    "rssi": round(s.rssi),
                    "adverts": s.count,
                    "age_s": round(now - s.last_seen, 1),
                    "model": f"0x{s.header.model:04x}" if s.header else None,
                    "record_type": s.header.record_type if s.header else None,
                    "solar_charger": bool(s.header and s.header.record_type == instant.RECORD_SOLAR_CHARGER),
                    "key": source,
                    "current": s.address == self.current_address,
                }
            )
        return rows

    def _event(self, kind: str, device: str | None, **data: Any) -> None:
        try:
            self._emit_event(Event(datetime.now().astimezone(), kind, device, data))
        except Exception:
            log.exception("event callback failed")
