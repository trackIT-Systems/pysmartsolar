"""One open GATT link to a Victron device: handshake, keepalive and the values pushed to us."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from smartsolar.ble import GattLink
from smartsolar.errors import NotConnectedError, ProtocolError, SmartsolarError, WriteFailed
from smartsolar.vreg import INIT_SEQUENCE, PING, VREG_KEY, RecordParser, get_request, set_request

log = logging.getLogger(__name__)

INIT_WRITE_GAP_S = 0.05
ACK = bytes.fromhex("f901")


class GattSession:
    def __init__(
        self,
        link: GattLink,
        *,
        keepalive_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.link = link
        self._keepalive_s = keepalive_s
        self._clock = clock
        self._parsers = {"0024": RecordParser(), "0027": RecordParser()}
        self.values: dict[int, Any] = {}  # latest raw value per register
        self.updated: dict[int, float] = {}  # monotonic time of the latest value
        self.errors: dict[int, int] = {}  # registers the device answered with an error code
        self.acks = 0
        self.lost = asyncio.Event()
        self.unavailable: set[int] = set()  # registers the device refused to read
        self._waiters: dict[int, list[tuple[Callable[[Any], bool], asyncio.Future]]] = {}
        self._keepalive_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Enable notifications, run the init sequence and keep the link alive."""
        await self.link.start_notify(self._on_notify)
        for char, hex_data in INIT_SEQUENCE:
            await self.link.write(char, bytes.fromhex(hex_data))
            await asyncio.sleep(INIT_WRITE_GAP_S)
        if self._keepalive_s > 0:
            self._keepalive_task = asyncio.create_task(self._keepalive(), name="victron-keepalive")

    async def _keepalive(self) -> None:
        # The ping sets a 10 s timeout: without a new one the device closes the connection.
        while True:
            await asyncio.sleep(self._keepalive_s)
            try:
                await self.link.write("0024", PING)
            except SmartsolarError as exc:
                log.debug("The GATT keepalive ping failed, stopping it: %s", exc)
                return

    def on_disconnect(self) -> None:
        self.lost.set()
        for waiters in self._waiters.values():
            for _, future in waiters:
                if not future.done():
                    future.set_exception(NotConnectedError("link lost"))
        self._waiters.clear()

    def _on_notify(self, char: str, data: bytes) -> None:
        if char == "0021":
            if data == ACK:
                self.acks += 1
            return
        parser = self._parsers.get(char)
        if parser is None:
            return
        now = self._clock()
        for record in parser.feed(data):
            if record.vreg is None:
                continue
            if record.kind == 0x08:
                self.values[record.vreg] = record.value
                self.updated[record.vreg] = now
                pending = self._waiters.pop(record.vreg, [])
                for predicate, future in pending:
                    if future.done():
                        continue
                    if predicate(record.value):
                        future.set_result(record.value)
                    else:
                        self._waiters.setdefault(record.vreg, []).append((predicate, future))
            elif record.kind == 0x09:
                self.errors[record.vreg] = record.value if isinstance(record.value, int) else -1
                for _, future in self._waiters.pop(record.vreg, []):
                    if not future.done():
                        future.set_exception(ProtocolError(f"register 0x{record.vreg:04x} is not available (code {self.errors[record.vreg]})"))

    async def wait_for(self, vreg: int, timeout_s: float) -> Any:
        """The value of a register, waiting for it if it has not arrived yet."""
        if vreg in self.values:
            return self.values[vreg]
        if self.lost.is_set():
            raise NotConnectedError("link lost")
        return await self._await_value(vreg, lambda _: True, timeout_s)

    async def _await_value(self, vreg: int, predicate: Callable[[Any], bool], timeout_s: float) -> Any:
        future = self._expect(vreg, predicate)
        return await self._wait(future, vreg, timeout_s)

    def _expect(self, vreg: int, predicate: Callable[[Any], bool]) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(vreg, []).append((predicate, future))
        return future

    @staticmethod
    async def _wait(future: asyncio.Future, vreg: int, timeout_s: float) -> Any:
        try:
            return await asyncio.wait_for(future, timeout_s)
        except TimeoutError:
            raise ProtocolError(f"register 0x{vreg:04x} was not answered within {timeout_s:g} s") from None

    async def request(self, vreg: int, timeout_s: float) -> Any:
        """Read a register now (the value that arrives after the request, not a cached one)."""
        future = self._expect(vreg, lambda _: True)
        await self.link.write("0024", get_request(vreg))
        return await self._wait(future, vreg, timeout_s)

    async def refresh(self, vregs: Iterable[int], timeout_s: float = 1.0) -> None:
        """Read registers one after the other. Ones the device refuses are not asked for again."""
        for vreg in vregs:
            if vreg in self.unavailable:
                continue
            try:
                await self.request(vreg, timeout_s)
            except NotConnectedError:
                raise
            except ProtocolError as exc:
                if vreg in self.errors:
                    self.unavailable.add(vreg)
                log.debug("Cannot refresh register 0x%04x over GATT: %s", vreg, exc)

    async def write_register(self, vreg: int, raw: bytes, timeout_s: float) -> Any:
        """Write a register. The device answers with a value record echoing what it stored."""
        future = self._expect(vreg, lambda value: bytes(value) == raw if isinstance(value, (bytes, bytearray)) else False)
        await self.link.write("0024", set_request(vreg, raw))
        try:
            return await asyncio.wait_for(future, timeout_s)
        except TimeoutError:
            raise WriteFailed(f"register 0x{vreg:04x}: the device did not confirm the write (read-only, or the value was refused)") from None
        except ProtocolError as exc:
            raise WriteFailed(str(exc)) from None

    async def fetch_key(self, timeout_s: float) -> bytes:
        raw = await self.wait_for(VREG_KEY, timeout_s)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != 16:
            raise ProtocolError("the key register did not hold 16 bytes")
        return bytes(raw)

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None
        try:
            await self.link.disconnect()
        except Exception as exc:
            log.debug("Cannot close the GATT session: %s", exc)
