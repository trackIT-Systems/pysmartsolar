"""VE.Smart: the protocol VictronConnect speaks with a Victron device over the GATT service 306b0001.

Taken from VictronConnect 6.43 for macOS (class ``VeSmartService``) and checked on a SmartSolar
75/15 (firmware 1.64); see docs/gatt-protocol.md.

**Transport.** The service has three characteristics: control (306b0002, called ``0021`` here),
lastData (306b0003, ``0024``) and data (306b0004, ``0027``). Both sides send CBOR messages in
chunks. A chunk is at most ``chunk_size`` bytes and goes out in pieces of at most
``max_att_length`` bytes: all but the last on data, the last on lastData. Both values are read
from the control characteristic when the session opens.

Flow control is by credit on the control characteristic: ``F9 n`` allows the other side ``n``
more chunks. The device allows us ``max_free_chunks`` at a time (1 on the SmartSolar), so the
next chunk may only be written after its ``F9 01`` for the previous one. We allow the device
0x80 chunks when the session opens and give back what it used once that is more than 0x40.

**Messages.** A message is a sequence of CBOR items: an opcode, then its arguments. Most
messages name an *instance*: the device is a tree of instances (the device list), and the
registers of the charger itself are in one of them (instance 3 on the SmartSolar, whose
Bluetooth chip is instance 0). Register values are CBOR byte strings in the little-endian
encoding of the VE.Direct HEX protocol.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import cbor2

from smartsolar.ble import GattLink
from smartsolar.errors import NotConnectedError, ProtocolError

log = logging.getLogger(__name__)

CONTROL = "0021"
LAST_DATA = "0024"
DATA = "0027"


class Op(IntEnum):
    GET_DEVICES = 1  # ()
    DEVICE_LIST = 2  # ([instance, parent, instance, parent, ...])
    SUBSCRIBE = 3  # (instance): the device then pushes the values of the instance as they change
    UNSUBSCRIBE = 4  # (instance): answered with RESPONSE; the pushes stop
    GET_VALUES = 5  # (instance, [vreg, ...])
    SET_VALUES = 6  # (instance, [vreg, bytes]): answered with a VALUE that echoes the stored value
    RESPONSE = 7  # (instance, opcode, result): 0 is success
    VALUE = 8  # (instance, vreg, bytes)
    VALUE_RESPONSE = 9  # (instance, vreg, result): a get or set of the register failed, see RESULTS
    # The "path" interface of devices whose parameters have FLAG_PATHS; not used by the SmartSolar.
    GET_PATH_LIST = 10  # (instance)
    GET_PATH_VALUES = 11  # (instance, [path index, ...])
    SET_PATH_VALUE = 12  # (instance, [path index, value])
    PATH_LIST = 13
    NEW_PATH = 14
    PATH_VALUE = 15
    PATH_VALUE_RESPONSE = 16


# ValueResponse results seen on a SmartSolar 75/15
RESULTS = {
    1: "unknown register",
    3: "access denied (a write to a read-only register, a read of a write-only one)",
    -5: "index out of range",
}


# Arguments after the opcode. Messages with an instance have it as the first argument.
DEVICE_ARGS = {Op.DEVICE_LIST: 1, Op.RESPONSE: 3, Op.VALUE: 3, Op.VALUE_RESPONSE: 3}
REQUEST_ARGS = {Op.GET_DEVICES: 0, Op.SUBSCRIBE: 1, Op.UNSUBSCRIBE: 1, Op.GET_VALUES: 2, Op.SET_VALUES: 2}
_WITHOUT_INSTANCE = {Op.GET_DEVICES, Op.DEVICE_LIST}

# Control opcodes
CTL_ERROR = 0xF7  # from the device: an error code follows (s8, or s16 if longer); VictronConnect drops the link on 3
CTL_RESET = 0xF8  # from the device: drop the partly received chunk
CTL_READY = 0xF9  # both ways: n more chunks may be sent
OPEN = bytes([0xFA, 0x80, 0xFF])  # written once after the parameters were read; constant and unnamed in VictronConnect
FLAG_PATHS = 0x01  # parameters flags: the device has the path interface (VictronConnect then asks for its paths)

INITIAL_GRANT = 0x80
GRANT_AFTER = 0x40  # chunks received before they are given back
FREE_CHUNK_TIMEOUT_S = 10.0  # VictronConnect gives up on a device that allows no chunk for this long

ROOT = 0  # the instance of the Bluetooth part; it holds the key, the MAC and the keepalive
VREG_KEEPALIVE = 0x0093  # VE_REG_CONNECTION_KEEP_ALIVE: ms after the last write until the device closes the link
KEEPALIVE_MS = 10_000


@dataclass(frozen=True)
class Params:
    """What the control characteristic holds: the limits of the device's side of the transport."""

    flags: int
    max_instances: int
    max_free_chunks: int
    chunk_size: int
    max_att_length: int

    @classmethod
    def parse(cls, data: bytes) -> Params:
        """``flags u8, max_instances u16, max_free_chunks u8, chunk_size u8 [, max_att_length u16]``, little endian."""
        if len(data) < 5:
            raise ProtocolError(f"the control characteristic holds {data.hex() or 'nothing'}, expected at least 5 bytes")
        att = int.from_bytes(data[5:7], "little") if len(data) >= 7 else 0
        return cls(data[0], int.from_bytes(data[1:3], "little"), data[3], data[4], max(att, 20))


