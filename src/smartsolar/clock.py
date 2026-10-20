"""Wall clock, boot clock, and whether the wall clock can be trusted yet.

A station without RTC boots with a wrong clock and stays that way until chrony
syncs, which can take long without a network. The daemon runs anyway; every
reading also carries CLOCK_BOOTTIME, so readings taken before the sync can be
re-timed for the CSV files once the offset is known (see outputs/csv.py).
"""

from __future__ import annotations

import ctypes
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# adjtimex(2) return value while STA_UNSYNC (or STA_CLOCKERR) is set
TIME_ERROR = 5


def boottime() -> float:
    """Seconds since boot, counting suspend (CLOCK_BOOTTIME; monotonic where it does not exist)."""
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)) if hasattr(time, "clock_gettime") else time.monotonic()


def kernel_clock_synced() -> bool:
    """Whether the kernel considers the clock synchronized.

    chronyd clears STA_UNSYNC once it is synced. adjtimex with `modes` 0 only
    reads, which needs no privileges. Where adjtimex does not exist (macOS,
    Windows) the clock is assumed to be right.
    """
    try:
        adjtimex = ctypes.CDLL(None, use_errno=True).adjtimex
    except (AttributeError, OSError, TypeError):
        return True
    # struct timex is about 208 bytes on 64-bit Linux; zeroed means modes == 0
    buf = ctypes.create_string_buffer(512)
    state = adjtimex(buf)
    if state == -1:
        log.debug("adjtimex failed: errno %d", ctypes.get_errno())
        return False
    return state != TIME_ERROR


class Clock:
    """The clocks the outputs read. Tests replace it with a fake.

    Once synced, the clock stays synced for the rest of the boot: the kernel sets
    STA_UNSYNC again when chrony loses its sources for a while, but the clock is
    still right then. A marker file `<boot_id>.synced` in `marker_dir` keeps this
    across restarts of the daemon.
    """

    def __init__(self, marker_dir: Path | None = None) -> None:
        self.marker_dir = marker_dir
        self._synced = False

    def time(self) -> float:
        return time.time()

    def boottime(self) -> float:
        return boottime()

    def boot_id(self) -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return "unknown"

    def synced(self) -> bool:
        if self._synced:
            return True
        boot_id = self.boot_id()
        # without a boot id (not Linux) there is no boot to remember it for
        marker = self.marker_dir / f"{boot_id}.synced" if self.marker_dir and boot_id != "unknown" else None
        if (marker is not None and marker.exists()) or kernel_clock_synced():
            self._synced = True
            if marker is not None:
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.touch()
                except OSError as exc:
                    log.debug("cannot write %s: %s", marker, exc)
        return self._synced
