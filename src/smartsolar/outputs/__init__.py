from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol

from smartsolar.failures import Failures
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
        self._failures = Failures(log)

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

    def _call(self, sink: Output, method: str, *args: Any) -> None:
        name = type(sink).__name__
        key = f"{name}.{method}"
        try:
            getattr(sink, method)(*args)
        except Exception:
            self._failures.exception(key, "Output %s failed in %s", name, method)
        else:
            self._failures.succeeded(key, "Output %s works again in %s", name, method)