def _message(*items: Any) -> bytes:
    return b"".join(cbor2.dumps(int(i) if isinstance(i, IntEnum) else i) for i in items)


def get_devices() -> bytes:
    return _message(Op.GET_DEVICES)


def subscribe(instance: int) -> bytes:
    return _message(Op.SUBSCRIBE, instance)


def get_values(instance: int, vregs: Iterable[int]) -> bytes:
    """``05 03 82 19 0100 19 0102`` reads registers 0x0100 and 0x0102 of instance 3."""
    return _message(Op.GET_VALUES, instance, list(vregs))


def set_value(instance: int, vreg: int, value: bytes) -> bytes:
    """``06 03 82 19 edab 41 02`` sets register 0xEDAB of instance 3 to 2."""
    return _message(Op.SET_VALUES, instance, [vreg, value])


def keepalive() -> bytes:
    """``06 00 82 18 93 42 10 27``: the device closes the link 10 s after the last one."""
    return set_value(ROOT, VREG_KEEPALIVE, KEEPALIVE_MS.to_bytes(2, "little"))


@dataclass(frozen=True)
class Message:
    op: int
    instance: int | None
    args: tuple[Any, ...]


def parse_messages(data: bytes, arity: dict[Op, int] = DEVICE_ARGS) -> list[Message]:
    """The messages of one received chunk. Like VictronConnect, an unknown opcode ends the chunk: its length is not known."""
    stream = io.BytesIO(data)
    decoder = cbor2.CBORDecoder(stream)
    messages: list[Message] = []
    while stream.tell() < len(data):
        try:
            op = decoder.decode()
            count = arity.get(op) if isinstance(op, int) else None
            if count is None:
                log.debug("Unknown VE.Smart opcode %r, skipping the rest of %s", op, data.hex())
                break
            args = tuple(decoder.decode() for _ in range(count))
        except (cbor2.CBORDecodeError, EOFError) as exc:
            log.debug("Cannot decode the VE.Smart chunk %s: %s", data.hex(), exc)
            break
        if op in _WITHOUT_INSTANCE:
            messages.append(Message(op, None, args))
        else:
            messages.append(Message(op, args[0], args[1:]))
    return messages


