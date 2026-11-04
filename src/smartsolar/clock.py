"""Whether the wall clock can be trusted yet.

A station boots with a clock that may be wrong, and stays that way until chrony
has a source: NTP, GNSS, or the RTC once tsscheduled has checked it (tsOS-base
docs/time.md). chrony then clears the kernel's STA_UNSYNC. The daemon runs
anyway; only its CSV rows wait (see outputs/csv.py).
"""

from __future__ import annotations

import ctypes
import logging
import time

log = logging.getLogger(__name__)

# adjtimex(2) return value while STA_UNSYNC (or STA_CLOCKERR) is set
TIME_ERROR = 5


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
        log.debug("Cannot read the clock state with adjtimex: errno %d", ctypes.get_errno())
        return False
    return state != TIME_ERROR


class Clock:
    """The clock the outputs read. Tests replace it with a fake.

    Once synced, the clock stays synced for the rest of the daemon's run: the
    kernel sets STA_UNSYNC again when chrony loses its sources for a while, but
    the clock is still right then.
    """

    def __init__(self) -> None:
        self._synced = False

    def time(self) -> float:
        return time.time()

    def synced(self) -> bool:
        if not self._synced and kernel_clock_synced():
            self._synced = True
        return self._synced
