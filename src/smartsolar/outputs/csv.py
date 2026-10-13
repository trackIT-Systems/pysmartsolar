from __future__ import annotations

import csv
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smartsolar.reading import Event, Reading, utc_iso

CSV_FIELDS = ("ts", "device", "block", "key", "value", "unit")
CSV_FILENAME_TIMESTAMP = "%Y-%m-%dT%H%M%S"
# Events that are useful for analysing the connection afterwards.
LOGGED_EVENTS = {"connected", "disconnected", "device_selected", "device_switched", "key_fetched", "key_rejected", "paired", "register_written", "serial_opened", "serial_lost", "serial_device"}


def csv_session_path(base: Path, hostname: str | None = None, when: datetime | None = None) -> Path:
    """`<base>/<hostname>/smartsolar/<hostname>_<timestamp>-smartsolar.csv`."""
    host = hostname if hostname is not None else socket.gethostname()
    ts = (when or datetime.now(timezone.utc)).strftime(CSV_FILENAME_TIMESTAMP)
    return Path(base) / host / "smartsolar" / f"{host}_{ts}-smartsolar.csv"


class CsvOutput:
    """Long format: one row per value, ``block=event`` rows for connection events."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not (self.path.is_file() and self.path.stat().st_size > 0):
            with self.path.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()

    def _write(self, rows: list[dict[str, Any]]) -> None:
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            csv.DictWriter(handle, fieldnames=CSV_FIELDS).writerows(rows)

    def emit_reading(self, reading: Reading) -> None:
        ts = utc_iso(reading.time)
        self._write(
            [
                {"ts": ts, "device": reading.device, "block": reading.block, "key": v.name, "value": v.value, "unit": v.unit}
                for v in reading.values
            ]
        )

    def emit_event(self, event: Event) -> None:
        if event.kind not in LOGGED_EVENTS:
            return
        self._write(
            [
                {
                    "ts": utc_iso(event.time),
                    "device": event.device or "",
                    "block": "event",
                    "key": event.kind,
                    "value": event.detail(),
                    "unit": "",
                }
            ]
        )

    def emit_state(self, state: dict[str, Any]) -> None:
        return

    def close(self) -> None:
        return
