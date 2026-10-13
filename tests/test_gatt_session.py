import asyncio

import pytest

from fakes import A, TEST_KEY, FakeBackend, FakeLink, run, until
from smartsolar.errors import NotConnectedError, ProtocolError, WriteFailed
from smartsolar.gatt_session import GattSession
from smartsolar.vreg import INIT_SEQUENCE, VREG_KEY


async def open_session(backend=None, keepalive_s=0.05):
    backend = backend or FakeBackend()
    lost = []
    link = FakeLink(A, backend, lambda: lost.append(1))
    session = GattSession(link, keepalive_s=keepalive_s)
    return backend, link, session, lost


def test_start_sends_the_init_sequence_in_order():
    async def go():
        _, link, session, _ = await open_session(keepalive_s=0)
        await session.start()
        assert link.writes == [(c, bytes.fromhex(h)) for c, h in INIT_SEQUENCE]
        await session.close()

    run(go())


def test_values_arrive_across_split_notifications_and_acks_are_counted():
    async def go():
        _, link, session, _ = await open_session(keepalive_s=0)
        await session.start()
        await until(lambda: 0xED8D in session.values)
        assert session.values[0xED8D] == (1331).to_bytes(2, "little")
        assert session.values[0xEC66] == bytes.fromhex("f748188a779d")
        assert session.acks >= len(INIT_SEQUENCE) - 1
        assert session.errors == {}
        await session.close()

    run(go())


def test_fetch_key():
    async def go():
        _, _, session, _ = await open_session(keepalive_s=0)
        await session.start()
        assert await session.fetch_key(1.0) == TEST_KEY
        await session.close()

    run(go())


def test_fetch_key_rejects_a_wrong_length():
    async def go():
        backend, _, session, _ = await open_session(keepalive_s=0)
        backend.values[VREG_KEY] = b"short"
        await session.start()
        with pytest.raises(ProtocolError, match="16 bytes"):
            await session.fetch_key(1.0)
        await session.close()

    run(go())


def test_wait_for_times_out_with_a_protocol_error():
    async def go():
        _, _, session, _ = await open_session(keepalive_s=0)
        await session.start()
        with pytest.raises(ProtocolError, match="0xbeef"):
            await session.wait_for(0xBEEF, 0.1)
        await session.close()

    run(go())


def test_keepalive_pings_keep_the_link_open_and_stop_with_the_session():
    async def go():
        backend = FakeBackend()
        backend.keepalive_timeout = 0.3
        _, link, session, lost = await open_session(backend, keepalive_s=0.1)
        await session.start()
        await asyncio.sleep(0.8)
        assert link.is_connected and not lost and link.pings >= 5
        await session.close()
        pings = link.pings
        await asyncio.sleep(0.2)
        assert link.pings == pings and link.disconnect_calls == 1

    run(go())


def test_without_keepalive_the_device_closes_the_link():
    async def go():
        backend = FakeBackend()
        backend.keepalive_timeout = 0.3
        _, link, session, lost = await open_session(backend, keepalive_s=0)
        await session.start()
        await until(lambda: lost, timeout=2.0)
        session.on_disconnect()
        assert session.lost.is_set()
        with pytest.raises(NotConnectedError):
            await session.wait_for(0xBEEF, 1.0)
        await session.close()

    run(go())


def test_a_lost_link_wakes_waiters():
    async def go():
        _, _, session, _ = await open_session(keepalive_s=0)
        waiter = asyncio.create_task(session.wait_for(0xBEEF, 5.0))
        await asyncio.sleep(0.05)
        session.on_disconnect()
        with pytest.raises(NotConnectedError):
            await waiter

    run(go())


