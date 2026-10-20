"""MQTT output: readings and events as CSV, JSON and CBOR, readings rate-limited.

* ``{host}/smartsolar/{device}/{fmt}``: readings; at most one per device every
  ``publish_interval_s``. Which source a reading came from is not published; the
  best source that delivers wins (VE.Direct, then GATT, then the advertisements).
  ``{device}`` is the serial number, or the MAC address as 12 hex digits.
* ``{host}/smartsolar/events/{fmt}``: connection events, not rate-limited
* ``{host}/smartsolar/state/{json,cbor}``: connection state as a map, retained; the
  will sets it to offline

See docs/outputs.md for the columns and encodings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import cbor2
import paho.mqtt.client as mqtt

from smartsolar.clock import Clock
from smartsolar.config import MqttConfig
from smartsolar.outputs.formats import ENCODERS, EVENTS, Record, event_record, reading_record
from smartsolar.reading import Event, Reading, topic_id

log = logging.getLogger(__name__)

APP = "smartsolar"

# Sources, best first: the cable has every value, GATT most, the advertisements a few
PRIORITY = {"vedirect": 0, "gatt": 1, "advert": 2}

# A callable that runs `callback` after `delay` seconds, returning something with cancel()
Scheduler = Callable[[float, Callable[[], None]], Any]


def topic(hostname: str, name: str, fmt: str) -> str:
    return f"{hostname}/{APP}/{name}/{fmt}"


def make_client() -> mqtt.Client:
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


def default_scheduler(delay: float, callback: Callable[[], None]) -> Any:
    """On the daemon's event loop when called from it, else on a timer thread."""
    try:
        return asyncio.get_running_loop().call_later(delay, callback)
    except RuntimeError:
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        return timer


class MqttOutput:
    def __init__(
        self,
        config: MqttConfig,
        hostname: str | None = None,
        client: Any | None = None,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        scheduler: Scheduler = default_scheduler,
    ) -> None:
        self._qos = config.qos
        self._interval = config.publish_interval_s
        self._hostname = hostname or socket.gethostname()
        self._formats = [f for f in ENCODERS if getattr(config, f)]
        self._state_formats = [f for f in ("json", "cbor") if getattr(config, f)]
        self._clock = clock or Clock()
        self._monotonic = monotonic
        self._scheduler = scheduler
        self._lock = threading.RLock()
        # rate limit, per device: when it was last published, the best reading held
        # back since (with its source's rank), the flush scheduled for it, and when
        # each source last delivered
        self._last: dict[str, float] = {}
        self._pending: dict[str, tuple[int, Record]] = {}
        self._timers: dict[str, Any] = {}
        self._seen: dict[str, dict[int, float]] = {}
        self._client = client if client is not None else make_client()
        for fmt in self._state_formats:
            self._client.will_set(
                topic(self._hostname, "state", fmt), self._encode_map({"state": "offline"}, fmt), qos=self._qos, retain=True
            )
        # connect_async: the daemon must start even if the broker is not up yet.
        self._client.connect_async(config.host, config.port)
        self._client.loop_start()

    @staticmethod
    def _encode_map(payload: dict[str, Any], fmt: str) -> bytes:
        if fmt == "cbor":
            return cbor2.dumps(payload)
        return json.dumps(payload, default=str).encode("utf-8")

    def _publish(self, name: str, record: Record) -> None:
        for fmt in self._formats:
            self._client.publish(topic(self._hostname, name, fmt), ENCODERS[fmt](record), qos=self._qos, retain=False)

    def _send(self, device: str, record: Record) -> None:
        timer = self._timers.pop(device, None)
        if timer is not None:
            timer.cancel()
        self._pending.pop(device, None)
        self._last[device] = self._monotonic()
        self._publish(topic_id(device), record)

    def _schedule(self, device: str, delay: float) -> None:
        if device not in self._timers:
            self._timers[device] = self._scheduler(max(delay, 0.0), lambda: self._flush(device))

    def emit_reading(self, reading: Reading) -> None:
        record = reading_record(reading, self._clock.synced())
        device = reading.device
        rank = PRIORITY.get(reading.block, len(PRIORITY))
        with self._lock:
            now = self._monotonic()
            seen = self._seen.setdefault(device, {})
            seen[rank] = now
            last = self._last.get(device)
            if self._interval <= 0 or last is None:
                self._send(device, record)
                return
            # a better source that delivered within the last interval is still active
            better = [t for r, t in seen.items() if r < rank and now - t < self._interval]
            if now - last >= self._interval and not better:
                self._send(device, record)
                return
            # hold it back; the best source held back wins, the newest within a source
            held = self._pending.get(device)
            if held is None or rank <= held[0]:
                self._pending[device] = (rank, record)
            if now - last < self._interval:
                self._schedule(device, last + self._interval - now)
            else:
                # due, but a better source is active: wait for it, at most until it counts as gone
                self._schedule(device, max(better) + self._interval - now)

    def _flush(self, device: str) -> None:
        with self._lock:
            self._timers.pop(device, None)
            held = self._pending.get(device)
            if held is not None:
                self._send(device, held[1])

    def emit_event(self, event: Event) -> None:
        self._publish(EVENTS, event_record(event, self._clock.synced()))

    def emit_state(self, state: dict[str, Any]) -> None:
        for fmt in self._state_formats:
            self._client.publish(topic(self._hostname, "state", fmt), self._encode_map(state, fmt), qos=self._qos, retain=True)

    def close(self) -> None:
        # what is held back goes out before the connection closes
        with self._lock:
            for device in list(self._pending):
                self._send(device, self._pending[device][1])
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        self._client.disconnect()
        self._client.loop_stop()
