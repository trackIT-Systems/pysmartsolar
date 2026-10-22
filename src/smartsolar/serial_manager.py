"""Follows a SmartSolar on a VE.Direct cable: text blocks as readings, HEX frames for registers.

The port is opened exclusively and reopened with backoff when it goes away (USB unplugged). The
text blocks the device pushes every second become readings of the ``vedirect`` block. The HEX
protocol is only used when a setting is read or written, because a request makes the device cut
the text block it is sending (that one block is lost). With ``serial.hex`` off the daemon never
transmits a byte.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from smartsolar import vedirect
from smartsolar.backoff import Backoff
from smartsolar.config import SerialConfig
from smartsolar.errors import ConnectError, NoDataError, NotConnectedError, ProtocolError, WriteFailed
from smartsolar.failures import Failures
from smartsolar.reading import Event, Reading, Value
from smartsolar.registers import check_write, write_result
from smartsolar.serial_link import SerialLink, is_pattern, open_pyserial
from smartsolar.state import Cache, SerialStats
from smartsolar.vreg import VREG_FIRMWARE, WriteItem
from smartsolar.vedirect import CMD_GET, CMD_SET, HexFrame, StreamParser, TextBlock

log = logging.getLogger(__name__)

CMD_DONE = 0x1
CMD_APP_VERSION = 0x3


class SerialManager:
    def __init__(
        self,
        cfg: SerialConfig,
        *,
        cache: Cache,
        emit_event: Callable[[Event], None],
        stats: SerialStats | None = None,
        opener: Callable[[str, int], Any] = open_pyserial,
        on_serial: Callable[[str | None], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.cache = cache
        self.stats = stats or SerialStats()
        self._emit_event = emit_event
        self._opener = opener
        self._on_serial = on_serial
        self._clock = clock
        self._backoff = Backoff(cfg.reopen_initial_s, cfg.reopen_max_s)
        self._link: SerialLink | None = None
        self._parser = StreamParser()
        self._lock = asyncio.Lock()
        self._waiter: tuple[Callable[[HexFrame], bool], asyncio.Future] | None = None
        self._reported: str | None = None  # the last failure that was published as an event; repeats are only retried
        self._failures = Failures(log)
        self._opened_before = False
        self._warned_no_serial = False
        self.state = "closed"  # closed | open | backoff
        self.serial: str | None = None
        self.product_id: int | None = None
        self.firmware: int | None = None
        self.last_block_mono: float | None = None
        self.last_reading: Reading | None = None
        self.registers: dict[int, bytes] = {}  # the latest raw value of every register the device told us about
        self.unknown_fields: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self._link is not None

    @property
    def hex_enabled(self) -> bool:
        return self.cfg.hex

    @property
    def device_id(self) -> str:
        return self.serial or self.cfg.port

    @property
    def parser_stats(self) -> vedirect.ParserStats:
        return self._parser.stats

    # -- the port -----------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                link = await self._opener(self.cfg.port, self.cfg.baud)
            except ConnectError as exc:
                await self._failed(str(exc), absent=exc.not_found)
                continue
            self._link = link
            self._parser = StreamParser()
            self._reported = None
            self.state = "open"
            self.stats.opens += 1
            if not self._failures.succeeded("port", "Serial port %s works again", self.cfg.port):
                log.log(logging.DEBUG if self._opened_before else logging.INFO, "Opened serial port %s", self.cfg.port)
            self._opened_before = True
            self._event("serial_opened", port=self.cfg.port)
            error = "closed"
            try:
                await self._read_loop(link)
            except NotConnectedError as exc:
                error = str(exc)
            finally:
                self._link = None
                self._fail_waiter(NotConnectedError("the serial port was lost"))
                await link.close()
            self.stats.losses += 1
            await self._failed(error, lost=True)

    @property
    def optional(self) -> bool:
        """A pattern (the default /dev/ttyUSBvd*) means "a cable if there is one"; a fixed port is expected to be there."""
        return is_pattern(self.cfg.port)

    async def _failed(self, message: str, *, absent: bool = False, lost: bool = False) -> None:
        self.stats.last_error = message
        delay = self.stats.backoff_s = self._backoff.next()
        self.state = "backoff"
        # A cable that is not there is retried for ever: say it once, not at every attempt
        repeated = message == self._reported
        self._reported = message
        # A cable that is missing or unplugged is expected with a pattern (INFO), not with a fixed port (WARNING)
        level = logging.INFO if self.optional and (absent or lost) else logging.WARNING
        if lost:
            self._failures.failed("port", message, "Lost serial port %s: %s (retry in %.1f s)", self.cfg.port, message, delay, level=level)
        else:
            self._failures.failed("port", message, "Serial port %s is not available: %s (retry in %.1f s)", self.cfg.port, message, delay, level=level)
        if not repeated:
            self._event("serial_lost", port=self.cfg.port, error=message, retry_in_s=round(delay, 1))
        await asyncio.sleep(delay)

    async def _read_loop(self, link: SerialLink) -> None:
        while True:
            data = await link.read()
            for item in self._parser.feed(data):
                if isinstance(item, TextBlock):
                    self._on_block(item)
                else:
                    self._on_hex(item)

    # -- text blocks --------------------------------------------------------

    def _on_block(self, block: TextBlock) -> None:
        decoded = vedirect.decode_text(block.fields)
        self._backoff.reset()
        self.stats.backoff_s = None
        self.last_block_mono = self._clock()
        self.unknown_fields = decoded.unknown
        if decoded.product_id is not None:
            self.product_id = decoded.product_id
        if decoded.firmware is not None:
            self.firmware = decoded.firmware
        if decoded.serial is None:
            if self.serial is None and not self._warned_no_serial:
                self._warned_no_serial = True
                log.warning("The text block on serial port %s has no SER# field, using the port name as the device id", self.cfg.port)
        elif decoded.serial != self.serial:
            if self.serial is not None:
                self.cache.clear("vedirect")
            self.serial = decoded.serial
            log.info("Serial number %s on serial port %s (product 0x%X, firmware 0x%X)", self.serial, self.cfg.port, self.product_id or 0, self.firmware or 0)
            self._event("serial_device", serial=self.serial, product_id=self.product_id, firmware=self.firmware)
            if self._on_serial is not None:
                self._on_serial(self.serial)
        values = tuple(
            Value(f.name, f.friendly, f.unit, decoded.values[f.name]) for f in vedirect.TEXT_FIELDS if f.name in decoded.values
        )
        self.last_reading = Reading(self.device_id, "vedirect", datetime.now().astimezone(), values)
        self.cache.update(self.last_reading)

    # -- HEX ----------------------------------------------------------------

    def _on_hex(self, frame: HexFrame) -> None:
        if frame.is_register and frame.flags == 0:
            self.registers[frame.vreg] = frame.value  # type: ignore[index]
        if self._waiter is not None and self._waiter[0](frame) and not self._waiter[1].done():
            self._waiter[1].set_result(frame)

    def _fail_waiter(self, exc: Exception) -> None:
        if self._waiter is not None and not self._waiter[1].done():
            self._waiter[1].set_exception(exc)

    async def _exchange(self, frame: bytes, match: Callable[[HexFrame], bool]) -> HexFrame:
        """Send a frame and wait for the answer; one more try if none comes (a text block can swallow it)."""
        for _ in range(2):
            link = self._link
            if link is None:
                raise NotConnectedError("the serial port is not open")
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiter = (match, future)
            self.stats.hex_requests += 1
            try:
                await link.write(frame)
                return await asyncio.wait_for(future, self.cfg.request_timeout_s)
            except TimeoutError:
                self.stats.hex_timeouts += 1
            finally:
                self._waiter = None
        raise ProtocolError(f"no answer on {self.cfg.port} within {self.cfg.request_timeout_s:g} s")

    def _need_hex(self) -> None:
        if not self.cfg.hex:
            raise NotConnectedError("serial.hex is off: the cable is read only")
        if self._link is None:
            raise NotConnectedError("the serial port is not open")

    async def _get(self, vreg: int) -> bytes:
        """One register, in the layout the BLE protocol uses (what vreg.py decodes)."""
        if vreg == VREG_FIRMWARE:  # the HEX protocol has no such register but a command for it
            reply = await self._exchange(vedirect.hex_frame(CMD_APP_VERSION), lambda f: f.command == CMD_DONE and len(f.payload) == 2)
            version = int.from_bytes(reply.payload, "little") & 0x0FFF  # the top nibble tells release from beta
            return b"\x00" + version.to_bytes(2, "little") + b"\x00"
        reply = await self._exchange(vedirect.get_frame(vreg), lambda f: f.command == CMD_GET and f.vreg == vreg)
        if reply.flags:
            raise ProtocolError(f"register 0x{vreg:04x} is not available (flags 0x{reply.flags:02x})")
        return reply.value

    async def read(self, vregs: list[int]) -> dict[int, bytes]:
        """Registers the device has; the ones it does not know are left out."""
        self._need_hex()
        values: dict[int, bytes] = {}
        async with self._lock:
            for vreg in vregs:
                try:
                    values[vreg] = await self._get(vreg)
                except ProtocolError as exc:
                    log.debug("Cannot read register 0x%04x over the serial port: %s", vreg, exc)
        if not values:
            raise NoDataError("the device answered none of the registers")
        return values

    async def write(self, items: list[WriteItem]) -> list[dict[str, Any]]:
        """Check, write and read back each register. Stops at the first one that does not verify."""
        self._need_hex()
        results: list[dict[str, Any]] = []
        async with self._lock:
            for item in items:
                await check_write(self._get, item, via_serial=True)
                try:
                    old_raw: bytes | None = await self._get(item.vreg)
                except ProtocolError:
                    old_raw = None
                reply = await self._exchange(vedirect.set_frame(item.vreg, item.raw), lambda f, v=item.vreg: f.command == CMD_SET and f.vreg == v)
                if reply.flags:
                    raise WriteFailed(f"{item.name}: the device refused the value (flags 0x{reply.flags:02x})")
                result = write_result(item, old_raw, await self._get(item.vreg))
                results.append(result)
                self._event("register_written", name=item.name, old=result["old"], new=result["value"], requested=item.value, ok=result["ok"])
                if not result["ok"]:
                    raise WriteFailed(f"{item.name}: written as {item.value}, but the device reports {result['value']}")
        return results

    # -- reporting ----------------------------------------------------------

    def _event(self, kind: str, **data: Any) -> None:
        try:
            self._emit_event(Event(datetime.now().astimezone(), kind, self.device_id if self.serial else None, data))
        except Exception:
            self._failures.exception("event callback", "Cannot emit the %s event", kind)

    def describe(self) -> dict[str, Any]:
        now = self._clock()
        ps = self._parser.stats
        return {
            "port": self.cfg.port,
            "state": self.state,
            "connected": self.connected,
            "hex": self.cfg.hex,
            "serial": self.serial,
            "product_id": None if self.product_id is None else f"0x{self.product_id:04X}",
            "firmware": None if self.firmware is None else f"{self.firmware >> 8}.{self.firmware & 0xFF:02x}",
            "last_block_age_s": None if self.last_block_mono is None else round(now - self.last_block_mono, 1),
            "opens": self.stats.opens,
            "losses": self.stats.losses,
            "blocks": ps.blocks,
            "bad_checksum": ps.bad_checksum,
            "resyncs": ps.resyncs,
            "hex_frames": ps.hex_frames,
            "bad_hex": ps.bad_hex,
            "hex_requests": self.stats.hex_requests,
            "hex_timeouts": self.stats.hex_timeouts,
            "last_error": self.stats.last_error,
            "backoff_s": None if self.stats.backoff_s is None else round(self.stats.backoff_s, 1),
            "unknown_fields": self.unknown_fields,
        }
