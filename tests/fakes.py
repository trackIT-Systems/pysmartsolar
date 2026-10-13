"""A fake Victron device behind the BleBackend / GattLink interfaces. No real key is used anywhere."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import cbor2

from smartsolar import instant
from smartsolar.ble import Advertisement
from smartsolar.errors import ConnectError, NotConnectedError
from smartsolar.vreg import PING, parse_requests

TEST_KEY = bytes.fromhex("4b2f9c10e7a35d68c1b0f4372ea98d56")
OTHER_KEY = bytes.fromhex("4b00112233445566778899aabbccddee")  # same check byte, other key
WRONG_CHECK_KEY = bytes.fromhex("aa2f9c10e7a35d68c1b0f4372ea98d56")
A = "F7:48:18:8A:77:9D"
B = "E1:22:33:44:55:66"
ACK = bytes.fromhex("f901")


def run(coro):
    return asyncio.run(coro)


async def until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def record8(vreg: int, value: bytes, ctx: int = 0) -> bytes:
    return bytes([0x08, ctx]) + cbor2.dumps(vreg) + cbor2.dumps(value)


def advert_data(*, nonce: int = 1, key: bytes = TEST_KEY, model: int = 0xA053, record_type: int = 1, **record) -> bytes:
    plain = instant.solar_charger_record(**record)
    return instant.encrypt(key, instant.Header(model, record_type, nonce, key[0]), plain)


def advert(address: str = A, *, rssi: float = -60, name: str | None = "SmartSolar", at: float | None = None, **kw) -> Advertisement:
    return Advertisement(address=address, name=name, rssi=rssi, seen_at=time.monotonic() if at is None else at, data=advert_data(**kw))


def default_values() -> dict[int, bytes]:
    return {
        0x0102: bytes.fromhex("00640100"),
        0xEC5A: (1000).to_bytes(4, "little"),
        0xEC66: bytes.fromhex("f748188a779d"),
        0xED8D: (1331).to_bytes(2, "little"),
        0xEDA9: (1331).to_bytes(2, "little"),
        0xEDEC: bytes.fromhex("ffff"),
        0xEC65: TEST_KEY,
    }


def default_registers() -> dict[int, bytes]:
    """What the fake answers to a read (ctx 3); the values are the ones the real device had."""
    registers = dict(default_values())
    registers.update(
        {
            0x0200: b"\x01", 0x0201: b"\x00", 0xEDDA: b"\x00", 0xEDD5: (1331).to_bytes(2, "little"), 0xEDD7: bytes(2),
            0xEDD3: bytes(4), 0xEDD2: bytes(4), 0xEDD1: bytes(4), 0xEDD0: bytes(4), 0xEDDC: (9847).to_bytes(4, "little"),
            0xEDDB: bytes.fromhex("400b"), 0xEDBB: bytes.fromhex("0300"), 0xEDBC: bytes(4), 0xEDAB: b"\x02", 0xEDAD: bytes(2),
            0xEDF0: bytes.fromhex("9600"), 0xEDF1: b"\xff", 0xEDF6: bytes.fromhex("4605"), 0xEDF7: bytes.fromhex("aa05"), 0xEDEF: b"\x0c",
            # the rest of the settings of the SmartSolar 75/15 used for the tests (a LiFePO4 profile)
            0xEDF4: bytes(2), 0xEDF2: bytes(2), 0xEDFB: bytes.fromhex("c800"), 0xEDFE: b"\x00", 0xEDFD: b"\x00", 0xEDE4: b"\x00",
            0xEDE3: bytes.fromhex("c800"), 0xEDE5: b"\x00", 0xED2E: bytes.fromhex("0a00"), 0xEDE7: bytes(2), 0xEDE0: bytes.fromhex("f401"),
            0xEDE6: b"\xff\xff", 0xED9D: bytes.fromhex("7805"), 0xED9C: bytes.fromhex("e803"), 0xED90: bytes.fromhex("1e00"),
            0xED98: b"\x01", 0xED9E: b"\x00", 0x0100: bytes.fromhex("0053a0fe"), 0x2211: bytes.fromhex("2003"), 0x2212: bytes.fromhex("cc06"),
        }
    )
    return registers


from smartsolar.vreg import WRITABLE  # noqa: E402

WRITABLE_REGISTERS = {w.vreg for w in WRITABLE.values()}


class FakeLink:
    """A GATT link to a fake device: answers the init sequence like the real one did."""

    def __init__(self, address: str, backend: FakeBackend, on_disconnect: Callable[[], None]) -> None:
        self.address = address
        self.backend = backend
        self._on_disconnect = on_disconnect
        self._connected = True
        self._callback: Callable[[str, bytes], None] | None = None
        self.writes: list[tuple[str, bytes]] = []
        self.pings = 0
        self.disconnect_calls = 0
        self._deadline: asyncio.TimerHandle | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start_notify(self, callback: Callable[[str, bytes], None]) -> None:
        self._callback = callback

    async def write(self, char: str, data: bytes) -> None:
        if not self._connected:
            raise NotConnectedError("not connected")
        self.writes.append((char, data))
        asyncio.get_running_loop().call_soon(self._respond, char, data)

    async def pair(self, passkey: str, timeout_s: float) -> None:
        self.backend.pair_calls.append(passkey)
        if self.backend.pair_error is not None:
            raise self.backend.pair_error

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False
        if self._deadline is not None:
            self._deadline.cancel()

    # -- the device side ----------------------------------------------------

    def _emit(self, char: str, data: bytes) -> None:
        if self._connected and self._callback is not None:
            self._callback(char, data)

    def _respond(self, char: str, data: bytes) -> None:
        self._emit("0021", ACK)
        if data.startswith(PING):
            self.pings += 1
            self._arm_deadline()
            self._emit("0024", bytes.fromhex("08001893421027"))
        if char == "0024" and data[:1] in (b"\x05", b"\x06"):
            self._answer_requests(data)
        if char == "0021" and data == bytes.fromhex("f941"):
            burst = b"".join(record8(v, raw, ctx=3 if v != 0xEC65 else 0) for v, raw in self.backend.values.items())
            for i in range(0, len(burst), 20):  # a record can be split across notifications
                self._emit("0024", burst[i : i + 20])

    def _answer_requests(self, data: bytes) -> None:
        try:
            requests = parse_requests(data)
        except Exception:  # a record that continues in the next write
            return
        for kind, ctx, items in requests:
            if ctx != 3:
                continue  # the real device refuses these for most registers; the tests do not need it
            if kind == 5:
                for vreg in items:
                    self.backend.get_log.append(vreg)
                    if vreg in self.backend.registers:
                        self._emit("0024", record8(vreg, self.backend.registers[vreg], ctx=3))
                    else:
                        self._emit("0024", bytes([0x09, 3]) + cbor2.dumps(vreg) + cbor2.dumps(1))
            elif kind == 6:
                vreg, raw = items
                self.backend.set_log.append((vreg, raw))
                if vreg in WRITABLE_REGISTERS and not self.backend.refuse_writes:
                    self.backend.registers[vreg] = raw
                    self._emit("0024", record8(vreg, raw, ctx=3))
                # a write the device does not accept gets no answer

    def push(self, vreg: int, raw: bytes) -> None:
        self.backend.values[vreg] = raw
        self._emit("0024", record8(vreg, raw, ctx=3))

    def _arm_deadline(self) -> None:
        if self.backend.keepalive_timeout is None:
            return
        if self._deadline is not None:
            self._deadline.cancel()
        self._deadline = asyncio.get_running_loop().call_later(self.backend.keepalive_timeout, self.drop_remote)

    def drop_remote(self) -> None:
        """The device closes the link."""
        if self._connected:
            self._connected = False
            self._on_disconnect()


class FakeBackend:
    def __init__(self) -> None:
        self.on_advert: Callable[[Advertisement], None] | None = None
        self.scanning = False
        self.scan_starts = 0
        self.values = default_values()
        self.registers = default_registers()
        self.get_log: list[int] = []
        self.set_log: list[tuple[int, bytes]] = []
        self.refuse_writes = False
        self.keepalive_timeout: float | None = None
        self.connect_errors: list[Exception] = []  # popped one per connect call
        self.connect_delay = 0.0
        self.pair_error: Exception | None = None
        self.pair_calls: list[str] = []
        self.links: list[FakeLink] = []
        self.scanning_during_connect: list[bool] = []
        self.connect_passkeys: list[str | None] = []

    async def start_scan(self, on_advert: Callable[[Advertisement], None]) -> None:
        self.on_advert = on_advert
        self.scanning = True
        self.scan_starts += 1

    async def stop_scan(self) -> None:
        self.scanning = False
        self.on_advert = None

    def emit(self, adv: Advertisement) -> None:
        assert self.on_advert is not None, "scan not started"
        self.on_advert(adv)

    async def connect(self, adv: Advertisement, *, timeout_s: float, on_disconnect: Callable[[], None], passkey: str | None = None) -> FakeLink:
        self.connect_passkeys.append(passkey)
        self.scanning_during_connect.append(self.scanning)  # the real backend stops the scan here
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        link = FakeLink(adv.address, self, on_disconnect)
        self.links.append(link)
        return link


def connect_error(message: str = "connect timed out") -> ConnectError:
    return ConnectError(message)


class FakeMqtt:
    def __init__(self):
        self.published = []
        self.wills = []
        self.connected_to = None
        self.looping = False

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, payload, retain))

    def connect_async(self, host, port):
        self.connected_to = (host, port)

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        self.connected_to = None

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


# -- the VE.Direct cable ------------------------------------------------------------

from smartsolar import vedirect  # noqa: E402
from smartsolar.errors import ConnectError as _ConnectError  # noqa: E402

SERIAL_NUMBER = "HQ00000TEST"
TEXT_FIELDS = [
    ("PID", "0xA053"), ("FW", "164"), ("SER#", SERIAL_NUMBER), ("V", "13320"), ("I", "-10"), ("VPV", "24010"),
    ("PPV", "2"), ("CS", "3"), ("MPPT", "2"), ("OR", "0x00000000"), ("ERR", "0"), ("LOAD", "ON"), ("IL", "0"),
    ("H19", "9847"), ("H20", "1"), ("H21", "2"), ("H22", "0"), ("H23", "0"), ("HSDS", "343"),
]


def text_block(fields=None, *, corrupt: bool = False) -> bytes:
    body = b"".join(b"\r\n" + k.encode() + b"\t" + v.encode() for k, v in (fields or TEXT_FIELDS)) + b"\r\nChecksum\t"
    checksum = (-sum(body)) & 0xFF
    return body + bytes([checksum ^ 1 if corrupt else checksum])


class FakeSerial:
    """The device end of a VE.Direct cable: pushes text blocks on request and answers HEX frames."""

    def __init__(self, registers: dict[int, bytes] | None = None) -> None:
        self.registers = dict(default_registers() if registers is None else registers)
        self.writes: list[bytes] = []
        self.refuse_flags: int | None = None  # answer every set with these flags instead of storing
        self.swallow = 0  # do not answer the next n requests
        self.silent = False  # never answer
        self.closed = False
        self.lost: Exception | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    # -- the SerialLink side ------------------------------------------------

    async def read(self) -> bytes:
        if self.lost is not None:
            raise self.lost
        data = await self._queue.get()
        if self.lost is not None:
            raise self.lost
        return data

    async def write(self, data: bytes) -> None:
        if self.lost is not None:
            raise self.lost
        self.writes.append(data)
        for frame in data.split(b"\n"):
            if frame.startswith(b":"):
                asyncio.get_running_loop().call_soon(self._answer, frame[1:])

    async def close(self) -> None:
        self.closed = True

    # -- driving it from a test ---------------------------------------------

    def push(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def block(self, **overrides: str) -> None:
        fields = [(k, overrides.get(k, v)) for k, v in TEXT_FIELDS]
        self.push(text_block(fields))

    def unplug(self) -> None:
        self.lost = NotConnectedError("device reports readiness to read but returned no data")
        self._queue.put_nowait(b"")

    # -- the device side ----------------------------------------------------

    def _reply(self, command: int, payload: bytes = b"") -> None:
        self.push(vedirect.hex_frame(command, payload))

    def _answer(self, body: bytes) -> None:
        frame = vedirect.parse_hex_frame(body.strip(b"\r"))
        if frame is None or self.silent:
            return
        if self.swallow:
            self.swallow -= 1
            return
        if frame.command == 3:
            self._reply(1, bytes.fromhex("6441"))  # release 1.64
            return
        if frame.command == vedirect.CMD_GET:
            vreg = frame.vreg
            if vreg in self.registers:
                self._reply(7, vreg.to_bytes(2, "little") + b"\x00" + self.registers[vreg])
            else:
                self._reply(7, vreg.to_bytes(2, "little") + b"\x01")
        elif frame.command == vedirect.CMD_SET:
            vreg = frame.vreg
            if vreg not in self.registers:
                self._reply(8, vreg.to_bytes(2, "little") + b"\x01")
            elif self.refuse_flags is not None:
                self._reply(8, vreg.to_bytes(2, "little") + bytes([self.refuse_flags]))
            else:
                self.registers[vreg] = frame.value
                self._reply(8, vreg.to_bytes(2, "little") + b"\x00" + frame.value)


class FakeSerialOpener:
    """Hands out the devices in turn; with none left (or ``missing``) the port does not exist."""

    def __init__(self, *devices: FakeSerial) -> None:
        self.devices = list(devices)
        self.missing = False
        self.opened: list[tuple[str, int]] = []
        self.current: FakeSerial | None = None

    async def __call__(self, port: str, baud: int) -> FakeSerial:
        self.opened.append((port, baud))
        if self.missing or not self.devices:
            raise _ConnectError(f"cannot open {port}: no such file", not_found=True)
        self.current = self.devices.pop(0)
        return self.current
