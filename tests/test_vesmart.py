import asyncio

import pytest
from fakes import A, FakeBackend, FakeLink, run, until

from smartsolar import vesmart
from smartsolar.errors import NotConnectedError, ProtocolError
from smartsolar.vesmart import Message, Op, Params, Transport, parse_messages


def test_requests_match_the_captured_ones():
    assert vesmart.get_devices() == bytes.fromhex("01")
    assert vesmart.subscribe(0) == bytes.fromhex("0300") and vesmart.subscribe(3) == bytes.fromhex("0303")
    assert vesmart.get_values(0, [0xEC65]) == bytes.fromhex("05008119ec65")
    assert vesmart.get_values(3, [0x0100, 0x0102]) == bytes.fromhex("050382190100190102")
    assert vesmart.get_values(0, [0x90]) == bytes.fromhex("0500811890")  # one byte CBOR ids work too
    assert vesmart.set_value(3, 0xEDAB, b"\x02") == bytes.fromhex("06038219edab4102")  # load output control = 2
    assert vesmart.set_value(3, 0x0200, b"\x04") == bytes.fromhex("0603821902004104")
    assert vesmart.keepalive() == bytes.fromhex("0600821893421027")


def test_parameters_of_the_smartsolar():
    p = Params.parse(bytes.fromhex("00040001de4a00"))
    assert (p.flags, p.max_instances, p.max_free_chunks, p.chunk_size, p.max_att_length) == (0, 4, 1, 222, 74)
    assert Params.parse(bytes.fromhex("0004000114")).max_att_length == 20  # without the optional length
    with pytest.raises(ProtocolError, match="at least 5"):
        Params.parse(b"\x00\x04")


def test_captured_device_messages():
    # the answer to the device list request, and three subscriptions confirmed in one chunk
    assert parse_messages(bytes.fromhex("029f000001000301ff")) == [Message(Op.DEVICE_LIST, None, ([0, 0, 1, 0, 3, 1],))]
    assert parse_messages(bytes.fromhex("0700030007000300")) == [Message(Op.RESPONSE, 0, (3, 0))] * 2
    # several values in one chunk; the keepalive echo has a one byte register id
    assert parse_messages(bytes.fromhex("080319ed8f420000080319ed8c4414000000" + "08001893421027")) == [
        Message(Op.VALUE, 3, (0xED8F, b"\x00\x00")),
        Message(Op.VALUE, 3, (0xED8C, bytes.fromhex("14000000"))),
        Message(Op.VALUE, 0, (0x93, b"\x10\x27")),
    ]
    assert parse_messages(bytes.fromhex("090019edd501")) == [Message(Op.VALUE_RESPONSE, 0, (0xEDD5, 1))]


def test_an_unknown_opcode_or_broken_cbor_ends_the_chunk():
    assert parse_messages(bytes.fromhex("080319ed8d423305" + "0e0102")) == [Message(Op.VALUE, 3, (0xED8D, b"\x33\x05"))]
    assert parse_messages(bytes.fromhex("0800ff")) == []
    assert parse_messages(bytes.fromhex("080319ed8d42")) == []  # cut off


def test_a_chunk_is_split_on_data_and_ends_on_last_data():
    assert vesmart.pieces(bytes(10), 74) == [("0024", bytes(10))]
    parts = vesmart.pieces(bytes(range(150)), 74)
    assert [c for c, _ in parts] == ["0027", "0027", "0024"] and b"".join(p for _, p in parts) == bytes(range(150))
    assert [len(p) for _, p in parts] == [74, 74, 2]


class Recorder:
    def __init__(self):
        self.messages = []

    def __call__(self, message):
        self.messages.append(message)


async def open_transport(backend=None):
    backend = backend or FakeBackend()
    link = FakeLink(A, backend, lambda: None)
    got = Recorder()
    transport = Transport(link, got)
    await link.start_notify(transport.feed)
    await transport.open()
    return link, transport, got


def test_open_reads_the_parameters_and_allows_the_device_its_chunks():
    async def go():
        link, transport, _ = await open_transport()
        assert transport.params.chunk_size == 222
        assert link.writes == [("0021", bytes.fromhex("fa80ff")), ("0021", bytes.fromhex("f980"))]

    run(go())


def test_one_chunk_at_a_time():
    async def go():
        link, transport, got = await open_transport()
        await asyncio.gather(*(transport.send(vesmart.get_values(3, [0xED8D])) for _ in range(5)))
        await until(lambda: len(got.messages) == 5)
        assert link.credit_violations == 0 and [m.args[0] for m in got.messages] == [0xED8D] * 5

    run(go())


def test_messages_split_across_notifications_are_put_together():
    async def go():
        backend = FakeBackend()
        link, transport, got = await open_transport(backend)
        vregs = list(backend.registers)[:30]  # an answer of several pieces
        await transport.send(vesmart.get_values(3, vregs))
        await until(lambda: len(got.messages) == len(vregs))
        assert [m.args[0] for m in got.messages] == vregs

    run(go())


def test_received_chunks_are_given_back():
    async def go():
        link, transport, _ = await open_transport()
        for _ in range(vesmart.GRANT_AFTER + 1):
            link.push(0xED8D, b"\x33\x05")
        await until(lambda: link.granted == vesmart.INITIAL_GRANT + vesmart.GRANT_AFTER + 1)

    run(go())


def test_a_burst_is_given_back_once():
    """Chunks that arrive before the give-back is written must not be given back again: the device
    keeps its credit in one byte, and the overflow stopped it sending (seen on a SmartSolar)."""

    async def go():
        link, transport, _ = await open_transport()
        for _ in range(200):
            link.push(0xED8D, b"\x33\x05")
        await asyncio.sleep(0.05)
        grants = [data for char, data in link.writes if char == "0021" and data[0] == 0xF9][1:]
        assert all(0 < g[1] for g in grants) and sum(g[1] for g in grants) <= 200
        assert link.granted - vesmart.INITIAL_GRANT == sum(g[1] for g in grants) >= 200 - vesmart.GRANT_AFTER

    run(go())


def test_a_sender_waiting_for_credit_learns_that_the_link_is_lost():
    async def go():
        link, transport, _ = await open_transport()
        await until(lambda: link.credit == 1)
        link._allow = lambda: None  # the device stops allowing chunks
        await transport.send(vesmart.get_devices())
        waiting = asyncio.create_task(transport.send(vesmart.get_devices()))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        transport.close()
        with pytest.raises(NotConnectedError):
            await waiting

    run(go())


def test_a_message_larger_than_a_chunk_is_refused():
    async def go():
        _, transport, _ = await open_transport()
        with pytest.raises(ProtocolError, match="does not fit"):
            await transport.send(vesmart.get_values(3, list(range(0x1000, 0x1050))))

    run(go())


def test_control_errors_and_resets():
    async def go():
        _, transport, got = await open_transport()
        transport.feed("0027", bytes.fromhex("0803"))
        transport.feed("0021", bytes.fromhex("f8"))  # the partly sent chunk is dropped
        transport.feed("0024", bytes.fromhex("080319ed8d423305"))
        transport.feed("0021", bytes.fromhex("f702"))
        transport.feed("0021", bytes.fromhex("00040001de4a00"))  # the parameters again: ignored
        assert [m.args for m in got.messages] == [(0xED8D, b"\x33\x05")] and transport.device_errors == [2]

    run(go())
