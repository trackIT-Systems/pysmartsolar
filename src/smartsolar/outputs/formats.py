"""One record (a reading or an event) and its CSV, JSON and CBOR encodings.

All three carry the same columns in the same order: `time`, `synced`, `device`,
then the reading's columns (see columns.py), the same for every source. The CSV
files add a `block` column (which source the reading came from) before them;
MQTT does not say. CBOR is a list without key names, the smallest over
cellular; the column order is what the CSV header and the JSON keys show.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import cbor2

from smartsolar.columns import COLUMNS, EVENT_COLUMNS, row
from smartsolar.reading import Event, Reading

LEADING_COLUMNS = ("time", "synced", "device")
READINGS = "readings"
EVENTS = "events"


@dataclass(frozen=True)
class Record:
    # What the record is: "readings" or "events"
    key: str
    columns: tuple[str, ...]
    values: tuple[Any, ...]
    # Timezone-aware, whole seconds
    time: datetime
    # Whether the wall clock was synced when the record was emitted
    synced: bool
    device: str | None
    # CLOCK_BOOTTIME of the reading; only used to re-time records taken before the sync
    boottime: float

    @property
    def header(self) -> list[str]:
        return [*LEADING_COLUMNS, *self.columns]

    @property
    def row(self) -> list[Any]:
        return [self.time, self.synced, self.device, *self.values]


def _second(when: datetime) -> datetime:
    return when.astimezone().replace(microsecond=0)


def reading_record(reading: Reading, synced: bool, with_block: bool = False) -> Record:
    """A reading's record; `with_block` adds the source as the first column (for the CSV files)."""
    if with_block:
        return Record(READINGS, ("block", *COLUMNS), (reading.block, *row(reading)), _second(reading.time), synced, reading.device, reading.boottime)
    return Record(READINGS, COLUMNS, row(reading), _second(reading.time), synced, reading.device, reading.boottime)


def event_record(event: Event, synced: bool) -> Record:
    return Record(EVENTS, EVENT_COLUMNS, (event.kind, event.detail()), _second(event.time), synced, event.device, event.boottime)


def _text(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def csv_line(values: list[Any]) -> str:
    """One `;`-separated line without line ending; None becomes an empty field."""
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=";", lineterminator="\n").writerow([_text(value) for value in values])
    return buffer.getvalue().rstrip("\n")


def encode_csv(record: Record) -> bytes:
    return csv_line(record.row).encode()


def encode_json(record: Record) -> bytes:
    return json.dumps(dict(zip(record.header, [_text(value) for value in record.row], strict=True)), default=str).encode()


def encode_cbor(record: Record) -> bytes:
    # A timezone-aware datetime is encoded as tag 0, an RFC 3339 string with offset.
    # canonical: each float in the smallest form that keeps it exact (24.0 in 3 bytes, not 9)
    return cbor2.dumps(record.row, canonical=True)


ENCODERS = {"csv": encode_csv, "json": encode_json, "cbor": encode_cbor}
