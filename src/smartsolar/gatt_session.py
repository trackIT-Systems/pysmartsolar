"""One open GATT link to a Victron device: the VE.Smart session, the keepalive and the values pushed to us."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from smartsolar import vesmart
from smartsolar.ble import GattLink
from smartsolar.errors import (
    NotConnectedError,
    ProtocolError,
    SmartsolarError,
    WriteFailed,
)
from smartsolar.vesmart import ROOT, VREG_KEEPALIVE, Message, Op
from smartsolar.vreg import (
    VREG_FIRMWARE,
    VREG_KEY,
    VREG_MAC,
    VREG_PRODUCT_ID,
    product_id,
)

log = logging.getLogger(__name__)

START_TIMEOUT_S = 10.0
ROOT_VREGS = (VREG_KEY, VREG_MAC)  # registers of the Bluetooth part that belong to the charger's values


class GattSession:
    def __init__(
        self,
        link: GattLink,
        *,
        keepalive_s: float,
        model: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.link = link
        self._keepalive_s = keepalive_s
        self._model = model  # the product id of the advertisement: picks the instance of the charger
        self._clock = clock
        self.transport = vesmart.Transport(link, self._on_message)
        self.devices: list[tuple[int, int]] = []  # (instance, parent)
        self.instance = ROOT  # the instance with the charger's registers
        self.product_ids: dict[int, int | None] = {}
        self._by_instance: dict[int, dict[int, Any]] = {}
        self.updated: dict[int, float] = {}  # monotonic time of the latest value of the charger
        self.errors: dict[int, int] = {}  # registers of the charger the device answered with an error code
        self.unavailable: set[int] = set()  # registers the device refused to read
        self.keepalives = 0  # keepalives the device confirmed
        self.lost = asyncio.Event()
        self._device_list: asyncio.Future | None = None
        self._waiters: dict[tuple[int, int], list[tuple[Callable[[Any], bool], asyncio.Future]]] = {}
        self._keepalive_task: asyncio.Task | None = None

    @property
    def values(self) -> dict[int, Any]:
        """The latest raw value of every register of the charger, and the MAC (and key) of the Bluetooth part."""
        root = self._by_instance.get(ROOT, {})
        merged = {vreg: root[vreg] for vreg in ROOT_VREGS if vreg in root}
        merged.update(self._by_instance.get(self.instance, {}))
        return merged

    async def start(self) -> None:
        """Open the transport, get the device list, subscribe to every instance and find the charger."""
        await self.link.start_notify(self.transport.feed)
        await self.transport.open()
        self._device_list = asyncio.get_running_loop().create_future()
        await self.transport.send(vesmart.get_devices())
        self.devices = await self._wait(self._device_list, "the device list", START_TIMEOUT_S)
        for instance, _ in self.devices:
            await self.transport.send(vesmart.subscribe(instance))
        if self._keepalive_s > 0:
            # VictronConnect sends it with the subscription of instance 0; the timer starts with its echo
            await self.transport.send(vesmart.keepalive())
        self.instance = await self._find_charger()
        await self.transport.send(vesmart.get_values(ROOT, [VREG_MAC]))
        log.debug("VE.Smart devices %s, product ids %s: the charger is instance %d", self.devices, self.product_ids, self.instance)

    async def _find_charger(self) -> int:
        """The instance whose product id is the advertised one; else the deepest one of the tree."""
        for instance, _ in self.devices:
            try:
                raw = await self._request(instance, VREG_PRODUCT_ID, START_TIMEOUT_S, also=(VREG_FIRMWARE,))
            except ProtocolError as exc:
                log.debug("Instance %d has no product id: %s", instance, exc)
                continue
            self.product_ids[instance] = product_id(raw)
        if self._model is not None:
            for instance, pid in self.product_ids.items():
                if pid == self._model:
                    return instance
        parents = dict(self.devices)

        def depth(instance: int) -> int:
            n = 0
            while parents.get(instance, instance) != instance and n < len(parents):
                instance = parents[instance]
                n += 1
            return n

        return max((i for i, _ in self.devices), key=depth, default=ROOT)

    def _start_keepalive(self) -> None:
        if self._keepalive_task is None and self._keepalive_s > 0 and not self.lost.is_set():
            self._keepalive_task = asyncio.get_running_loop().create_task(self._keepalive(), name="victron-keepalive")

    async def _keepalive(self) -> None:
        # The keepalive sets a 10 s timeout: without a new one the device closes the connection.
        while True:
            await asyncio.sleep(self._keepalive_s)
            try:
                await self.transport.send(vesmart.keepalive())
            except SmartsolarError as exc:
                log.debug("The GATT keepalive failed, stopping it: %s", exc)
                return

    def on_disconnect(self) -> None:
        self.lost.set()
        self.transport.close()
        if self._device_list is not None and not self._device_list.done():
            self._device_list.set_exception(NotConnectedError("link lost"))
            self._device_list.exception()  # retrieved: start() may already have given up on it
        for waiters in self._waiters.values():
            for _, future in waiters:
                if not future.done():
                    future.set_exception(NotConnectedError("link lost"))
        self._waiters.clear()

    def _on_message(self, message: Message) -> None:
        if message.op == Op.DEVICE_LIST:
            flat = message.args[0] if isinstance(message.args[0], list) else []
            devices = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
            if self._device_list is not None and not self._device_list.done():
                self._device_list.set_result(devices or [(ROOT, ROOT)])
        elif message.op == Op.VALUE:
            vreg, value = message.args
            self._value(message.instance, vreg, value)
        elif message.op == Op.VALUE_RESPONSE:
            vreg, code = message.args
            if message.instance == ROOT and vreg == VREG_KEEPALIVE:
                log.debug("The device does not take the keepalive (code %s)", code)
                return
            if message.instance == self.instance:
                self.errors[vreg] = code if isinstance(code, int) else -1
            for _, future in self._waiters.pop((message.instance, vreg), []):
                if not future.done():
                    future.set_exception(ProtocolError(f"register 0x{vreg:04x} is not available (code {code})"))
        elif message.op == Op.RESPONSE:
            opcode, result = message.args
            if result != 0:
                log.debug("VE.Smart opcode %s for instance %s failed with %s", opcode, message.instance, result)

    def _value(self, instance: int | None, vreg: int, value: Any) -> None:
        if instance is None:
            return
        if instance == ROOT and vreg == VREG_KEEPALIVE:
            self.keepalives += 1
            self._start_keepalive()
        self._by_instance.setdefault(instance, {})[vreg] = value
        if instance == self.instance:
            self.updated[vreg] = self._clock()
        pending = self._waiters.pop((instance, vreg), [])
        for predicate, future in pending:
            if future.done():
                continue
            if predicate(value):
                future.set_result(value)
            else:
                self._waiters.setdefault((instance, vreg), []).append((predicate, future))

    async def wait_for(self, vreg: int, timeout_s: float) -> Any:
        """The value of a register of the charger, waiting for it if it has not arrived yet."""
        if vreg in self.values:
            return self.values[vreg]
        if self.lost.is_set():
            raise NotConnectedError("link lost")
        future = self._expect(self.instance, vreg, lambda _: True)
        return await self._wait(future, f"register 0x{vreg:04x}", timeout_s)

    def _expect(self, instance: int, vreg: int, predicate: Callable[[Any], bool]) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault((instance, vreg), []).append((predicate, future))
        return future

    async def _send(self, future: asyncio.Future, data: bytes) -> None:
        """Write a request whose answer ``future`` waits for. If the write fails, nobody waits for it."""
        try:
            await self.transport.send(data)
        except BaseException:
            self._forget(future)
            raise

    def _forget(self, future: asyncio.Future) -> None:
        for key, waiters in list(self._waiters.items()):
            self._waiters[key] = [(p, f) for p, f in waiters if f is not future]
            if not self._waiters[key]:
                del self._waiters[key]
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            # A lost link already gave it its error (on_disconnect, often during the failing write):
            # retrieve it, or asyncio logs "Future exception was never retrieved"
            future.exception()

    async def _wait(self, future: asyncio.Future, what: str, timeout_s: float) -> Any:
        try:
            return await asyncio.wait_for(future, timeout_s)
        except TimeoutError:
            self._forget(future)
            raise ProtocolError(f"{what} was not answered within {timeout_s:g} s") from None

    async def _request(self, instance: int, vreg: int, timeout_s: float, also: Iterable[int] = ()) -> Any:
        """Read a register of an instance now (the value that arrives after the request, not a cached one)."""
        future = self._expect(instance, vreg, lambda _: True)
        await self._send(future, vesmart.get_values(instance, [vreg, *also]))
        return await self._wait(future, f"register 0x{vreg:04x}", timeout_s)

    async def request(self, vreg: int, timeout_s: float) -> Any:
        """Read a register of the charger now."""
        return await self._request(self.instance, vreg, timeout_s)

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
        """Write a register of the charger. The device answers with a value that echoes what it stored."""
        future = self._expect(self.instance, vreg, lambda value: bytes(value) == raw if isinstance(value, (bytes, bytearray)) else False)
        await self._send(future, vesmart.set_value(self.instance, vreg, raw))
        try:
            return await asyncio.wait_for(future, timeout_s)
        except TimeoutError:
            self._forget(future)
            raise WriteFailed(f"register 0x{vreg:04x}: the device did not confirm the write (read-only, or the value was refused)") from None
        except ProtocolError as exc:
            raise WriteFailed(str(exc)) from None

    async def fetch_key(self, timeout_s: float) -> bytes:
        """The Instant Readout key, a register of the Bluetooth part (instance 0)."""
        raw = await self._request(ROOT, VREG_KEY, timeout_s)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != 16:
            raise ProtocolError("the key register did not hold 16 bytes")
        return bytes(raw)

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None
        self.transport.close()
        try:
            await self.link.disconnect()
        except Exception as exc:
            log.debug("Cannot close the GATT session: %s", exc)
