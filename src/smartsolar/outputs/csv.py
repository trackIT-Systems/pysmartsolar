"""CSV output: one file per calendar day, with the same columns as MQTT.

* ``<path>/<host>/smartsolar/<host>_<YYYY-MM-DD>_smartsolar.csv``: the readings of
  all sources, with a ``block`` column naming the source
* ``<path>/<host>/smartsolar/events/<host>_<YYYY-MM-DD>_smartsolar-events.csv``
  (only the events in ``LOGGED_EVENTS``)

Rows are only written with a trusted clock (chrony has a source: NTP, GNSS or
the checked RTC; tsOS-base docs/time.md), because a wrong clock would put them
into the wrong day's file (or a file dated 1970), with a time nobody can
correct. Until then readings only go over MQTT. Nothing is ever deleted.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from smartsolar.clock import Clock
from smartsolar.outputs.formats import EVENTS, Record, csv_line, event_record, reading_record
from smartsolar.reading import Event, Reading

log = logging.getLogger(__name__)

APP = "smartsolar"
# Events that are useful for analysing the connection afterwards.
LOGGED_EVENTS = {"connected", "disconnected", "device_selected", "device_switched", "key_fetched", "key_rejected", "paired", "register_written", "serial_opened", "serial_lost", "serial_device"}


class CsvOutput:
    def __init__(self, path: Path, hostname: str | None = None, clock: Clock | None = None) -> None:
        self.host = hostname if hostname is not None else socket.gethostname()
        self.dir = Path(path) / self.host / APP
        self.clock = clock or Clock()
        # Whether rows are being dropped, to log when that starts and ends
        self._dropping = False
        # fails at startup, not with the first reading, when the directory can't be made
        self.dir.mkdir(parents=True, exist_ok=True)

    def daily_file(self, key: str, when: datetime) -> Path:
        stem = f"{self.host}_{when:%Y-%m-%d}"
        if key == EVENTS:
            return self.dir / EVENTS / f"{stem}_{APP}-{EVENTS}.csv"
        return self.dir / f"{stem}_{APP}.csv"

    @staticmethod
    def _append(file: Path, header: list[str], row: list[Any]) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8", newline="") as handle:
            if handle.tell() == 0:
                handle.write(csv_line(header) + "\n")
            handle.write(csv_line(row) + "\n")

    def _write(self, record: Record) -> None:
        if not record.synced:
            if not self._dropping:
                log.info("The clock is not trusted yet: rows go to %s once it is", self.dir)
                self._dropping = True
            return
        if self._dropping:
            log.info("The clock is trusted: writing rows to %s", self.dir)
            self._dropping = False
        self._append(self.daily_file(record.key, record.time), record.file_header, record.file_row)

    def emit_reading(self, reading: Reading) -> None:
        self._write(reading_record(reading, self.clock.synced(), with_block=True))

    def emit_event(self, event: Event) -> None:
        if event.kind in LOGGED_EVENTS:
            self._write(event_record(event, self.clock.synced()))

    def close(self) -> None:
        return