def test_error_records_are_kept_apart_from_values():
    async def go():
        _, _, session, _ = await open_session(keepalive_s=0)
        session._on_notify("0024", bytes.fromhex("090019edd501"))
        session._on_notify("0021", bytes.fromhex("f901"))
        session._on_notify("0021", bytes.fromhex("1234"))  # other bytes on the control characteristic are ignored
        assert session.errors == {0xEDD5: 1} and session.values == {} and session.acks == 1

    run(go())


def test_request_reads_a_register_after_the_init_sequence():
    async def go():
        backend, link, session, _ = await open_session(keepalive_s=0)
        await session.start()
        assert await session.request(0xEDD5, 1.0) == (1331).to_bytes(2, "little")
        backend.registers[0xEDD5] = (1400).to_bytes(2, "little")
        assert await session.request(0xEDD5, 1.0) == (1400).to_bytes(2, "little")  # fresh, not the first value
        await session.close()

    run(go())


def test_request_for_an_unavailable_register_fails_and_is_remembered_by_refresh():
    async def go():
        backend, _, session, _ = await open_session(keepalive_s=0)
        await session.start()
        with pytest.raises(ProtocolError, match="not available"):
            await session.request(0xBEEF, 1.0)
        await session.refresh([0xBEEF, 0xEDD5, 0xEDAB])
        assert session.unavailable == {0xBEEF} and 0xEDD5 in session.values and 0xEDAB in session.values
        before = len(backend.get_log)
        await session.refresh([0xBEEF])
        assert len(backend.get_log) == before  # not asked again
        await session.close()

    run(go())


def test_refresh_continues_when_a_register_does_not_answer():
    async def go():
        backend, _, session, _ = await open_session(keepalive_s=0)
        await session.start()
        original = backend.registers.pop(0xEDD5)
        backend.registers[0xEDD5] = original
        # a register that never answers (not even with an error) is skipped after the timeout
        session.link.backend.registers.pop(0xEDDB)
        link_emit = session.link._emit
        session.link._emit = lambda char, data: None if (b"\x19\xed\xdb" in data) else link_emit(char, data)
        await session.refresh([0xEDDB, 0xEDD5], timeout_s=0.1)
        assert 0xEDD5 in session.values and 0xEDDB not in session.unavailable
        await session.close()

    run(go())


def test_write_register_waits_for_the_echo_and_the_register_changes():
    async def go():
        backend, link, session, _ = await open_session(keepalive_s=0)
        await session.start()
        echoed = await session.write_register(0xEDAB, b"\x04", 1.0)
        assert echoed == b"\x04" and backend.registers[0xEDAB] == b"\x04" and backend.set_log == [(0xEDAB, b"\x04")]
        await session.close()

    run(go())


def test_a_write_the_device_does_not_answer_fails():
    async def go():
        backend, _, session, _ = await open_session(keepalive_s=0)
        await session.start()
        with pytest.raises(WriteFailed, match="did not confirm"):
            await session.write_register(0xEDD5, b"\x00\x00", 0.2)  # a read-only register: no answer
        assert backend.registers[0xEDD5] == (1331).to_bytes(2, "little")
        await session.close()

    run(go())


def test_an_older_pushed_value_does_not_count_as_the_echo():
    async def go():
        backend, link, session, _ = await open_session(keepalive_s=0)
        await session.start()
        backend.refuse_writes = True
        task = asyncio.create_task(session.write_register(0xEDAB, b"\x04", 0.5))
        await asyncio.sleep(0.05)
        link.push(0xEDAB, b"\x02")  # the device pushes the old value: not the confirmation
        with pytest.raises(WriteFailed):
            await task
        await session.close()

    run(go())


def test_a_write_to_a_lost_link_raises_not_connected():
    async def go():
        _, _, session, _ = await open_session(keepalive_s=0)
        await session.start()
        task = asyncio.create_task(session.write_register(0xEDAB, b"\x04", 2.0))
        session.link.backend.refuse_writes = True
        await asyncio.sleep(0.05)
        session.on_disconnect()
        with pytest.raises(NotConnectedError):
            await task

    run(go())
