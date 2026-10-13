from __future__ import annotations

import json
import logging
import socket
from typing import Any

import cbor2
import paho.mqtt.client as mqtt

from smartsolar.config import MqttConfig
from smartsolar.reading import Event, Reading, mac12

log = logging.getLogger(__name__)

APP = "smartsolar"


def topic(hostname: str, name: str, fmt: str) -> str:
    return f"{hostname}/{APP}/{name}/{fmt}"


def make_client() -> mqtt.Client:
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


class MqttOutput:
    """Publishes every message as JSON and/or CBOR (same dict, different encoding).

    * ``{host}/smartsolar/{mac12}/{fmt}``: one message per block read
    * ``{host}/smartsolar/state/{fmt}``: connection state, retained; the will sets it to offline
    * ``{host}/smartsolar/events/{fmt}``: connection events
    """

    def __init__(self, config: MqttConfig, hostname: str | None = None, client: Any | None = None) -> None:
        self._qos = config.qos
        self._hostname = hostname or socket.gethostname()
        self._formats = [f for f, on in (("json", config.json), ("cbor", config.cbor)) if on]
        self._client = client if client is not None else make_client()
        for fmt in self._formats:
            self._client.will_set(
                topic(self._hostname, "state", fmt), self._encode({"state": "offline"}, fmt), qos=self._qos, retain=True
            )
        # connect_async: the daemon must start even if the broker is not up yet.
        self._client.connect_async(config.host, config.port)
        self._client.loop_start()

    @staticmethod
    def _encode(payload: dict[str, Any], fmt: str) -> bytes:
        if fmt == "cbor":
            return cbor2.dumps(payload)
        return json.dumps(payload, default=str).encode("utf-8")

    def _publish(self, name: str, payload: dict[str, Any], *, retain: bool = False) -> None:
        for fmt in self._formats:
            self._client.publish(
                topic(self._hostname, name, fmt), self._encode(payload, fmt), qos=self._qos, retain=retain
            )

    def emit_reading(self, reading: Reading) -> None:
        self._publish(mac12(reading.device), reading.payload())

    def emit_event(self, event: Event) -> None:
        self._publish("events", event.payload())

    def emit_state(self, state: dict[str, Any]) -> None:
        self._publish("state", state, retain=True)

    def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
