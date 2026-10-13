"""The optional GATT side: key fetch, held connection or periodic polls.

``GattManager.run()`` is the only task that connects. What it does depends on ``gatt.mode``:

* ``off``: connects only when the Instant Readout key of the selected device is unknown, reads
  it (register 0xEC65), stores it and disconnects.
* ``hold``: keeps one connection open (with the keepalive ping) and publishes the values the
  device pushes.
* ``poll``: connects every ``poll_interval_s``, collects the values, publishes them and disconnects.

While the Wi-Fi access point is up on 2.4 GHz, opening a connection mostly fails (see the wiki),
so failed attempts are retried with a jittered, growing delay. Nothing here touches Wi-Fi.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from smartsolar.ble import Advertisement, BleBackend, GattLink
from smartsolar.config import GattConfig
from smartsolar.errors import ConnectError, NoDataError, NotConnectedError, SmartsolarError, WriteFailed
from smartsolar.gatt_session import GattSession
from smartsolar.reading import Event, Reading
from smartsolar.state import GattStats
from smartsolar.vreg import POLLED_VREGS, VREG_KEY, VREGS, WriteItem, decode_vreg

log = logging.getLogger(__name__)

# Registers that arrive in the first seconds of a session; a poll is complete once they are in.
EXPECTED_VREGS = (0x0102, 0xEC5A, 0xEC66, 0xED8D, 0xEDA9)
MIN_COLLECT_S = 1.0
STABLE_AFTER_S = 60.0
CONNECT_GRACE_S = 10.0
DISCONNECT_TIMEOUT_S = 10.0


class GattState(StrEnum):
    WAITING = "waiting"  # no device selected yet
    IDLE = "idle"  # nothing to do right now
    CONNECTING = "connecting"
    PAIRING = "pairing"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    STOPPED = "stopped"


class _Stopping(Exception):
    """Raised inside run() to unwind when stop() was requested."""


class Backoff:
    """Full-jitter exponential backoff: delay = random() * min(maximum, initial * factor**n)."""

    def __init__(self, initial: float, maximum: float, factor: float = 2.0, rng: Callable[[], float] = random.random) -> None:
        self.initial = initial
        self.maximum = maximum
        self.factor = factor
        self._rng = rng
        self.attempt = 0

    def next(self) -> float:
        ceiling = min(self.maximum, self.initial * self.factor**self.attempt)
        self.attempt += 1
        return self._rng() * ceiling

    def reset(self) -> None:
        self.attempt = 0


class GattManager:
    def __init__(
        self,
        cfg: GattConfig,
        backend: BleBackend,
        device: Callable[[], Advertisement | None],
        key_known: Callable[[str], bool],
        store_key: Callable[[str, bytes], None],
        emit_event: Callable[[Event], None],
        emit_reading: Callable[[Reading], None],
        stats: GattStats,
        *,
        on_state: Callable[[GattState], None] | None = None,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._backend = backend
        self._device = device
        self._key_known = key_known
        self._store_key = store_key
        self._emit_event = emit_event
        self._emit_reading = emit_reading
        self._on_state = on_state
        self._clock = clock
        self.stats = stats
        self._backoff = Backoff(cfg.backoff_initial_s, cfg.backoff_max_s, rng=rng)

        self.state = GattState.WAITING
        self.session: GattSession | None = None
        self.device: Advertisement | None = None
        self.connected_since: datetime | None = None
        self.last_poll: datetime | None = None
        self.next_poll_mono: float | None = None
        self.last_key_fetch: datetime | None = None
        self._last_raw: dict[str, str] = {}

        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._generation = 0
        self._reconnect = False
        self._device_changed = False
        self._poll_futures: list[asyncio.Future] = []
        self._key_futures: list[asyncio.Future] = []
        self._write_requests: list[tuple[list[WriteItem], asyncio.Future]] = []
        self._read_requests: list[tuple[list[int], asyncio.Future]] = []
        self._poll_now = False
        self._next_poll = 0.0
        self._paired: set[str] = set()

    # -- requests from the outside ------------------------------------------

    def request_poll(self) -> asyncio.Future:
        """Poll mode: connect now and collect. The future resolves with the Reading."""
        if self.cfg.mode != "poll":
            raise NoDataError(f"gatt.mode is {self.cfg.mode}, a poll needs mode poll")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._poll_futures.append(future)
        self._poll_now = True
        self._wake.set()
        return future

    def request_key(self) -> asyncio.Future:
        """Connect and read the Instant Readout key now. The future resolves with the key bytes."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._key_futures.append(future)
        self._wake.set()
        return future

    def request_write(self, items: list[WriteItem]) -> asyncio.Future:
        """Connect if needed, write the registers, read them back. Resolves with one result per item."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._write_requests.append((items, future))
        self._wake.set()
        return future

    def request_read(self, vregs: list[int]) -> asyncio.Future:
        """Connect if needed and read registers now. Resolves with {vreg: raw value}."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._read_requests.append((vregs, future))
        self._wake.set()
        return future

    def request_reconnect(self) -> None:
        self._reconnect = True
        self._wake.set()

    def device_changed(self) -> None:
        """The tracker selected another device: drop the link and use the new one."""
        self._device_changed = True
        self._wake.set()

    def poke(self) -> None:
        """Something changed that may create work (e.g. a key was forgotten)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def snapshot(self) -> Reading | None:
        """The known registers the connected device has sent so far (unknown ones are only in ``raw_registers``)."""
        session, device = self.session, self.device
        if session is None or device is None or not session.values:
            return None
        self._last_raw = self._raw(session)
        values = []
        for vreg in sorted(session.values):
            value = decode_vreg(vreg, session.values[vreg]) if vreg in VREGS else None
            if value is not None:
                values.append(value)
        return Reading(device.address, "gatt", datetime.now().astimezone(), tuple(values))

    @staticmethod
    def _raw(session: GattSession) -> dict[str, str]:
        raw = {}
        for vreg in sorted(session.values):
            if vreg == VREG_KEY:
                continue  # a secret
            value = session.values[vreg]
            raw[f"0x{vreg:04x}"] = value.hex() if isinstance(value, (bytes, bytearray)) else str(value)
        return raw

    def raw_registers(self) -> dict[str, str]:
        """Every register received, known or not, as hex (the key excluded). From the last session if none is open."""
        return self._raw(self.session) if self.session is not None else dict(self._last_raw)

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        self._set_state(GattState.WAITING)
        try:
            while not self._stop.is_set():
                adv = self._device()
                if adv is None:
                    self._set_state(GattState.WAITING)
                    await self._sleep(2.0)
                    continue
                if not self._has_work(adv):
                    self._set_state(GattState.IDLE)
                    await self._sleep(self._idle_wait())
                    continue
                await self._session(adv)
        except _Stopping:
            pass
        finally:
            await self._teardown()
            self._set_state(GattState.STOPPED)

    def _needs_key(self, adv: Advertisement) -> bool:
        return bool(self._key_futures) or not self._key_known(adv.address)

    def _has_work(self, adv: Advertisement) -> bool:
        if self._needs_key(adv) or self._write_requests or self._read_requests:
            return True
        if self.cfg.mode == "hold":
            return True
        return self.cfg.mode == "poll" and (self._poll_now or self._clock() >= self._next_poll)

    def _idle_wait(self) -> float:
        if self.cfg.mode == "poll":
            return max(0.5, self._next_poll - self._clock())
        return 30.0

    async def _session(self, adv: Advertisement) -> None:
        """Connect, do the work of the mode, disconnect; back off if it failed."""
        failure = await self._connect_and_work(adv)
        if failure is not None:
            await self._failed(adv, failure)

    async def _connect_and_work(self, adv: Advertisement) -> BaseException | None:
        """Returns the error if the attempt failed. The link is closed before this returns."""
        self._reconnect = False
        self._device_changed = False
        link: GattLink | None = None
        session: GattSession | None = None
        up_since = 0.0
        self._generation += 1
        generation = self._generation
        try:
            self._set_state(GattState.CONNECTING)
            self.stats.connect_attempts += 1
            try:
                link = await self._until_stop(
                    asyncio.wait_for(
                        self._backend.connect(
                            adv,
                            timeout_s=self.cfg.connect_timeout_s,
                            on_disconnect=lambda: self._link_lost(generation),
                            passkey=self.cfg.passkey,
                        ),
                        self.cfg.connect_timeout_s + CONNECT_GRACE_S,
                    )
                )
                self._set_state(GattState.PAIRING)
                await self._until_stop(link.pair(self.cfg.passkey, self.cfg.pair_timeout_s))
                if adv.address not in self._paired:
                    self._paired.add(adv.address)
                    self.stats.pairings += 1
                    self._event("paired", adv.address)
                session = GattSession(link, keepalive_s=self.cfg.keepalive_s)
                self.session = session
                self.device = adv
                await self._until_stop(session.start())
                if session.lost.is_set():
                    raise ConnectError("link lost while starting the session")
            except _Stopping:
                raise
            except (SmartsolarError, TimeoutError, OSError) as exc:
                return exc

            self.stats.connects += 1
            up_since = self._clock()
            self.connected_since = datetime.now().astimezone()
            self._set_state(GattState.CONNECTED)
            self._event("connected", adv.address, rssi=round(adv.rssi), mode=self.cfg.mode)
            try:
                await self._work(adv, session)
            except _Stopping:
                raise
            except (SmartsolarError, TimeoutError) as exc:
                return exc
            if self._clock() - up_since >= STABLE_AFTER_S or self.cfg.mode != "hold":
                self._backoff.reset()
                self.stats.backoff_s = None
            return None
        finally:
            if session is not None:
                self._generation += 1
                reason = "shutdown" if self._stop.is_set() else "done"
                await session.close()
                self.stats.disconnects += 1
                self._event("disconnected", adv.address, reason=reason, up_s=round(self._clock() - up_since) if up_since else 0)
            elif link is not None:
                self._generation += 1
                await self._disconnect_quietly(link)
            self.session = None
            self.connected_since = None

    async def _work(self, adv: Advertisement, session: GattSession) -> None:
        if self._needs_key(adv):
            key = await self._until_stop(session.fetch_key(self.cfg.request_timeout_s))
            self._store_key(adv.address, key)
            self.last_key_fetch = datetime.now().astimezone()
            self._event("key_fetched", adv.address)
            for future in self._key_futures:
                if not future.done():
                    future.set_result(key)
            self._key_futures = []
        await self._do_reads(session)
        await self._do_writes(adv, session)
        if self.cfg.mode == "poll":
            await self._collect(adv, session)
        elif self.cfg.mode == "hold":
            await self._hold(adv, session)

    async def _wait_expected(self, session: GattSession, max_s: float) -> None:
        deadline = self._clock() + max_s
        started = self._clock()
        while self._clock() < deadline and not session.lost.is_set():
            if self._clock() - started >= MIN_COLLECT_S and all(v in session.values for v in EXPECTED_VREGS):
                return
            await self._until_stop(asyncio.sleep(0.1))
        if session.lost.is_set() and not session.values:
            raise NotConnectedError("link lost before any value arrived")

    async def _collect(self, adv: Advertisement, session: GattSession) -> None:
        await self._wait_expected(session, self.cfg.collect_s)
        await self._until_stop(session.refresh(POLLED_VREGS))
        reading = self.snapshot()
        if reading is None:
            raise NoDataError("the device sent no values")
        self.stats.polls += 1
        self.last_poll = datetime.now().astimezone()
        self._poll_now = False
        self._next_poll = self._clock() + self.cfg.poll_interval_s
        self.next_poll_mono = self._next_poll
        self._emit_reading(reading)
        for future in self._poll_futures:
            if not future.done():
                future.set_result(reading)
        self._poll_futures = []

    async def _hold(self, adv: Advertisement, session: GattSession) -> None:
        await self._wait_expected(session, self.cfg.collect_s)
        next_publish = self._clock()
        while True:
            if session.lost.is_set():
                raise NotConnectedError("link lost")
            if self._reconnect or self._device_changed:
                return
            await self._do_reads(session)
            await self._do_writes(adv, session)
            if self._clock() >= next_publish:
                await self._until_stop(session.refresh(POLLED_VREGS))
                reading = self.snapshot()
                if reading is not None:
                    self._emit_reading(reading)
                    self.last_poll = reading.time
                next_publish = self._clock() + self.cfg.publish_interval_s
            await self._sleep(max(0.0, min(1.0, next_publish - self._clock())))

    async def _do_reads(self, session: GattSession) -> None:
        while self._read_requests:
            vregs, future = self._read_requests.pop(0)
            try:
                values = {}
                for vreg in vregs:
                    values[vreg] = await self._until_stop(session.request(vreg, self.cfg.request_timeout_s))
                future.set_result(values)
            except _Stopping:
                if not future.done():
                    future.set_exception(NotConnectedError("shutting down"))
                raise
            except NotConnectedError as exc:
                if not future.done():
                    future.set_exception(exc)
                raise
            except (SmartsolarError, TimeoutError) as exc:
                if not future.done():
                    future.set_exception(exc)

    async def _do_writes(self, adv: Advertisement, session: GattSession) -> None:
        """Run the queued writes: note the old value, write, check the echo, read the register back."""
        while self._write_requests:
            items, future = self._write_requests.pop(0)
            results: list[dict[str, Any]] = []
            try:
                for item in items:
                    old_raw = session.values.get(item.vreg)
                    if old_raw is None:
                        try:
                            old_raw = await self._until_stop(session.request(item.vreg, self.cfg.request_timeout_s))
                        except SmartsolarError:
                            old_raw = None
                    await self._until_stop(session.write_register(item.vreg, item.raw, self.cfg.request_timeout_s))
                    read_raw = await self._until_stop(session.request(item.vreg, self.cfg.request_timeout_s))
                    ok = bytes(read_raw) == item.raw
                    old = decode_vreg(item.vreg, old_raw) if old_raw is not None else None
                    new = decode_vreg(item.vreg, read_raw)
                    result = {
                        "name": item.name,
                        "requested": item.value,
                        "old": old.value if old else None,
                        "value": new.value if new else None,
                        "ok": ok,
                    }
                    results.append(result)
                    self._event("register_written", adv.address, name=item.name, old=result["old"], new=result["value"], requested=item.value, ok=ok)
                    if not ok:
                        raise WriteFailed(f"{item.name}: written as {item.value}, but the device reports {result['value']}")
                future.set_result(results)
            except _Stopping:
                if not future.done():
                    future.set_exception(NotConnectedError("shutting down"))
                raise
            except NotConnectedError as exc:
                if not future.done():
                    future.set_exception(exc)
                raise
            except (SmartsolarError, TimeoutError) as exc:
                if not future.done():
                    future.set_exception(exc)

    async def _failed(self, adv: Advertisement, exc: BaseException) -> None:
        message = str(exc) or type(exc).__name__
        self.stats.last_error = message
        delay = self.stats.backoff_s = self._backoff.next()
        log.info("gatt connection to %s failed: %s (retry in %.1f s)", adv.address, message, delay)
        self._event("gatt_failed", adv.address, error=message, retry_in_s=round(delay, 1))
        self._set_state(GattState.BACKOFF)
        await self._sleep(delay)

    # -- helpers ------------------------------------------------------------

    def _link_lost(self, generation: int) -> None:
        if generation != self._generation:
            return  # a link we already dropped
        if self.session is not None:
            self.session.on_disconnect()
        self._wake.set()

    async def _sleep(self, seconds: float) -> bool:
        """Wait until woken or the time is up. Returns True on timeout. Raises _Stopping on stop."""
        if self._stop.is_set():
            raise _Stopping
        timed_out = False
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            timed_out = True
        self._wake.clear()
        if self._stop.is_set():
            raise _Stopping
        return timed_out

    async def _until_stop(self, awaitable: Any) -> Any:
        """Await something, but abort it (and raise _Stopping) as soon as stop() is called."""
        if self._stop.is_set():
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise _Stopping
        task = asyncio.ensure_future(awaitable)
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            stopper.cancel()
            raise
        if task.done():
            stopper.cancel()
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise _Stopping

    @staticmethod
    async def _disconnect_quietly(link: GattLink) -> None:
        try:
            await asyncio.wait_for(link.disconnect(), DISCONNECT_TIMEOUT_S)
        except Exception as exc:
            log.debug("disconnect: %s", exc)

    async def _teardown(self) -> None:
        for future in self._poll_futures + self._key_futures + [f for _, f in self._write_requests + self._read_requests]:
            if not future.done():
                future.set_exception(NotConnectedError("shutting down"))
        self._poll_futures = []
        self._key_futures = []
        self._write_requests = []
        self._read_requests = []

    def _set_state(self, state: GattState) -> None:
        if state is self.state:
            return
        self.state = state
        log.info("gatt state: %s", state.value)
        if self._on_state is not None:
            try:
                self._on_state(state)
            except Exception:
                log.exception("state callback failed")

    def _event(self, kind: str, device: str | None, **data: Any) -> None:
        try:
            self._emit_event(Event(datetime.now().astimezone(), kind, device, data))
        except Exception:
            log.exception("event callback failed")
