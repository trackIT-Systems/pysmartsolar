"""The serial port of a VE.Direct cable. Only this module imports pyserial."""

from __future__ import annotations

import asyncio
import glob
import logging
import re
import sys
from pathlib import Path
from typing import Protocol

from smartsolar.errors import ConnectError, NotConnectedError

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 0.5


class SerialLink(Protocol):
    async def read(self) -> bytes:
        """Wait for bytes and return what is there. Raises NotConnectedError if the port is gone."""

    async def write(self, data: bytes) -> None:
        """Raises NotConnectedError if the port is gone."""

    async def close(self) -> None: ...


class SerialOpener(Protocol):
    async def __call__(self, port: str, baud: int) -> SerialLink:
        """Open the port. Raises ConnectError (``not_found``: it does not exist)."""


class PySerialLink:
    """A pyserial port, read in a thread so the event loop is never blocked."""

    def __init__(self, ser) -> None:  # noqa: ANN001  (serial.Serial)
        self._ser = ser
        self._closed = False

    async def read(self) -> bytes:
        import serial

        while True:
            if self._closed:
                raise NotConnectedError("the serial port is closed")
            try:
                data = await asyncio.to_thread(self._read_once)
            except (serial.SerialException, OSError, TypeError, ValueError) as exc:
                raise NotConnectedError(f"serial read failed: {exc}") from exc
            if data:
                return data

    def _read_once(self) -> bytes:
        first = self._ser.read(1)  # blocks up to READ_TIMEOUT_S
        if not first:
            return b""
        waiting = self._ser.in_waiting
        return first + (self._ser.read(waiting) if waiting else b"")

    async def write(self, data: bytes) -> None:
        import serial

        try:
            await asyncio.to_thread(self._write, data)
        except (serial.SerialException, OSError, TypeError, ValueError) as exc:
            raise NotConnectedError(f"serial write failed: {exc}") from exc

    def _write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    async def close(self) -> None:
        self._closed = True
        try:
            await asyncio.to_thread(self._ser.close)
        except Exception as exc:
            log.debug("Cannot close the serial port: %s", exc)


def is_pattern(port: str) -> bool:
    """Whether ``port`` is a pattern such as /dev/ttyUSBvd* rather than one port."""
    return any(c in port for c in "*?[")


def resolve_port(port: str) -> str:
    """``port`` itself, or for a pattern such as /dev/ttyUSBvd* the first port that matches.

    The match is looked for at every open, so a cable that is plugged in later (or into another
    USB port, under another index) is found. With several matches the lowest index wins: the
    daemon follows one device.
    """
    if not is_pattern(port):
        return port
    matches = sorted(glob.glob(port), key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p)])
    if not matches:
        raise ConnectError(f"no serial port matches {port}", not_found=True)
    return matches[0]


async def open_pyserial(port: str, baud: int) -> SerialLink:
    """Open ``port`` (a pattern is resolved, symlinks such as /dev/ttyUSBvd0 are followed) for
    exclusive use: a second reader would take bytes away from us and corrupt both."""
    import serial

    port = resolve_port(port)
    path = Path(port)
    real = str(path.resolve()) if path.exists() else port  # not for COM3 and the like
    # exclusive is POSIX only; Windows opens a COM port exclusively anyway
    options = {} if sys.platform.startswith("win") else {"exclusive": True}
    try:
        ser = await asyncio.to_thread(
            lambda: serial.Serial(real, baud, bytesize=8, parity="N", stopbits=1, timeout=READ_TIMEOUT_S, **options)
        )
    except (serial.SerialException, OSError) as exc:
        missing = isinstance(exc, FileNotFoundError) or not path.exists()
        raise ConnectError(f"cannot open {port}: {exc}", not_found=missing) from exc
    return PySerialLink(ser)