def pieces(chunk: bytes, max_att_length: int) -> list[tuple[str, bytes]]:
    """The writes of one chunk: (characteristic, bytes), the last one on lastData."""
    parts = [chunk[i : i + max_att_length] for i in range(0, len(chunk), max_att_length)]
    return [(LAST_DATA if i == len(parts) - 1 else DATA, part) for i, part in enumerate(parts)]


class Transport:
    """Chunks, credit and reassembly on one GATT link. ``on_message`` gets every message the device sends."""

    def __init__(self, link: GattLink, on_message: Callable[[Message], None]) -> None:
        self.link = link
        self._on_message = on_message
        self.params: Params | None = None
        self.device_errors: list[int] = []  # codes the device sent with CTL_ERROR
        self._free = 0  # chunks the device allows us
        self._credit = asyncio.Event()
        self._lock = asyncio.Lock()
        self._received = 0  # chunks received since we last gave them back
        self._rx = b""
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    async def open(self) -> Params:
        """Read the parameters, open the transport and allow the device its first chunks."""
        self.params = Params.parse(await self.link.read(CONTROL))
        log.debug("VE.Smart parameters: %s", self.params)
        await self.link.write(CONTROL, OPEN)
        self._received = 0
        await self._give_back(INITIAL_GRANT)
        return self.params

    async def send(self, message: bytes) -> None:
        """Write one message as one chunk, once the device allows it."""
        params = self.params
        if params is None:
            raise ProtocolError("the VE.Smart transport is not open")
        if len(message) > params.chunk_size:
            raise ProtocolError(f"a message of {len(message)} bytes does not fit a chunk of {params.chunk_size}")
        async with self._lock:
            await self._wait_for_credit()
            self._free -= 1
            for char, part in pieces(message, params.max_att_length):
                await self.link.write(char, part)

    async def _wait_for_credit(self) -> None:
        while self._free <= 0:
            if self._closed:
                raise NotConnectedError("link lost")
            self._credit.clear()
            try:
                await asyncio.wait_for(self._credit.wait(), FREE_CHUNK_TIMEOUT_S)
            except TimeoutError:
                raise ProtocolError(f"the device allowed no chunk for {FREE_CHUNK_TIMEOUT_S:g} s") from None
        if self._closed:
            raise NotConnectedError("link lost")

    async def _give_back(self, count: int) -> None:
        await self.link.write(CONTROL, bytes([CTL_READY, count]))

    def feed(self, char: str, data: bytes) -> None:
        """A notification of the link (on the event loop thread)."""
        if char == CONTROL:
            self._control(data)
        elif char == DATA:
            self._rx += data
        elif char == LAST_DATA:
            chunk, self._rx = self._rx + data, b""
            self._received += 1
            if self._received > GRANT_AFTER and not self._closed:
                # Counted down here, not in the task: the device keeps its credit in one byte, and
                # chunks that arrive before the task runs must not be given back twice (it overflows).
                count, self._received = self._received, 0
                self._spawn(self._give_back(count))
            for message in parse_messages(chunk):
                self._on_message(message)

    def _control(self, data: bytes) -> None:
        op = data[0] if data else None
        if op == CTL_READY and len(data) >= 2:
            limit = max(1, self.params.max_free_chunks) if self.params is not None else 0xFF
            self._free = min(self._free + data[1], limit)
            self._credit.set()
        elif op == CTL_RESET:
            self._rx = b""
        elif op == CTL_ERROR:
            code = int.from_bytes(data[1:3], "little") if len(data) >= 2 else -1
            self.device_errors.append(code)
            log.debug("The device reports VE.Smart error %d", code)
        else:
            # BlueZ may also deliver the parameters read at the start as a notification
            log.debug("Ignoring %s on the control characteristic", data.hex())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.debug("Cannot give chunks back to the device: %s", task.exception())

    def close(self) -> None:
        """The link is gone: wake a sender that waits for credit."""
        self._closed = True
        self._credit.set()
        for task in list(self._tasks):
            task.cancel()
