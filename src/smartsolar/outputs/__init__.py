from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol

from smartsolar.reading import Event, Reading

log = logging.getLogger(__name__)


class Output(Protocol):
    def emit_reading(self, reading: Reading) -> None: ...

    def emit_event(self, event: Event) -> None: ...

    def emit_state(self, state: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class Outputs:
    """Fan out to all sinks. A failing sink never affects the others or the caller."""

    def __init__(self, sinks: Iterable[Output] = ()) -> None:
        self.sinks = list(sinks)

    def emit_reading(self, reading: Reading) -> None:
        for sink in self.sinks:
            self._call(sink, "emit_reading", reading)

    def emit_event(self, event: Event) -> None:
        for sink in self.sinks:
            self._call(sink, "emit_event", event)

    def emit_state(self, state: dict[str, Any]) -> None:
        for sink in self.sinks:
            self._call(sink, "emit_state", state)

    def close(self) -> None:
        for sink in self.sinks:
            self._call(sink, "close")

    @staticmethod
    def _call(sink: Output, method: str, *args: Any) -> None:
        try:
            getattr(sink, method)(*args)
        except Exception:
            log.exception("output %s failed in %s", type(sink).__name__, method)
