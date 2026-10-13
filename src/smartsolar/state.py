"""What the daemon knows right now: cached readings and statistics."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from smartsolar.reading import Reading, utc_iso

BLOCKS = ("advert", "gatt", "vedirect")


@dataclass
class BlockState:
    reading: Reading | None = None
    ok_at: float | None = None  # monotonic
    last_error: str | None = None


class Cache:
    """The last reading per source ("advert", "gatt", "vedirect"), with age and staleness.

    ``stale_after`` gives the age in seconds from which a block counts as stale; None means the
    source is not in use and the block is never reported as stale.
    """

    def __init__(self, stale_after: Mapping[str, float | None], clock: Callable[[], float] = time.monotonic) -> None:
        self._stale_after = dict(stale_after)
        self._clock = clock
        self.blocks: dict[str, BlockState] = {name: BlockState() for name in BLOCKS}

    def update(self, reading: Reading) -> None:
        state = self.blocks[reading.block]
        state.reading = reading
        state.ok_at = self._clock()
        state.last_error = None

    def fail(self, block: str, error: str) -> None:
        self.blocks[block].last_error = error

    def clear(self, *names: str) -> None:
        """Forget the given blocks (all if none are given), e.g. the BLE ones after switching to another device."""
        for name in names or BLOCKS:
            self.blocks[name] = BlockState()

    def age_s(self, block: str) -> float | None:
        ok_at = self.blocks[block].ok_at
        return None if ok_at is None else self._clock() - ok_at

    def is_stale(self, block: str) -> bool:
        limit = self._stale_after.get(block)
        if limit is None:
            return False
        age = self.age_s(block)
        return age is None or age > limit

    def describe(self, block: str) -> dict[str, Any]:
        state = self.blocks[block]
        age = self.age_s(block)
        return {
            "enabled": self._stale_after.get(block) is not None,
            "stale_after_s": self._stale_after.get(block),
            "last_ok": utc_iso(state.reading.time) if state.reading else None,
            "age_s": None if age is None else round(age, 1),
            "stale": self.is_stale(block),
            "last_error": state.last_error,
        }


@dataclass
class AdvertStats:
    """Counters for ``smartsolar status``."""

    started: datetime
    seen: int = 0  # advertisements of the selected device
    decoded: int = 0
    no_key: int = 0
    rejected: int = 0  # check byte did not match the key
    undecodable: int = 0


@dataclass
class GattStats:
    connect_attempts: int = 0
    connects: int = 0
    disconnects: int = 0
    pairings: int = 0
    polls: int = 0
    last_error: str | None = None
    backoff_s: float | None = None


@dataclass
class SerialStats:
    opens: int = 0
    losses: int = 0
    hex_requests: int = 0
    hex_timeouts: int = 0
    last_error: str | None = None
    backoff_s: float | None = None
