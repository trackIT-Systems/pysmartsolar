import asyncio

import pytest

from fakes import A, TEST_KEY, FakeBackend, FakeLink, run, until
from smartsolar.errors import NotConnectedError, ProtocolError
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
