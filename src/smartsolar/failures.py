"""Log a failure once per change of state, following tsOS's logging convention (tsOS-base docs/logging.md).

A failure that repeats (a retry every few seconds, a callback that keeps raising) is logged when it
starts and again only when its error text changes; the repeats go to DEBUG. When it works again,
that is logged once. This is the idea of ``Daemon._failed()`` / ``Daemon._succeeded()`` of
tstelemetry, with the message chosen by the caller.
"""

from __future__ import annotations

import logging
import sys


class Failures:
    """The last logged error per kind of failure (``key``)."""

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger
        self._errors: dict[str, str] = {}

    def failing(self, key: str) -> bool:
        """Whether a failure of this kind was logged and has not worked again since."""
        return key in self._errors

    def failed(self, key: str, error: str, msg: str, *args: object, level: int = logging.WARNING) -> bool:
        """Log ``msg`` at ``level`` if ``error`` differs from the last one of ``key``, else at DEBUG.

        Returns whether it was logged at ``level``.
        """
        first = self._errors.get(key) != error
        self._errors[key] = error
        self._log.log(level if first else logging.DEBUG, msg, *args, stacklevel=2)
        return first

    def exception(self, key: str, msg: str, *args: object) -> bool:
        """In an ``except`` block: ERROR with the traceback the first time, then DEBUG with the error text.

        Returns whether it was logged at ERROR.
        """
        exc = sys.exception()
        error = f"{type(exc).__name__}: {exc}"
        first = self._errors.get(key) != error
        self._errors[key] = error
        if first:
            self._log.error(msg, *args, exc_info=True, stacklevel=2)
        else:
            self._log.debug(msg + " again: %s", *args, error, stacklevel=2)
        return first

    def succeeded(self, key: str, msg: str, *args: object, level: int = logging.INFO) -> bool:
        """Log ``msg`` at ``level`` if a failure of ``key`` was logged before; forget that failure.

        Returns whether it was logged.
        """
        if self._errors.pop(key, None) is None:
            return False
        self._log.log(level, msg, *args, stacklevel=2)
        return True
